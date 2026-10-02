"""Diff mod overrides and referenced names against vanilla patch changes.

Six audits (name one or more to run just those, or none to run all six), all
driven by the vanilla-tracker bare git repo:

  Override audit (--overrides): finds every INJECT:, REPLACE:, TRY_ and _OR_CREATE
  directive in the mod and compares each REPLACE with vanilla's
  tracked versions of its block, so a change vanilla made that your copy lacks is
  told apart from your own edits and from a change that meets one of them.

  Dependency audit (--deps): flags names the mod uses (keys it writes, names
  it references, GUI types and templates, and data-binding names in its .gui
  files) that vanilla used at some tracked version but no longer uses, with the
  patch that dropped them.

  GUI audit (--gui): finds mod .gui templates/types that shadow vanilla's and
  mod .gui files that replace a vanilla file, and compares each copy with
  vanilla's tracked versions the same way.

  Same-path file audit (--files): every other mod file at a vanilla file's path
  (events, map_data, setup, common/ files, .csv, shaders) replaces vanilla's whole
  file. Each copy is compared with vanilla's tracked versions definition by
  definition, and definitions vanilla added that the copy lacks are reported.

  Localization audit (--loc): loc keys the mod redefines whose vanilla value
  changed or was removed.

  Duplicate audit (--dupes): one source of truth per definition: names defined
  or overridden in more than one place, define keys set twice, GUI definitions
  defined twice, localization keys defined more than once, on_action effect and
  trigger conflicts, and plain redefinitions of vanilla names outside vanilla's file.

Findings records (dismissals and open findings) are kept in the
per-user data folder, keyed by the mod id and commit; pdx-audit never writes
into the mod.
"""

import datetime
import io
import json
import sys
import argparse
import types
from contextlib import redirect_stdout

from . import config, ledger, session
from .dupes import run_dupes_audit
from .files import run_file_audit
from .gui import run_gui_audit
from .loc import run_loc_audit
from .gui_names import run_deps
from .overrides import run_override_audit
from .base import VanillaBase
from .report import ColorWriter, color_enabled, render_triage, window_heading
from .results import build_payload, mod_fingerprint
from .store import open_store, orphan_note, remove_orphaned_records
from .tracker import (do_commit, find_mod_root, find_vanilla_repo, get_commits, locate_vanilla_repo,
                      patch_name, prune_cache, resolve_ref, resolve_tracker_path, warn_if_tracker_stale)

ALL_AUDITS = ["overrides", "deps", "gui", "files", "loc", "dupes"]
# Commands that do their own thing and exit; the --display app has a button for each.
APP_COMMANDS = ("dismiss", "undismiss", "show_dismissed", "remove_orphaned_records",
                "commit", "list_commits", "results_file")

# Config commands, which need no mod and no tracker, so they run before either is found.
CONFIG_COMMANDS = ("config", "set_value", "unset_value")


def _usage_error(msg):
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(2)


def _tag(msg):
    from .tracker import tag_of
    return tag_of(msg)


