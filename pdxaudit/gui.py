"""GUI override audit: mod templates and types that shadow vanilla's, and mod .gui
files that replace vanilla's file at the same path, each compared with vanilla's
tracked versions (see diff3)."""

import re
import sys
import tarfile
import io
import json
import hashlib
from pathlib import Path

from . import changes, diff3, session
from .report import Finding
from .tracker import MODULE_ROOTS, _git_archive, cache_path, full_hash, tag_of
from .config import should_skip

# The engine reads these keywords in any case: vanilla 1.4 hud_topbar.gui opens
# with `Types HUD_TopbarTypes`.
GUI_DEF_HEAD = re.compile(r"^\s*(template|local_template|types)\s+([A-Za-z_][\w.]*)", re.I)

GUI_TYPE_HEAD = re.compile(r"^\s*type\s+([A-Za-z_][\w.]*)\s*=", re.I)

def _gui_code(line):
    """Code portion of a .gui line: comment stripped, string contents blanked
    (quotes kept), so braces inside comments and strings never count."""
    out = []
    in_str = False
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
                out.append('"')
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append('"')
        elif ch == "#":
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out)

def parse_gui_defs(text):
    """Extract template/local_template/types/type definitions from .gui content.
    Line-granular: headers sharing a line with a second definition are missed,
    and a definition's text may include a trailing brace that closed its parent
    on the same line; both are stable across commits, so comparisons hold.
    Returns (defs, clean). clean=False flags brace anomalies vanilla actually
    ships: a stray extra closer (engine-tolerated; accounting resyncs at 0) or
    a block left open at EOF (its definition is dropped). Emitted defs closed
    properly and are trustworthy either way."""
    lines = text.split("\n")
    defs = []
    depth = 0
    clean = True
    open_defs = []  # {kind, name, start, base, opened}
    for i, raw in enumerate(lines):
        code = _gui_code(raw)
        if depth == 0:
            m = GUI_DEF_HEAD.match(code)
            if m:
                # a still-braceless def at depth 0 was malformed; drop it
                open_defs = [d for d in open_defs if d["opened"]]
                open_defs.append({"kind": m.group(1).lower(), "name": m.group(2),
                                  "start": i, "base": 0, "opened": False})
        elif depth == 1 and open_defs and open_defs[0]["kind"] == "types":
            m = GUI_TYPE_HEAD.match(code)
            if m:
                open_defs.append({"kind": "type", "name": m.group(1),
                                  "start": i, "base": 1, "opened": False})
        o, c = code.count("{"), code.count("}")
        if o:
            for d in open_defs:
                d["opened"] = True
        depth += o - c
        while open_defs and open_defs[-1]["opened"] and depth <= open_defs[-1]["base"]:
            d = open_defs.pop()
            defs.append({"kind": d["kind"], "name": d["name"], "line": d["start"] + 1,
                         "text": "\n".join(lines[d["start"]:i + 1])})
        if depth < 0:
            depth = 0
            clean = False
    if depth != 0 or any(d["opened"] for d in open_defs):
        clean = False
    return defs, clean

def _top_defs(text):
    """(kind, name, first line, last line) of each top-level definition in .gui `text`;
    a `type` inside a `types` group is not top-level."""
    defs = parse_gui_defs(text)[0]
    span = lambda d: (d["line"], d["line"] + d["text"].count("\n"))
    groups = [span(d) for d in defs if d["kind"] == "types"]
    return [(d["kind"], d["name"], *span(d)) for d in defs
            if not (d["kind"] == "type" and any(lo < d["line"] <= hi for lo, hi in groups))]


def _without(text, names):
    """`text` with its top-level definitions named in `names` ({(kind, name)}) blanked
    out line for line, so the lines left keep their numbers."""
    if text is None or not names:
        return text
    lines = text.split("\n")
    for kind, name, first, last in _top_defs(text):
        if (kind, name) in names:
            lines[first - 1:last] = [""] * (last - first + 1)
    return "\n".join(lines)


