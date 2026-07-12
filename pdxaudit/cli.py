"""Diff mod overrides and referenced tokens against vanilla patch changes.

Four audits (run one, or all at once with --all), all driven by the
vanilla-tracker bare git repo:

  Override audit (default): finds every INJECT:/REPLACE:/TRY_INJECT:/TRY_REPLACE: directive
  in the mod, locates the target block in vanilla at two commits (old, new), and
  reports what vanilla changed. For changed REPLACE blocks it also checks the
  mod's replacement text and reports whether it already contains vanilla's new
  lines (reconciled) or is missing them (stale). REPLACE blocks vanilla changed
  are highest priority; the mod may be silently suppressing those improvements.

  Dependency audit (--deps): extracts every `token =` identifier the mod's
  scripts assign, then flags any that vanilla used at the old commit but no
  longer uses at the new commit (present-then-gone = likely renamed/removed).
  Catches silent breakage the override audit cannot see, e.g. a modifier key
  the mod writes that vanilla dropped. Suggests rename candidates. Findings are
  suspects to verify with pdx-syntax, not confirmed breakage.

  GUI audit (--gui): finds implicit GUI overrides: mod .gui template/type
  definitions that shadow a same-name vanilla definition (first-loaded file
  wins, hence the aaa_ prefix convention), plus mod .gui files that replace a
  vanilla file at the same relative path, and reports which shadowed
  definitions vanilla changed between the two commits. For changed definitions
  it also checks whether the mod's copy already contains vanilla's new lines.
  Load order is approximated by case-insensitive path sort; findings are
  suspects to verify in game.

  Localization audit (--loc): finds loc keys the mod redefines whose vanilla
  value changed or was removed between the two commits. Matched by
  (language, key), not by filename, since keys are not unique across languages
  and vanilla moves them between files. A mod's own new keys are counted as
  mod-only, not overrides.

Requires a vanilla-tracker bare git repo, resolved by precedence:
  1. --vanilla-repo <path> argument
  2. PDX_VANILLA_REPO environment variable
  3. config file ("vanilla_repo"; see config.sample.json)
  4. <mod-parent>/vanilla-tracker/repo.git (the convention)

The --deps vanilla vocabulary is cached per tracker commit under
<vanilla-tracker>/cache/, keyed by commit hash, so entries never go stale.
On every run a sample of live game files ($PDX_GAME_ROOT or the default
install) is hashed against the newest tracked commit; a mismatch means the
game patched but the tracker was not updated, and a warning is printed.

Usage (no audit flag runs all four; name one or more to run just those):
    pdx-audit                              # all audits (override, deps, GUI, loc)
    pdx-audit --overrides                  # override blocks only (the quick check)
    pdx-audit --deps                       # dependency audit (dropped tokens)
    pdx-audit --gui                        # GUI override audit (implicit shadowing)
    pdx-audit --loc                        # localization audit (overridden keys)
    pdx-audit --deps --gui                 # any combination runs just those
    pdx-audit --include-unchanged          # also list unchanged blocks/keys
    pdx-audit --diff                       # show unified diffs
    pdx-audit --full                       # widen window to oldest tracked commit
    pdx-audit --block farming_village
    pdx-audit --old abc1234 --new def5678
    pdx-audit --mod-root /path/to/mod
"""

import sys
import argparse