def build_parser():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--diff", action="store_true",
                    help="Show full unified diffs for changed blocks")
    ap.add_argument("--summary", action="store_true",
                    help="Print only the cross-audit summary, not the per-audit "
                         "detail (keeps large mods readable)")
    ap.add_argument("--display", action="store_true",
                    help="Open the desktop app: run the audits and act on the findings "
                         "with buttons")
    ap.add_argument("--results-file", metavar="FILE", help=argparse.SUPPRESS)
    ap.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                    help="Colorize output: auto (a terminal only), always, or never")
    ap.add_argument("--overrides", action="store_true",
                    help="Override audit: INJECT/REPLACE blocks vs vanilla")
    ap.add_argument("--deps", action="store_true",
                    help="Dependency audit: names the mod uses that vanilla no longer uses")
    ap.add_argument("--gui", action="store_true",
                    help="GUI audit: implicit template/type shadowing and "
                         "same-path .gui file replacements")
    ap.add_argument("--files", action="store_true",
                    help="Same-path file audit: mod files that replace a vanilla file "
                         "(events, map_data, setup, common/), compared definition by definition")
    ap.add_argument("--loc", action="store_true",
                    help="Localization audit: loc keys the mod overrides "
                         "whose vanilla value changed or was removed")
    ap.add_argument("--dupes", action="store_true",
                    help="Duplicate audit: one source of truth per definition")
    ap.add_argument("--full", action="store_true",
                    help="Compare every audit from the oldest tracked commit to new")
    ap.add_argument("--dismiss", nargs="+", metavar="ID",
                    help="Dismiss current findings by the id shown in the summary")
    ap.add_argument("--reason", metavar="TEXT",
                    help="With --dismiss, a note saved with the dismissal")
    ap.add_argument("--undismiss", nargs="+", metavar="ID",
                    help="Bring back dismissed findings")
    ap.add_argument("--show-dismissed", action="store_true",
                    help="List dismissed findings for this mod and commit")
    ap.add_argument("--remove-orphaned-records", action="store_true",
                    help="Remove saved records whose commit is on no branch of "
                         "this repository (asks for confirmation)")
    ap.add_argument("--force", action="store_true",
                    help="With --remove-orphaned-records, skip the confirmation")
    ap.add_argument("--block",
                    help="Audit a single block name only")
    ap.add_argument("--category",
                    help="Filter to a specific category directory "
                         "(with --overrides and/or --dupes only)")
    ap.add_argument("--old",
                    help="Old vanilla version tag or commit hash (default: the oldest commit for "
                         "the override and GUI audits, the commit before --new for the others)")
    ap.add_argument("--new",
                    help="New vanilla version tag or commit (default: most-recent)")
    ap.add_argument("--list-commits", action="store_true",
                    help="List available vanilla-tracker commits and exit")
    ap.add_argument("--mod-root",
                    help="Mod root directory (default: auto-detect via .metadata/)")
    ap.add_argument("--vanilla-repo",
                    help="Path to vanilla-tracker bare git repo")
    ap.add_argument("--commit", metavar="TAG",
                    help="Commit the current vanilla install into the "
                         "tracker as version TAG (creates the tracker repo "
                         "on first use), then exit")
    ap.add_argument("--patch-name",
                    help="Patch name used in the commit message (default: "
                         "$PDX_PATCH_NAME, the config file's patch_name, or Pavia)")
    ap.add_argument("--game-root", metavar="DIR",
                    help="Game 'game' directory to commit from (default: "
                         "$PDX_GAME_ROOT, the config file's game_root, or the Steam "
                         "install). Point at an extracted old-version copy to "
                         "back-populate history")
    # The settings options are in their own group, below the main options.
    ap.usage = ap.format_usage()[len("usage: "):].strip().replace(ap.prog, "%(prog)s", 1)
    setgrp = ap.add_argument_group(
        "settings",
        "Stable per-machine settings, stored for every mod in the per-user config file. The "
        "tracker is a bare git repository under any name, in any folder. Each option below does "
        "its own thing and exits; the app's Settings page writes the same file. Precedence for "
        "every setting: a command-line option, then the environment variable, then the config "
        f"file, then the built-in default. Settable: {', '.join(config.SETTABLE)}.")
    setgrp.add_argument("--config", action="store_true",
                        help="Show the settings in use, where each comes from, and the config "
                             "files read")
    setgrp.add_argument("--set", nargs=2, metavar=("KEY", "VALUE"), dest="set_value",
                        help="Store a setting in the per-user config file, for example "
                             "`--set vanilla_repo /path/to/my-tracker.git`")
    setgrp.add_argument("--unset", metavar="KEY", dest="unset_value",
                        help="Remove a setting from the per-user config file, so the setting "
                             "below it applies again")
    return ap


def _print_messages(messages):
    for m in messages:
        print(m, file=sys.stderr if m.startswith(("Warning:", "Note:")) else sys.stdout)