def mod_gui_files(mod_root):
    """[(rel_path, text), ...] for the mod's .gui files under <module>/gui/."""
    out = []
    for fp in session.mod_paths(mod_root, ".gui"):
        rel = fp.relative_to(mod_root)
        if (len(rel.parts) < 3 or rel.parts[0] not in MODULE_ROOTS
                or rel.parts[1] != "gui" or should_skip(rel)):
            continue
        try:
            out.append((rel.as_posix(), session.read_text(fp)))
        except Exception:
            continue
    return out

def build_gui_vanilla(vanilla_repo, commit, modules, label=""):
    """Parse all vanilla .gui under the given modules' gui/ dirs at `commit`.
    Returns (def_idx, file_idx, bad):
      def_idx: (module, kind, name) -> (vfile, text) for template/type;
                 on duplicate names the first file in path-sorted order wins,
                 approximating first-loaded-wins
      file_idx: vfile -> full text (for same-path replacement checks)
      bad: vfiles with brace anomalies (defs still indexed best-effort)"""
    dirs = [f"{m}/gui" for m in sorted(modules)]
    if label:
        print(f"  {label}: extracting {len(dirs)} gui directories...",
              end="", file=sys.stderr, flush=True)
    def_idx, file_idx, bad = {}, {}, []
    raw = _git_archive(vanilla_repo, commit, dirs)
    if not raw:
        if label:
            print(" failed!", file=sys.stderr)
        return def_idx, file_idx, bad
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), ignore_zeros=True) as tf:
            members = [m for m in tf.getmembers()
                       if m.isfile() and m.name.endswith(".gui")]
            for member in sorted(members, key=lambda m: m.name.lower()):
                f = tf.extractfile(member)
                if f is None:
                    continue
                content = f.read().decode("utf-8-sig", errors="replace")
                vfile = member.name
                file_idx[vfile] = content
                defs, clean = parse_gui_defs(content)
                if not clean:
                    bad.append(vfile)
                module = vfile.split("/", 1)[0]
                for d in defs:
                    if d["kind"] not in ("template", "type"):
                        continue
                    key = (module, d["kind"], d["name"])
                    if key not in def_idx:
                        def_idx[key] = (vfile, d["text"])
    except tarfile.TarError:
        if label:
            print(" tar error!", file=sys.stderr)
        return def_idx, file_idx, bad
    if label:
        print(f" {len(def_idx)} defs in {len(file_idx)} files.", file=sys.stderr)
    return def_idx, file_idx, bad

GUI_CACHE_VERSION = 2

def _gui_cache_path(vanilla_repo, commit, modules):
    """Cache file for a commit's parsed GUI index, keyed by the full commit hash
    and the module set. Commit content is immutable, so entries never go stale;
    the version bumps when the parser or index shape changes."""
    full = full_hash(vanilla_repo, commit)
    if not full:
        return None
    mod_key = hashlib.sha1(",".join(sorted(modules)).encode()).hexdigest()[:12]
    return cache_path(vanilla_repo, f"gui-v{GUI_CACHE_VERSION}-{full}-{mod_key}.json")

def build_gui_vanilla_cached(vanilla_repo, commit, modules, label=""):
    """Does the same as build_gui_vanilla, and also keeps the result on disk.

    The audit reads the GUI index at each commit in its window. This function
    writes each index into the cache folder of the tracker. The name of the cache
    file contains the full hash of the commit."""
    cache = _gui_cache_path(vanilla_repo, commit, modules)
    if cache and cache.is_file():
        try:
            data = json.loads(cache.read_text())
            def_idx = {tuple(k): (v[0], v[1]) for k, v in data["defs"]}
            if label:
                print(f"  {label}: gui index from cache "
                      f"({len(def_idx)} defs).", file=sys.stderr)
            return def_idx, data["files"], data["bad"]
        except (OSError, ValueError, KeyError, IndexError):
            pass
    def_idx, file_idx, bad = build_gui_vanilla(vanilla_repo, commit, modules, label)
    if cache:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "defs": [[list(k), [v[0], v[1]]] for k, v in def_idx.items()],
                "files": file_idx,
                "bad": bad,
            }
            cache.write_text(json.dumps(payload))
        except OSError:
            pass
    return def_idx, file_idx, bad