from .gui import run_gui_audit, run_stamp_fork_points
from .loc import run_loc_audit
from .overrides import run_deps_audit, run_override_audit
from .tracker import do_snapshot, find_mod_root, find_vanilla_repo, get_commits, resolve_ref, resolve_tracker_path, warn_if_tracker_stale

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--all", dest="run_all", action="store_true",
                    help="Run every audit (the default when no audit is named)")
    ap.add_argument("--include-unchanged", action="store_true",
                    help="Include unchanged blocks/keys in output")
    ap.add_argument("--diff", action="store_true",
                    help="Show full unified diffs for changed blocks")
    ap.add_argument("--overrides", action="store_true",
                    help="Override audit only: INJECT/REPLACE blocks vs vanilla")
    ap.add_argument("--deps", action="store_true",
                    help="Dependency audit: flag referenced tokens vanilla dropped")
    ap.add_argument("--gui", action="store_true",
                    help="GUI audit: implicit template/type shadowing and "
                         "same-path .gui file replacements")
    ap.add_argument("--loc", action="store_true",
                    help="Localization audit: flag loc keys the mod overrides "
                         "whose vanilla value changed or was removed")
    ap.add_argument("--full", action="store_true",
                    help="Fixed window from the oldest tracked commit to new "
                         "(GUI audit: opts out of the default fork-relative "
                         "baseline)")
    ap.add_argument("--since-fork", action="store_true",
                    help="No-op: the GUI audit is fork-relative by default now. "
                         "Kept so existing commands keep working")
    ap.add_argument("--stamp-fork-points", action="store_true",
                    help="Detect each mod .gui file's fork point and write a "
                         "'# pdx-audit fork-point:' comment at the top of the "
                         "file. Shows the plan and asks for confirmation before "
                         "modifying any files")
    ap.add_argument("--refresh", action="store_true",
                    help="With --stamp-fork-points, also rewrite existing pins "
                         "whose version no longer matches the file's contents")
    ap.add_argument("--block",
                    help="Audit a single block name only")
    ap.add_argument("--category",
                    help="Filter to a specific category directory")
    ap.add_argument("--old",
                    help="Old vanilla commit hash (default: second-most-recent)")
    ap.add_argument("--new",
                    help="New vanilla commit hash (default: most-recent)")
    ap.add_argument("--list-commits", action="store_true",
                    help="List available vanilla-tracker commits and exit")
    ap.add_argument("--mod-root",
                    help="Mod root directory (default: auto-detect via .metadata/)")
    ap.add_argument("--vanilla-repo",
                    help="Path to vanilla-tracker bare git repo")
    ap.add_argument("--snapshot", metavar="TAG",
                    help="Snapshot the current vanilla install into the "
                         "tracker as version TAG (creates the tracker repo "
                         "on first use), then exit")
    ap.add_argument("--patch-name", default="Pavia",
                    help="Patch name used in the snapshot commit message "
                         "(default: Pavia)")
    ap.add_argument("--game-root", metavar="DIR",
                    help="Game 'game' directory to snapshot from (default: "
                         "$PDX_GAME_ROOT or the Steam install). Point at an "
                         "extracted old-version copy to back-populate history")
    args = ap.parse_args()

    if args.snapshot:
        repo = resolve_tracker_path(args.mod_root, args.vanilla_repo)
        do_snapshot(repo, args.snapshot, args.patch_name, args.game_root)
        sys.exit(0)

    mod_root = find_mod_root(args.mod_root)
    vanilla_repo = find_vanilla_repo(mod_root, args.vanilla_repo)

    commits = get_commits(vanilla_repo)
    if not commits:
        print("No commits in vanilla-tracker.", file=sys.stderr)
        sys.exit(1)

    warn_if_tracker_stale(vanilla_repo, commits[0][0])

    if args.list_commits:
        print("Available vanilla-tracker commits:")
        for h, msg in commits:
            print(f"  {h}  {msg}")
        sys.exit(0)

    if args.stamp_fork_points:
        run_stamp_fork_points(mod_root, vanilla_repo, commits, refresh=args.refresh)
        sys.exit(0)

    if len(commits) < 2:
        print("Need at least 2 commits in vanilla-tracker.", file=sys.stderr)
        sys.exit(1)

    new_hash, new_msg = commits[0]
    old_hash, old_msg = commits[1]
    if args.full:
        old_hash, old_msg = commits[-1]
    if args.old:
        old_msg = resolve_ref(vanilla_repo, args.old, commits, "old")
        old_hash = args.old
    if args.new:
        new_msg = resolve_ref(vanilla_repo, args.new, commits, "new")
        new_hash = args.new

    # No audit flag runs everything; naming one or more runs just those.
    # --all is an explicit way to ask for the full set.
    picked = [name for name, on in (
        ("overrides", args.overrides), ("deps", args.deps),
        ("gui", args.gui), ("loc", args.loc)) if on]
    selected = picked if (picked and not args.run_all) else ["overrides", "deps", "gui", "loc"]

    runners = {
        "overrides": lambda: run_override_audit(
            mod_root, vanilla_repo, old_hash, old_msg, new_hash, new_msg, args),
        "deps": lambda: run_deps_audit(
            mod_root, vanilla_repo, old_hash, old_msg, new_hash, new_msg),
        "gui": lambda: run_gui_audit(
            mod_root, vanilla_repo, old_hash, old_msg, new_hash, new_msg, args),
        "loc": lambda: run_loc_audit(
            mod_root, vanilla_repo, old_hash, old_msg, new_hash, new_msg, args),
    }
    for i, name in enumerate(selected):
        if i:
            print("\n")
        runners[name]()