def _config_command(args):
    """Run one config option: --config, --set or --unset. Returns the exit code."""
    if args.config:
        print(config.render_config(config.config_view()), end="")
        return 0
    try:
        if args.set_value:
            messages = config.set_value(*args.set_value)
        else:
            messages = config.unset_value(args.unset_value)
    except config.ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    _print_messages(messages)
    return 0


def _show_dismissed(store):
    entries = store.state["dismissed"]
    if not entries:
        print(f"No dismissed findings for {store.mod_id}.")
        return
    print(f"Dismissed findings for {store.mod_id} ({len(entries)}):")
    for fid, e in sorted(entries.items(), key=lambda kv: (kv[1].get("finding", ""),
                                                           kv[1].get("name", ""))):
        detail = f" ({e['detail']})" if e.get("detail") else ""
        reason = f": {e['reason']}" if e.get("reason") else ""
        gone = "; no longer found" if e.get("gone") else ""
        print(f"  [{ledger.short_id(fid)}] {e.get('finding', '?')} "
              f"`{e.get('name', '?')}`{detail}, dismissed {e.get('on', '?')}{reason}{gone}")
    if any(e.get("gone") for e in entries.values()):
        print("A dismissal marked no longer found matched nothing in the last run: the finding changed or "
              "was fixed. `pdx-audit --undismiss <id>` removes it.")


def _open_app(mod_root, vanilla_repo, args, missing=None):
    try:
        from .app import launch
    except ImportError:
        print("Error: the desktop app needs PySide6, which is part of pdx-audit. This install is "
              "incomplete. Install pdx-audit again, or add PySide6 with "
              "`pipx inject pdx-audit PySide6`.", file=sys.stderr)
        return 1
    return launch(mod_root, vanilla_repo, {
        "missing_tracker": missing, "tracker_override": args.vanilla_repo,
        "audits": [a for a in ALL_AUDITS if getattr(args, a)], "full": args.full,
        "old": args.old, "new": args.new, "block": args.block, "category": args.category})


def main():
    with session.run():
        return _main()