def _tag(msg):
    return tag_of(msg)

def gui_def_target(key):
    """Record target name for a shadowed GUI definition (module, kind, name)."""
    module, kind, name = key
    return f"gui:{module}/{kind}/{name}"

def gui_file_target(rel):
    """Record target name for a same-path .gui file replacement."""
    return f"guifile:{rel}"

def commits_up_to(commits, new_hash):
    """The newest-first commit list trimmed so `new_hash` is its first entry."""
    for i, (h, _msg) in enumerate(commits):
        if new_hash.startswith(h) or h.startswith(new_hash):
            return commits[i:]
    return commits

def version_window(commits, new_hash, old_hash=None):
    """The (hash, message) pairs from old_hash through new_hash, oldest first; from
    the oldest tracked commit when old_hash is None."""
    newest_first = commits_up_to(commits, new_hash)
    if old_hash:
        for i, (h, _msg) in enumerate(newest_first):
            if old_hash.startswith(h) or h.startswith(old_hash):
                return list(reversed(newest_first[:i + 1]))
    return list(reversed(newest_first))

def audit_window(base, old_hash, new_hash, args, ctx=None):
    """The versions the GUI and override audits compare a copy with: from --old,
    else the oldest commit, through the new version."""
    from .base import as_base
    commits = ctx.commits if ctx is not None else as_base(base).commits()
    return version_window(commits, new_hash, old_hash if getattr(args, "old", None) else None)


def _mod_defs(files):
    mdefs, skipped = [], 0
    for rel, text in files:
        defs, clean = parse_gui_defs(text)
        if not clean:
            print(f"Warning: unbalanced braces in mod file {rel}; "
                  f"definitions parsed best-effort", file=sys.stderr)
        module = rel.split("/", 1)[0]
        for d in defs:
            if d["kind"] in ("template", "type"):
                d["file"] = rel
                d["module"] = module
                mdefs.append(d)
            else:
                skipped += 1
    return mdefs, skipped

