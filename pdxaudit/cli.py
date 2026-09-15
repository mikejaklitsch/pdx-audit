"""Diff mod overrides and referenced names against vanilla patch changes.

Five audits (name one or more to run just those, or none to run all five), all
driven by the vanilla-tracker bare git repo:

  Override audit (--overrides): finds every INJECT:, REPLACE:, TRY_ and _OR_CREATE
  directive in the mod and compares each REPLACE with vanilla's
  tracked versions of its block, so a change vanilla made that your copy lacks is
  told apart from your own edits and from a change that meets one of them.

  Dependency audit (--deps): flags names the mod uses (keys it writes and names
  it references) that vanilla used at some tracked version but no longer uses,
  with the patch that dropped them.

  GUI audit (--gui): finds mod .gui templates/types that shadow vanilla's and
  mod .gui files that replace a vanilla file, and compares each copy with
  vanilla's tracked versions the same way.

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

from . import ledger, session
from .dupes import run_dupes_audit
from .gui import run_gui_audit
from .loc import run_loc_audit
from .overrides import run_deps_audit, run_override_audit
from .report import ColorWriter, color_enabled, render_triage, window_heading
from .results import build_payload, mod_fingerprint
from .store import open_store, orphan_note, remove_orphaned_records
from .tracker import do_snapshot, find_mod_root, find_vanilla_repo, get_commits, prune_cache, resolve_ref, resolve_tracker_path, warn_if_tracker_stale

ALL_AUDITS = ["overrides", "deps", "gui", "loc", "dupes"]
# Commands on a mod's sources; each matches one action in the app's sources panel.
SOURCE_COMMANDS = ("sources", "add_source", "remove_source", "relocate_source", "move_source",
                   "set_kind", "rename", "unrename", "ignore_suggestion", "remove_orphaned_sources",
                   "snapshot_source", "patch")
# Commands that do their own thing and exit; the --display app has a button for each.
APP_COMMANDS = ("dismiss", "undismiss", "show_dismissed", "remove_orphaned_records",
                "snapshot", "list_commits", "results_file") + SOURCE_COMMANDS


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
                         "with buttons (needs PySide6)")
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
    ap.add_argument("--loc", action="store_true",
                    help="Localization audit: loc keys the mod overrides "
                         "whose vanilla value changed or was removed")
    ap.add_argument("--dupes", action="store_true",
                    help="Duplicate audit: one source of truth per definition")
    ap.add_argument("--full", action="store_true",
                    help="Compare every audit from the oldest tracked snapshot to new")
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
                    help="Old vanilla version tag or commit (default: the oldest snapshot for "
                         "the override and GUI audits, the snapshot before --new for the others)")
    ap.add_argument("--new",
                    help="New vanilla version tag or commit (default: most-recent)")
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
    # The source options sit in their own group, below the main options, which stay as they were.
    ap.usage = ap.format_usage()[len("usage: "):].strip().replace(ap.prog, "%(prog)s", 1)
    src = ap.add_argument_group(
        "sources",
        "Foundations are dependencies that load before the mod, in the order you store; a run "
        "compares the mod against vanilla and its foundations together. Adopted sources are mods "
        "whose code the mod absorbed. Choices are stored per mod in the per-user data folder. "
        "--force also skips the confirmation of --remove-orphaned-sources.")
    src.add_argument("--sources", action="store_true",
                     help="List the mod's sources with their versions, patches and freshness, then "
                          "suggested sources and orphaned sources")
    src.add_argument("--add-source", metavar="FOLDER",
                     help="Choose a folder as a source of the mod (with --as)")
    src.add_argument("--as", dest="as_role", choices=("foundation", "adopted"),
                     help="With --add-source, what the folder is to the mod")
    src.add_argument("--kind", choices=("git", "folder"),
                     help="With --add-source, read the folder as a git repository or as a folder "
                          "(default: detected)")
    src.add_argument("--replace", action="store_true",
                     help="With --add-source, replace a chosen source with the same id")
    src.add_argument("--remove-source", metavar="ID", help="Stop using a source for the mod")
    src.add_argument("--relocate-source", nargs=2, metavar=("ID", "FOLDER"),
                     help="Point a source at the folder it moved to, keeping its snapshots and patches")
    src.add_argument("--move-source", nargs=2, metavar=("ID", "POSITION"),
                     help="Move a foundation to a position in the load order (1 loads first)")
    src.add_argument("--set-kind", nargs=2, metavar=("ID", "KIND"),
                     help="Read a source as git, folder, or auto (detected)")
    src.add_argument("--rename", nargs=3, metavar=("ID", "FROM", "TO"),
                     help="For an adopted source, read a name or file name part FROM on its side as TO")
    src.add_argument("--unrename", nargs=2, metavar=("ID", "FROM"),
                     help="Remove an adopted source's rename rule")
    src.add_argument("--ignore-suggestion", metavar="ID",
                     help="Stop suggesting sources with this id until the mod's declared "
                          "dependencies change")
    src.add_argument("--remove-orphaned-sources", action="store_true",
                     help="Remove stored sources no mod chooses (asks for confirmation)")
    src.add_argument("--snapshot-source", metavar="ID",
                     help="Record a folder source's current files as a new version")
    src.add_argument("--patch", nargs=3, metavar=("ID", "VERSIONS", "PATCH"),
                     help="Assign the vanilla patch a source version, or a run of versions "
                          "(a..b), belongs to")
    src.add_argument("--vanilla-only", action="store_true",
                     help="Compare the mod against vanilla alone, leaving its foundations out")
    src.add_argument("--adopted", metavar="ID",
                     help="Compare the mod against an adopted source instead of vanilla")
    src.add_argument("--source-version", nargs=2, action="append", metavar=("ID", "VERSION"),
                     help="Compare against this version of a source (default: its newest)")
    return ap


def _prepare_sources(mod_root, vanilla_repo, stale):
    """Before a run: warn about chosen sources whose folder is gone or that changed
    after their newest recorded version, record new git versions, and prune each
    source's cache. Returns (warnings, the mod's ModSources or None); the warnings
    are also printed to stderr."""
    from . import sources
    try:
        chosen = sources.load_sources(mod_root)
    except sources.SourceError:
        return [], None
    warnings = chosen.warnings()
    for s in chosen.foundations + chosen.adopted:
        warnings += [f"Warning: {m}" for m in sources.freshness(s)]
        if s.kind == sources.GIT:
            warnings += sources.record_versions(s, vanilla_repo, stale)
        prune_cache(s)
    for w in warnings:
        print(w, file=sys.stderr)
    return warnings, chosen


def _choose_base(args, vanilla_repo, chosen):
    """(base, warnings) for a run: the stack of the mod's foundations, or vanilla alone
    with --vanilla-only or when the mod has no foundation."""
    from .base import StackBase, VanillaBase
    from .sources import SourceError
    limits = dict(args.source_version or [])
    for sid in limits:
        if chosen is None or chosen.by_id(sid) is None:
            print(f"Error: --source-version {sid}: {sid} is not a chosen source of this mod.", file=sys.stderr)
            sys.exit(1)
        if args.vanilla_only and chosen.by_id(sid) in chosen.foundations:
            _usage_error(f"--source-version {sid} names a foundation, which --vanilla-only leaves out")
    if chosen is None or args.vanilla_only or not chosen.foundations:
        return VanillaBase(vanilla_repo), []
    warnings = [f"Warning: foundation {s.id} has no version with a patch, so no point of the stack includes "
                f"it. Assign patches with `pdx-audit --patch {s.id} <version>[..<version>] <patch>`."
                for s in chosen.foundations if not any(p for _c, _t, p in s.versions())]
    try:
        base = StackBase(vanilla_repo, chosen.foundations,
                         {k: v for k, v in limits.items() if chosen.by_id(k) in chosen.foundations})
    except SourceError as e:
        _usage_error(str(e))
    base.prune()
    for w in warnings:
        print(w, file=sys.stderr)
    return base, warnings


def _print_messages(messages):
    for m in messages:
        print(m, file=sys.stderr if m.startswith(("Warning:", "Note:")) else sys.stdout)


def _source_command(args, mod_root):
    """Run one --sources option. Returns the exit code."""
    from . import sources
    vanilla_repo = find_vanilla_repo(mod_root, args.vanilla_repo)
    stale = None

    def lagging():
        nonlocal stale
        if stale is None:
            commits = get_commits(vanilla_repo)
            stale = bool(commits and warn_if_tracker_stale(vanilla_repo, commits[0][0]))
        return stale

    try:
        if args.remove_orphaned_sources:
            return sources.remove_orphaned_sources(force=args.force)
        if args.sources:
            chosen = sources.load_sources(mod_root)
            messages = chosen.warnings()
            for s in chosen.foundations + chosen.adopted:
                if s.kind == sources.GIT:
                    messages += sources.record_versions(s, vanilla_repo, lagging())
            _print_messages(messages)
            print(sources.render_sources(sources.sources_view(mod_root, vanilla_repo, full=True)), end="")
            return 0
        if args.add_source:
            messages = sources.add_source(mod_root, args.add_source, args.as_role, args.kind, args.replace,
                                          vanilla_repo, lagging())
        elif args.remove_source:
            messages = sources.remove_source(mod_root, args.remove_source)
        elif args.relocate_source:
            messages = sources.relocate_source(mod_root, *args.relocate_source)
        elif args.move_source:
            messages = sources.move_source(mod_root, *args.move_source)
        elif args.set_kind:
            messages = sources.set_kind(mod_root, *args.set_kind)
        elif args.rename:
            messages = sources.add_rename(mod_root, *args.rename)
        elif args.unrename:
            messages = sources.remove_rename(mod_root, *args.unrename)
        elif args.ignore_suggestion:
            messages = sources.ignore_suggestion(mod_root, args.ignore_suggestion)
        elif args.snapshot_source:
            messages = sources.snapshot_source(mod_root, args.snapshot_source, vanilla_repo, lagging())
        else:
            messages = sources.assign_patch(mod_root, args.patch[0], args.patch[1], args.patch[2], vanilla_repo)
    except sources.SourceError as e:
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


def _open_app(mod_root, vanilla_repo, args):
    try:
        from .app import launch
    except ImportError:
        print('Error: --display needs PySide6. Install it with `pip install "pdx-audit[app]"`, '
              "or `pipx inject pdx-audit PySide6` for a pipx install.", file=sys.stderr)
        return 1
    return launch(mod_root, vanilla_repo, {
        "audits": [a for a in ALL_AUDITS if getattr(args, a)], "full": args.full,
        "old": args.old, "new": args.new, "block": args.block, "category": args.category,
        "base": "vanilla" if args.vanilla_only else (f"adopted:{args.adopted}" if args.adopted else "")})


def main():
    with session.run():
        return _main()


def _main():
    args = build_parser().parse_args()

    if args.force and not (args.remove_orphaned_records or args.remove_orphaned_sources):
        _usage_error("--force only works together with --remove-orphaned-records or "
                     "--remove-orphaned-sources")
    if args.reason and not args.dismiss:
        _usage_error("--reason only works together with --dismiss")
    if bool(args.add_source) != bool(args.as_role):
        _usage_error("--add-source and --as go together")
    for flag in ("kind", "replace"):
        if getattr(args, flag) and not args.add_source:
            _usage_error(f"--{flag} only works together with --add-source")
    commands = [c for c in SOURCE_COMMANDS if getattr(args, c)]
    if len(commands) > 1:
        _usage_error(f"{' and '.join('--' + c.replace('_', '-') for c in commands)} cannot be combined")
    if args.vanilla_only and args.adopted:
        _usage_error("--vanilla-only and --adopted cannot be combined")
    if args.full and args.old:
        _usage_error("--full and --old cannot be combined; --full starts from the oldest snapshot")
    if args.diff and args.summary:
        _usage_error("--diff and --summary cannot be combined; the diffs are part of the per-audit detail")
    if args.display:
        clash = [f"--{c.replace('_', '-')}" for c in APP_COMMANDS if getattr(args, c)]
        if clash:
            _usage_error(f"--display cannot be combined with {', '.join(clash)}; "
                         f"the app has a button for it")

    if color_enabled(args.color):
        sys.stdout = ColorWriter(sys.stdout)

    if args.snapshot:
        repo = resolve_tracker_path(args.mod_root, args.vanilla_repo)
        do_snapshot(repo, args.snapshot, args.patch_name, args.game_root)
        sys.exit(0)

    mod_root = find_mod_root(args.mod_root)
    if commands:
        sys.exit(_source_command(args, mod_root))
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
    if not args.display:
        from .sources import orphan_sources_note, suggestion_note
        for note in (orphan_sources_note(), suggestion_note(mod_root)):
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

    vanilla_repo = find_vanilla_repo(mod_root, args.vanilla_repo)
    if args.display:
        sys.exit(_open_app(mod_root, vanilla_repo, args))
    commits = get_commits(vanilla_repo)
    if not commits:
        print("No commits in vanilla-tracker.", file=sys.stderr)
        sys.exit(1)

    stale_warning = warn_if_tracker_stale(vanilla_repo, commits[0][0])
    prune_cache(vanilla_repo)
    source_warnings, chosen = _prepare_sources(mod_root, vanilla_repo, bool(stale_warning))

    if args.list_commits:
        print("Available vanilla-tracker commits:")
        for h, msg in commits:
            print(f"  {h}  {msg}")
        sys.exit(0)

    if len(commits) < 2:
        print("Need at least 2 commits in vanilla-tracker.", file=sys.stderr)
        sys.exit(1)

    base, base_warnings = _choose_base(args, vanilla_repo, chosen)
    source_warnings += base_warnings
    vanilla_commits, commits = commits, base.commits()
    tag_of_vanilla = lambda point: base.by_id[point].vanilla_tag if base.stacked else None

    def position(ref, side):
        """Index in `commits` (newest first) of a --old/--new version or commit. In a
        stack, --new reaches the newest point of that vanilla version."""
        from .tracker import git
        if base.stacked:
            hit = next((i for i, (_h, m) in enumerate(commits) if m.tag == ref), None)
            if hit is not None and commits[hit][1].tag != tag_of_vanilla(commits[hit][0]):
                return hit                                            # a foundation point's tag
        msg = resolve_ref(vanilla_repo, ref, vanilla_commits, side)   # exits on no match
        full = git(vanilla_repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()
        for i, (h, m) in enumerate(commits):
            if (full and full.startswith(h)) or (msg and m == msg):
                if base.stacked and side == "new":
                    vanilla = base.vanilla_commit(h)
                    while i > 0 and base.vanilla_commit(commits[i - 1][0]) == vanilla:
                        i -= 1
                return i
        print(f"Error: --{side} '{ref}' is not a tracked snapshot.", file=sys.stderr)
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
    adopted = []
    if args.adopted:
        src = chosen.by_id(args.adopted) if chosen else None
        if src is None or src not in chosen.adopted:
            print(f"Error: --adopted {args.adopted}: not an adopted source of this mod with its folder in place.",
                  file=sys.stderr)
            sys.exit(1)
        adopted, selected = [src], []
    elif chosen and chosen.adopted and not (args.vanilla_only or args.block or args.category or picked):
        adopted = list(chosen.adopted)

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
        "deps": lambda: run_deps_audit(
            mod_root, base, old_hash, old_msg, new_hash, new_msg, ctx),
        "gui": lambda: run_gui_audit(
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
    remedies = {}
    if adopted:
        from .adopt import run_adopted_audit
        prior = [f for _, _, fs in results for f in fs]
        buf = io.StringIO()
        with redirect_stdout(buf):
            findings, remedies = run_adopted_audit(mod_root, vanilla_repo, adopted, chosen.foundations,
                                                   args, ctx, prior)
        results.append(("adopted", buf.getvalue(), findings))
        selected = selected + ["adopted"]

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
    default_window = not (ctx.fixed_window or args.block or args.category or args.vanilla_only or args.adopted)
    if store and default_window:
        # A run of some audits updates only those audits' open findings.
        ran, adopted_here = set(selected), {s.id for s in chosen.adopted} if chosen else set()

        def covers(entry):
            audit = ledger.audit_of(entry, adopted_here)
            return audit in ran or (audit not in ALL_AUDITS + ["adopted"] and set(ALL_AUDITS) <= ran)
        ledger.update_open(store.state, visible, ctx.new_tag, covers,
                           produced={ledger.finding_id(f) for f in all_findings})
        store.save()

    # Every point placed on the newest vanilla version is this patch; findings measured
    # against an adopted source are worded and grouped as that source's.
    newest_vanilla = base.vanilla_commit(new_hash)
    patch_tags = {_tag(m) for h, m in commits if base.vanilla_commit(h) == newest_vanilla}
    adopted_ids = {s.id for s in chosen.adopted} if chosen else set()
    title = None
    if args.adopted:
        versions = adopted[0].versions()
        title = f"compared with {adopted[0].id}" + (f" {versions[-1][1]}" if versions else "")
    history_old = None if args.old else commits[-1][1]
    triage = render_triage(visible, old_msg, new_msg, selected,
                           detail_shown=not args.summary, new_tag=ctx.new_tag,
                           dismissed=hidden, remedies=remedies, patch_tags=patch_tags,
                           adopted=adopted_ids, title=title,
                           history_old=history_old)
    print(triage)
    if args.results_file:
        payload = build_payload(
            visible, mod_name=mod_root.name, old_msg=old_msg, new_msg=new_msg,
            new_tag=ctx.new_tag, selected=selected, dismissed=hidden, triage=triage,
            details=[(name, detail) for name, detail, _ in results],
            warnings=([stale_warning] if stale_warning else []) + source_warnings,
            patch_tags=patch_tags, adopted=adopted_ids,
            window=window_heading(old_msg, new_msg, selected, history_old, title)[0])
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