def _main():
    args = build_parser().parse_args()

    if args.force and not args.remove_orphaned_records:
        _usage_error("--force only works together with --remove-orphaned-records")
    if args.reason and not args.dismiss:
        _usage_error("--reason only works together with --dismiss")
    config_commands = [c for c in CONFIG_COMMANDS if getattr(args, c)]
    if len(config_commands) > 1:
        _usage_error(f"{' and '.join('--' + c.split('_')[0] for c in config_commands)} cannot be "
                     f"combined; set one setting at a time")
    if config_commands:
        # A config command exits before a run, so anything else asked for would be
        # dropped without a word.
        others = ([f"--{c.replace('_', '-')}" for c in APP_COMMANDS if getattr(args, c)]
                  + [f"--{a}" for a in ALL_AUDITS if getattr(args, a)]
                  + [f"--{f}" for f in ("display", "summary", "diff", "full", "block",
                                        "category", "old", "new") if getattr(args, f)])
        if others:
            _usage_error(f"--{config_commands[0].split('_')[0]} cannot be combined with "
                         f"{', '.join(others)}; it does its own thing and exits")
    if args.full and args.old:
        _usage_error("--full and --old cannot be combined; --full starts from the oldest commit")
    if args.diff and args.summary:
        _usage_error("--diff and --summary cannot be combined; the diffs are part of the per-audit detail")
    if args.display:
        clash = [f"--{c.replace('_', '-')}" for c in APP_COMMANDS if getattr(args, c)]
        if clash:
            _usage_error(f"--display cannot be combined with {', '.join(clash)}; "
                         f"the app has a button for it")

    if color_enabled(args.color):
        sys.stdout = ColorWriter(sys.stdout)

    if config_commands:
        sys.exit(_config_command(args))

    if args.commit:
        repo = resolve_tracker_path(args.mod_root, args.vanilla_repo)
        do_commit(repo, args.commit, patch_name(args.patch_name), args.game_root)
        sys.exit(0)

    mod_root = find_mod_root(args.mod_root)
    store, store_err = open_store(mod_root)
    needs_store = (args.dismiss or args.undismiss or args.show_dismissed
                   or args.remove_orphaned_records or args.display)
    if store is None:
        if needs_store:
            print(f"Error: {store_err}", file=sys.stderr)
            sys.exit(1)
        print(f"Warning: {store_err}; findings records are disabled for this run.",
              file=sys.stderr)
    elif not args.remove_orphaned_records:
        note = orphan_note(store)
        if note:
            print(note, file=sys.stderr)

    if args.show_dismissed:
        _show_dismissed(store)
        sys.exit(0)
    if args.undismiss:
        removed, errors = ledger.undismiss(store.state, args.undismiss)
        for fid, e in removed:
            print(f"Restored [{ledger.short_id(fid)}] {e.get('finding', '?')} "
                  f"`{e.get('name', '?')}`")
        for err in errors:
            print(f"Error: {err}", file=sys.stderr)
        store.save()
        sys.exit(1 if errors else 0)
    if args.remove_orphaned_records:
        sys.exit(remove_orphaned_records(store, force=args.force))

    if args.display:
        # The app opens without a tracker, on its Settings page, so one can be chosen there.
        vanilla_repo, missing = locate_vanilla_repo(mod_root, args.vanilla_repo)
        sys.exit(_open_app(mod_root, vanilla_repo, args, missing))
    vanilla_repo = find_vanilla_repo(mod_root, args.vanilla_repo)
    commits = get_commits(vanilla_repo)
    if not commits:
        print("No commits in vanilla-tracker.", file=sys.stderr)
        sys.exit(1)

    stale_warning = warn_if_tracker_stale(vanilla_repo, commits[0][0])
    prune_cache(vanilla_repo)

    if args.list_commits:
        print("Available vanilla-tracker commits:")
        for h, msg in commits:
            print(f"  {h}  {msg}")
        sys.exit(0)

    if len(commits) < 2:
        print("Need at least 2 commits in vanilla-tracker.", file=sys.stderr)
        sys.exit(1)

    base = VanillaBase(vanilla_repo)

    def position(ref, side):
        """Returns the index in `commits` of a --old or --new version or commit.
        The newest commit is first."""
        from .tracker import git
        msg = resolve_ref(vanilla_repo, ref, commits, side)            # exits on no match
        full = git(vanilla_repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()
        for i, (h, m) in enumerate(commits):
            if (full and full.startswith(h)) or (msg and m == msg):
                return i
        print(f"Error: --{side} '{ref}' is not a tracked commit.", file=sys.stderr)
        sys.exit(1)

    new_i, old_i = 0, 1
    if args.full:
        old_i = len(commits) - 1
    if args.old:
        old_i = position(args.old, "old")
    if args.new:
        new_i = position(args.new, "new")
        if not (args.old or args.full):
            old_i = min(new_i + 1, len(commits) - 1)
    if old_i < new_i:
        _usage_error(f"--old ({_tag(commits[old_i][1])}) must be older than "
                     f"--new ({_tag(commits[new_i][1])})")
    if old_i == new_i:
        print("Warning: --old and --new are the same version; nothing can have changed.",
              file=sys.stderr)
    new_hash, new_msg = commits[new_i]
    old_hash, old_msg = commits[old_i]

    picked = [name for name in ALL_AUDITS if getattr(args, name)]
    selected = picked if picked else list(ALL_AUDITS)
    if args.category and any(s not in ("overrides", "dupes") for s in selected):
        _usage_error("--category only works with --overrides and/or --dupes")
    if args.block and "deps" in selected:
        selected.remove("deps")   # dependency names are not blocks
        if not selected:
            _usage_error("--block does not apply to the dependency audit; name another audit or leave --block out")

    order = [_tag(m) for _h, m in reversed(commits)]
    ctx = types.SimpleNamespace(
        commits=commits, new_tag=_tag(new_msg), base=base,
        fixed_window=bool(args.full or args.old or args.new),
        bases=ledger.bases_from_state(store.state, order) if store else {},
        scanned={},
        dismissed=set(store.state["dismissed"]) if store else set(),
    )

    # The mod's files as this run found them, so the app can tell when they change.
    files_at_start = mod_fingerprint(mod_root) if args.results_file else None

    runners = {
        "overrides": lambda: run_override_audit(
            mod_root, base, old_hash, old_msg, new_hash, new_msg, args, ctx),
        "deps": lambda: run_deps(
            mod_root, base, old_hash, old_msg, new_hash, new_msg, ctx),
        "gui": lambda: run_gui_audit(
            mod_root, base, old_hash, old_msg, new_hash, new_msg, args, ctx),
        "files": lambda: run_file_audit(
            mod_root, base, old_hash, old_msg, new_hash, new_msg, args, ctx),
        "loc": lambda: run_loc_audit(
            mod_root, base, old_hash, old_msg, new_hash, new_msg, args, ctx),
        "dupes": lambda: run_dupes_audit(
            mod_root, base, new_hash, new_msg, args, ctx),
    }
    # Run each audit with its detailed output captured, so the cross-audit
    # triage summary can be assembled from every audit's findings and printed
    # first, above the detail. Progress still streams live to stderr.
    results = []  # (name, detail_text, findings)
    for name in selected:
        buf = io.StringIO()
        with redirect_stdout(buf):
            findings = runners[name]() or []
        results.append((name, buf.getvalue(), findings))

    if (args.block or args.category) and not any(ctx.scanned.get(s) for s in selected):
        what = " and ".join(f"--{flag} {getattr(args, flag)}"
                            for flag in ("block", "category") if getattr(args, flag))
        print(f"Error: {what} matched nothing in the selected audits "
              f"({', '.join(selected)}).", file=sys.stderr)
        sys.exit(2)

    # The copy comparisons already gave their findings distinct ids; this covers the rest.
    all_findings = ledger.distinct([f for _, _, fs in results for f in fs])

    if args.dismiss:
        done, errors = ledger.dismiss(store.state, all_findings, args.dismiss, args.reason,
                                      datetime.date.today().isoformat())
        for fid, f in done:
            print(f"Dismissed [{ledger.short_id(fid)}] {f.kind} `{f.name}`"
                  + (f" ({f.detail})" if f.detail else ""))
        for err in errors:
            print(f"Error: {err}", file=sys.stderr)
        if done:
            store.save()
        sys.exit(1 if errors else 0)

    visible, hidden = (ledger.split_dismissed(all_findings, store.state)
                       if store else (all_findings, 0))
    default_window = not (ctx.fixed_window or args.block or args.category)
    if store and default_window:
        # A run of some audits updates only those audits' open findings.
        ran = set(selected)

        def covers(entry):
            audit = ledger.audit_of(entry)
            return audit in ran or (audit not in ALL_AUDITS and set(ALL_AUDITS) <= ran)
        ledger.update_open(store.state, visible, ctx.new_tag, covers,
                           produced={ledger.finding_id(f) for f in all_findings})
        store.save()

    history_old = None if args.old else commits[-1][1]
    triage = render_triage(visible, old_msg, new_msg, selected,
                           detail_shown=not args.summary, new_tag=ctx.new_tag,
                           dismissed=hidden, history_old=history_old)
    print(triage)
    if args.results_file:
        payload = build_payload(
            visible, mod_name=mod_root.name, old_msg=old_msg, new_msg=new_msg,
            new_tag=ctx.new_tag, selected=selected, dismissed=hidden, triage=triage,
            details=[(name, detail) for name, detail, _ in results],
            warnings=[stale_warning] if stale_warning else [],
            window=window_heading(old_msg, new_msg, selected, history_old)[0])
        payload["files"] = files_at_start
        with open(args.results_file, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
    elif not args.summary:
        for name, detail, _ in results:
            if detail.strip():
                print("\n")
                sys.stdout.write(detail)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