def run_gui_audit(mod_root, base, old_hash, old_msg, new_hash, new_msg, args, ctx=None):
    """Compare each mod GUI definition that shadows the base's, and each mod .gui file
    at a base file's path, with the base's versions from the window's start through
    the new version. `base` is a Base or the vanilla tracker's path."""
    from .base import as_base
    base = as_base(base)
    files = mod_gui_files(mod_root)
    if not files:
        print("No mod .gui files found.", file=sys.stderr)
        if ctx is not None:
            ctx.scanned["gui"] = 0
        return []

    mdefs, skipped = _mod_defs(files)
    files_audited = files
    if args.block:
        mdefs = [d for d in mdefs if d["name"] == args.block]
        files_audited = []
    only = getattr(ctx, "only_files", None)          # an intent check of some files
    if only is not None:
        mdefs = [d for d in mdefs if d["file"] in only]
        files_audited = [(rel, text) for rel, text in files_audited if rel in only]
    if ctx is not None:
        ctx.scanned["gui"] = len(mdefs) + len(files_audited)

    modules = sorted({rel.split("/", 1)[0] for rel, _ in files})
    window = audit_window(base, old_hash, new_hash, args, ctx)
    tags = [_tag(m) for _h, m in window]
    print(f"Scanning {len(mdefs)} GUI definitions in {len(files)} mod .gui files "
          f"against {len(window)} vanilla versions...", file=sys.stderr)
    indexes = [base.gui_index(h, modules, f"version {i + 1}/{len(window)} ({h[:7]})")
               for i, (h, _m) in enumerate(window)]
    new_defs, new_files, _bad = indexes[-1]
    if not new_defs and not new_files:
        print("Could not read vanilla .gui files (archive failed).", file=sys.stderr)
        sys.exit(1)
    bad_files = sorted({vf for _d, _f, bad in indexes for vf in bad})
    if bad_files:
        print(f"Warning: {len(bad_files)} vanilla .gui file(s) have unbalanced "
              f"braces (vanilla's own); parsed best-effort:", file=sys.stderr)
        for vf in bad_files:
            print(f"  {vf}", file=sys.stderr)

    want = bool(getattr(args, "results_file", None))
    dismissed = getattr(ctx, "dismissed", frozenset())
    same_path = {rel for rel, _ in files if any(rel in fi for _d, fi, _b in indexes)}

    from .registry import Generated
    generated = Generated(mod_root, ctx)
    shadowed, new_coll, van_removed = [], [], []
    current = mod_only = 0
    for d in mdefs:
        if d["file"] in same_path:
            continue
        key = (d["module"], d["kind"], d["name"])
        entries = [di.get(key) for di, _f, _b in indexes]
        is_gen, tool = generated.check(d["file"])
        if is_gen:
            generated.add(d["file"], tool, [e and e[1] for e in entries], tags)
            continue
        if not any(entries):
            mod_only += 1
            continue
        if entries[-1] is None:
            last = max(i for i, e in enumerate(entries) if e)
            van_removed.append((d, entries[last][0], tags[last + 1]))
            continue
        if len(entries) > 1 and entries[-2] is None:
            new_coll.append((d, entries[-1][0], tags[-1]))
        versions = [e[1] if e else None for e in entries]
        result = changes.audit("gui", d["name"], gui_def_target(key), d["text"], versions, tags,
                               d["file"], d["line"], want=want, vanilla_file=entries[-1][0],
                               dialect=diff3.GUI)
        if result.flagged:
            shadowed.append((d, entries[-1][0], versions, result))
        else:
            current += 1

    # Each mod file's top-level definitions: a definition a replaced file's copy lacks
    # but another mod file of its module defines was moved there, and is compared there.
    made = {rel: {(kind, name) for kind, name, _first, _last in _top_defs(text)} for rel, text in files}
    replaced, file_review = [], []
    for rel, text in files_audited:
        versions = [fi.get(rel) for _d, fi, _b in indexes]
        is_gen, tool = generated.check(rel)
        if is_gen:
            generated.add(rel, tool, versions, tags)
            continue
        present = [v is not None for v in versions]
        if not any(present):
            continue
        if versions[-1] is None:
            last = max(i for i, p in enumerate(present) if p)
            file_review.append((rel, "removed", tags[last + 1]))
            continue
        if len(present) > 1 and not present[-2]:
            file_review.append((rel, "added", tags[-1]))
        module = rel.split("/", 1)[0]
        moved = {name for other, names in made.items() if other != rel and other.split("/", 1)[0] == module
                 for name in names} - made[rel]
        versions = [_without(v, moved) for v in versions]
        result = changes.audit("gui", rel, gui_file_target(rel), text, versions, tags, rel, 1,
                               want=want, vanilla_file=rel, dialect=diff3.GUI)
        if result.flagged:
            replaced.append((rel, versions, result))
        else:
            current += 1

    fixed = iter(changes.distinct([item[-1] for item in shadowed + replaced]))
    shadowed = [(*item[:-1], next(fixed)) for item in shadowed]
    replaced = [(*item[:-1], next(fixed)) for item in replaced]

    print(f"# GUI Override Audit: {tags[0]} → {tags[-1]}")
    print(f"*each copy compared with vanilla's {len(window)} tracked versions up to {new_msg}*")
    print()
    shadow_defs = [d for d in mdefs if d["file"] not in same_path]
    n_tmpl = sum(1 for d in shadow_defs if d["kind"] == "template")
    extras = []
    if len(mdefs) > len(shadow_defs):
        extras.append(f"{len(mdefs) - len(shadow_defs)} in same-path files, audited with their file")
    if skipped:
        extras.append(f"{skipped} file-scoped skipped")
    n_changes = lambda items: sum(len(item[-1].flagged) for item in items)
    print("\n".join([
        f"**{len(shadow_defs)}** shadow-capable definitions ({n_tmpl} template, "
        f"{len(shadow_defs) - n_tmpl} type) in **{len(files)}** mod .gui files"
        + ("; " + "; ".join(extras) if extras else ""),
        f"- **{len(shadowed)}** shadowed definitions with vanilla changes to take or check "
        f"({n_changes(shadowed)} changes)",
        f"- **{len(replaced)}** same-path file replacements with vanilla changes to take or check "
        f"({n_changes(replaced)} changes)",
        f"- **{len(file_review)}** same-path files vanilla added or removed",
        f"- **{len(new_coll)}** new name collisions (vanilla added a same-name definition)",
        f"- **{len(van_removed)}** shadowed definitions removed from vanilla",
        f"- **{current}** copies current with vanilla, **{mod_only}** mod-only definitions",
        "",
    ]))

    if shadowed:
        print(f"## Shadowed Definitions Vanilla Changed ({len(shadowed)})")
        print()
        for d, vfile, versions, result in shadowed:
            changes.print_target(d["name"], [("Kind", d["kind"]), ("Mod", f"`{d['file']}:{d['line']}`"),
                                             ("Vanilla", f"`{vfile}`")],
                                 result, versions, tags, args.diff, dismissed)

    if replaced:
        print(f"## Same-Path File Replacements Vanilla Changed ({len(replaced)})")
        print()
        print("The mod file replaces vanilla's file at the same path, so every definition "
              "inside it is measured here, not as a shadow.")
        print()
        for rel, versions, result in replaced:
            changes.print_target(rel, [], result, versions, tags, args.diff, dismissed)

    if file_review:
        print(f"## Same-Path Files Vanilla Added or Removed ({len(file_review)})")
        print()
        for rel, change, since in file_review:
            what = ("vanilla added this file; your file replaces it" if change == "added"
                    else "vanilla removed this file; your copy is now the only one")
            print(f"- `{rel}`: {what} *({since})*")
        print()

    if new_coll:
        print(f"## New Name Collisions: vanilla now defines a name the mod also "
              f"defines ({len(new_coll)})")
        print()
        for d, vfile, since in new_coll:
            print(f"- **{d['kind']}:{d['name']}**, mod `{d['file']}:{d['line']}` "
                  f"vs vanilla `{vfile}` *({since})*")
        print()

    if van_removed:
        print(f"## Shadowed Definitions Removed from Vanilla ({len(van_removed)})")
        print()
        for d, vfile, since in van_removed:
            print(f"- **{d['kind']}:{d['name']}**, mod `{d['file']}:{d['line']}` "
                  f"(was in `{vfile}`, removed in {since}); the mod copy is now the only definition")
        print()

    if not shadowed and not replaced and not file_review and not new_coll and not van_removed:
        print("**All GUI overrides are current with vanilla.**")
    else:
        print("---")
        print(f"**Action needed:** {n_changes(shadowed)} changes in {len(shadowed)} shadowed "
              f"definitions, {n_changes(replaced)} in {len(replaced)} replaced files, "
              f"{len(new_coll)} new collisions.")
        if not args.diff and (shadowed or replaced):
            print("Run with `--diff` for vanilla's changes since each copy's version.")
    print()
    print("_Implicit GUI overrides: a mod template/type definition shadows the "
          "vanilla definition of the same name. The mod loads after vanilla, so "
          "the override applies; this reports where vanilla then changed underneath "
          "it._")

    findings = []
    for _d, _vfile, _versions, result in shadowed:
        findings += result.findings
    for rel, change, since in file_review:
        findings.append(Finding("gui_file_review", rel, rel, f"vanilla {change}", None,
                                {"target": gui_file_target(rel), "change": change}, since))
    for _rel, _versions, result in replaced:
        findings += result.findings
    for d, vfile, since in van_removed:
        findings.append(Finding("gui_van_removed", d["name"], f"{d['file']}:{d['line']}",
                                "", {"vanilla_file": vfile} if want else None,
                                {"target": gui_def_target((d["module"], d["kind"], d["name"]))},
                                since))
    for d, vfile, since in new_coll:
        findings.append(Finding("gui_new_collision", d["name"], f"{d['file']}:{d['line']}",
                                "", {"vanilla_file": vfile} if want else None,
                                {"target": gui_def_target((d["module"], d["kind"], d["name"]))},
                                since))
    generated.print()
    return findings + generated.findings("gui_generated")
