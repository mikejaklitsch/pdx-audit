"""Duplicate audit: one source of truth per definition.

For every `common/<type>` folder (all module roots together) the mod should
define or override each name in exactly one place: one plain definition, one
REPLACE, or one INJECT. The audit reports

  multiple sources        a name defined or overridden in 2+ places in the mod
  define key set twice    the same define key set in 2+ places in the mod
  GUI definition twice    the same template/type in 2+ mod .gui files
  plain in other file     a plain definition of a vanilla name outside the file
                          at vanilla's path (it duplicates vanilla's)
  file override drops     a mod file at vanilla's path that lacks some of
                          vanilla's definitions (informational)
  loc key twice           the same (language, key) defined 2+ times among the
                          mod's replace/ localization files, or among its others
  on_action syntax        one on_action block setting `effect` or `trigger` twice
  on_action key           one on_action's `effect` or `trigger` in 2+ blocks

Types vanilla itself defines across several files are merged by the engine
(on_action, defines, named_colors, ...), so they skip the multiple-sources and
plain-in-other-file checks. They are worked out from the newest snapshot; the
config key `merge_types` adds more. Only the newest snapshot is needed."""

import bisect
import io
import json
import re
import sys
import tarfile
from collections import defaultdict
from pathlib import Path

from . import session
from .config import cfg, should_skip
from .gui import mod_gui_files, parse_gui_defs
from .loc import is_replace_loc, loc_entries, mod_loc_files
from .report import Finding
from .tracker import MODULE_ROOTS, _git_archive, cache_path, full_hash

DUPES_CACHE_VERSION = 1
_HEAD = re.compile(r"^\s*(?:([A-Z][A-Z_]*):)?([A-Za-z0-9_.\-:]+)\s*\??=")
ON_ACTION = "common/on_action"
# Keys an on_action holds once: a later one replaces the earlier one.
ON_ACTION_SINGLE = ("effect", "trigger")


def type_of(rel):
    """'common/<type>' for a module-root-relative .txt path, else None."""
    parts = rel.split("/")
    if len(parts) >= 4 and parts[0] in MODULE_ROOTS and parts[1] == "common":
        return f"common/{parts[2]}"
    return None


def _code(line):
    in_str = False
    for i, c in enumerate(line):
        if c == '"':
            in_str = not in_str
        elif c == "#" and not in_str:
            return line[:i]
    return line


def scan_script(text):
    """(entries, define_keys) for one script file: entries are (prefix, name,
    line) for top-level statements; define_keys are (namespace, key, line) for
    statements one level down."""
    entries, keys = [], []
    depth, namespace = 0, None
    for i, raw in enumerate(text.split("\n"), 1):
        code = _code(raw)
        m = _HEAD.match(code)
        if m and not m.group(2).startswith("@"):
            if depth == 0:
                entries.append((m.group(1) or "", m.group(2), i))
                namespace = m.group(2)
            elif depth == 1 and namespace:
                keys.append((namespace, m.group(2), i))
        depth = max(0, depth + code.count("{") - code.count("}"))
    return entries, keys


def _cache_path(vanilla_repo, commit):
    full = full_hash(vanilla_repo, commit)
    if not full:
        return None
    return cache_path(vanilla_repo, f"dupes-v{DUPES_CACHE_VERSION}-{full}.json")


def vanilla_definitions(vanilla_repo, commit):
    """Returns the top-level script definitions of vanilla at `commit`.

    The result has the form {"names": {type: {name: [files]}}, "files": {path:
    [names]}}. The result goes into the cache, with one entry for each commit."""
    cache = _cache_path(vanilla_repo, commit)
    if cache and cache.is_file():
        try:
            return json.loads(cache.read_text())
        except (OSError, ValueError):
            pass
    names = defaultdict(lambda: defaultdict(list))
    files = {}
    raw = _git_archive(vanilla_repo, commit, [f"{m}/common" for m in MODULE_ROOTS],
                       timeout=180)
    try:
        with tarfile.open(fileobj=io.BytesIO(raw or b""), ignore_zeros=True) as tf:
            for member in tf.getmembers():
                if not member.isfile() or not member.name.endswith(".txt"):
                    continue
                t = type_of(member.name)
                f = tf.extractfile(member)
                if t is None or f is None:
                    continue
                entries, _keys = scan_script(f.read().decode("utf-8-sig", errors="replace"))
                files[member.name] = sorted({n for _p, n, _l in entries})
                for _p, n, _l in entries:
                    if member.name not in names[t][n]:
                        names[t][n].append(member.name)
    except tarfile.TarError:
        pass
    data = {"names": {t: dict(v) for t, v in names.items()}, "files": files}
    if cache and files:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(cache)
        except OSError:
            pass
    return data


def merge_types(vanilla):
    """Types vanilla defines some name in more than one file, plus config extras."""
    out = {t for t, names in vanilla["names"].items()
           if any(len(set(files)) > 1 for files in names.values())}
    for extra in cfg("merge_types", []) or []:
        extra = str(extra).strip("/")
        out.add(extra if extra.startswith("common/") else f"common/{extra}")
    return out


def _same_text(locs):
    """True when every copy of a localization key reads the same."""
    return len({value for _rel, _line, value in locs}) == 1


def _where(prefix, rel, line):
    kind = prefix if prefix else "definition"
    return f"{kind} at {rel}:{line}"


def run_dupes_audit(mod_root, base, new_hash, new_msg, args, ctx=None):
    """The `base` argument is a Base, or the path to the vanilla tracker."""
    from .base import as_base
    base = as_base(base)
    print(f"Scanning {mod_root.name} for duplicate definitions...", file=sys.stderr)
    vanilla = base.definitions(new_hash)
    merging = base.merge_types(new_hash)
    category = getattr(args, "category", None)
    block = getattr(args, "block", None)

    # Which object a name refers to is settled by where vanilla defines it, not by the
    # folder the mod's file sits in: a REPLACE in common/buildings and a definition in
    # common/building_types are one definition twice, however the mod files them.
    vanilla_types = defaultdict(set)
    for vt, names in vanilla["names"].items():
        for n in names:
            vanilla_types[n].add(vt)
    of_vanilla = {n: next(iter(ts)) for n, ts in vanilla_types.items() if len(ts) == 1}
    # A name the mod alone defines stays with its folder. The engine namespaces names by
    # object type, and mods reuse one name across types on purpose: a modifier icon and
    # a modifier type definition must share it, as must a game concept and the value it
    # documents. Those are not one definition twice.

    entries = defaultdict(list)       # (type, name) -> [(prefix, rel, line)]
    file_names = {}                   # rel -> names defined in that mod file
    define_keys = defaultdict(list)   # (namespace, key) -> [(rel, line)]
    single_keys = defaultdict(list)   # (on_action, key) -> [(rel, block line, [lines])]
    for fp in session.mod_paths(mod_root, ".txt"):
        rel = fp.relative_to(mod_root).as_posix()
        t = type_of(rel)
        if t is None or should_skip(rel) or (category and category not in t):
            continue
        try:
            text = session.read_text(fp)
        except Exception:
            continue
        found, keys = scan_script(text)
        file_names[rel] = {n for _p, n, _l in found}
        for prefix, name, line in found:
            if not block or name == block:
                entries[(of_vanilla.get(name, t), name)].append((prefix, rel, line))
        if t == "common/defines":
            for ns, key, line in keys:
                if not block or f"{ns}.{key}" == block or key == block:
                    define_keys[(ns, key)].append((rel, line))
        if t == ON_ACTION:
            starts = [line for _p, _n, line in found]
            blocks = {}
            for ns, key, line in keys:
                if key in ON_ACTION_SINGLE and (not block or ns == block):
                    start = starts[bisect.bisect_right(starts, line) - 1]
                    blocks.setdefault((ns, key, start), []).append(line)
            for (ns, key, start), lines in blocks.items():
                single_keys[(ns, key)].append((rel, start, lines))

    gui_defs = defaultdict(list)      # (module, kind, name) -> [(rel, line)]
    loc_keys = defaultdict(list)      # (replace?, language, key) -> [(rel, line, value)]
    if not category:
        for rel, text in mod_gui_files(mod_root):
            defs, _clean = parse_gui_defs(text)
            for d in defs:
                if d["kind"] in ("template", "type") and (not block or d["name"] == block):
                    gui_defs[(rel.split("/", 1)[0], d["kind"], d["name"])].append((rel, d["line"]))
        for rel, text in mod_loc_files(mod_root):
            replace = is_replace_loc(rel)
            for lang, key, value, line in loc_entries(text):
                if not block or key == block:
                    loc_keys[(replace, lang, key)].append((rel, line, value))

    if ctx is not None:
        ctx.scanned["dupes"] = (len(entries) + len(define_keys) + len(gui_defs)
                                + len(loc_keys))

    multiple, plain_other, define_twice, gui_twice, drops = [], [], [], [], []
    loc_twice = [(replace, lang, key, locs) for (replace, lang, key), locs in sorted(loc_keys.items())
                 if len(locs) >= 2]
    on_action_syntax = [(ns, key, rel, lines) for (ns, key), found in sorted(single_keys.items())
                        for rel, _start, lines in found if len(lines) >= 2]
    on_action_twice = [(ns, key, found) for (ns, key), found in sorted(single_keys.items())
                       if len(found) >= 2]
    for (t, name), locs in sorted(entries.items()):
        if t in merging:
            continue
        if len(locs) >= 2:
            multiple.append((t, name, locs))
            continue
        prefix, rel, line = locs[0]
        vfiles = vanilla["names"].get(t, {}).get(name)
        if not prefix and vfiles and rel not in vfiles:
            plain_other.append((t, name, rel, line, vfiles))
    for (ns, key), locs in sorted(define_keys.items()):
        if len(locs) >= 2:
            define_twice.append((ns, key, locs))
    for (module, kind, name), locs in sorted(gui_defs.items()):
        if len(locs) >= 2:
            gui_twice.append((module, kind, name, locs))
    if not block:
        for rel, names in sorted(file_names.items()):
            vnames = vanilla["files"].get(rel)
            if vnames:
                dropped = sorted(set(vnames) - names)
                if dropped:
                    drops.append((rel, dropped))

    print(f"# Duplicate Audit: {new_msg}")
    print()
    print(f"Checked **{len(entries)}** script names, **{len(define_keys)}** define keys, "
          f"**{len(gui_defs)}** GUI definitions and **{len(loc_keys)}** localization keys. "
          f"Merged by the engine, so exempt from the multiple-sources check: "
          + (", ".join(f"`{t}`" for t in sorted(merging)) or "none") + ".")
    print(f"- **{len(multiple)}** names defined or overridden in more than one place")
    print(f"- **{len(define_twice)}** define keys set in more than one place")
    print(f"- **{len(gui_twice)}** GUI definitions defined in more than one file")
    print(f"- **{len(loc_twice)}** localization keys defined more than once")
    print(f"- **{len(on_action_syntax)}** on_action blocks setting `effect` or `trigger` twice")
    print(f"- **{len(on_action_twice)}** on_action `effect` or `trigger` keys set in more than one block")
    print(f"- **{len(plain_other)}** plain definitions of a vanilla name outside vanilla's file")
    print(f"- **{len(drops)}** mod files at vanilla's path that drop vanilla definitions")
    print()

    def listing(title, rows):
        if not rows:
            return
        print(f"## {title} ({len(rows)})")
        print()
        for head, locs in rows:
            print(f"- {'⚠' if head.endswith(', same text)') else '✗'} **{head}**")
            for loc in locs:
                print(f"    - {loc}")
        print()

    listing("Multiple Sources of Truth",
            [(f"{name} ({t})", [_where(p, r, l) for p, r, l in locs])
             for t, name, locs in multiple])
    listing("Define Keys Set Twice",
            [(f"{ns}.{key}", [f"{r}:{l}" for r, l in locs]) for ns, key, locs in define_twice])
    listing("GUI Definitions Defined Twice",
            [(f"{kind} {name}", [f"{r}:{l}" for r, l in locs]) for _m, kind, name, locs in gui_twice])
    listing("Localization Keys Defined More Than Once",
            [(f"{key} ({lang}{', replace' if replace else ''}"
              f"{', same text' if _same_text(locs) else ''})",
              [f"{r}:{l}" for r, l, _v in locs]) for replace, lang, key, locs in loc_twice])
    listing("On_action Blocks Setting a Key Twice",
            [(f"{ns} {key}", [f"{rel}:{l}" for l in lines]) for ns, key, rel, lines in on_action_syntax])
    listing("On_action Keys Set in More Than One Block",
            [(f"{ns} {key}", [f"{rel}:{lines[0]}" for rel, _s, lines in found])
             for ns, key, found in on_action_twice])
    if plain_other:
        print(f"## Plain Definitions of a Vanilla Name Outside Vanilla's File ({len(plain_other)})")
        print()
        for t, name, rel, line, vfiles in plain_other:
            print(f"- ⚠ **{name}** at `{rel}:{line}`; vanilla defines it in "
                  + ", ".join(f"`{v}`" for v in vfiles))
        print()
    if drops:
        print(f"## Mod Files at Vanilla's Path That Drop Vanilla Definitions ({len(drops)})")
        print()
        for rel, dropped in drops:
            print(f"- `{rel}`: " + ", ".join(dropped))
        print()
    if not (multiple or define_twice or gui_twice or plain_other or loc_twice
            or on_action_syntax or on_action_twice):
        print("**No duplicate definitions found.**")

    # Where each definition is, for the --display app to read and show.
    want = getattr(args, "results_file", None)

    def sources(locs):
        if not want:
            return None
        return {"sources": [{"how": how or "definition", "file": rel, "line": line}
                            for how, rel, line in locs]}

    findings = []
    for t, name, locs in multiple:
        findings.append(Finding("dupes_multiple_sources", name, f"{locs[0][1]}:{locs[0][2]}",
                                "; ".join(_where(p, r, l) for p, r, l in locs), sources(locs),
                                {"target": f"dupes:{t}/{name}"}))
    for ns, key, locs in define_twice:
        findings.append(Finding("dupes_define_key", f"{ns}.{key}", f"{locs[0][0]}:{locs[0][1]}",
                                "; ".join(f"{r}:{l}" for r, l in locs),
                                sources([("set", r, l) for r, l in locs]),
                                {"target": f"dupes:common/defines/{ns}.{key}"}))
    for module, kind, name, locs in gui_twice:
        findings.append(Finding("dupes_gui_definition", name, f"{locs[0][0]}:{locs[0][1]}",
                                "; ".join(f"{r}:{l}" for r, l in locs),
                                sources([(kind, r, l) for r, l in locs]),
                                {"target": f"dupes:gui/{module}/{kind}/{name}"}))
    for replace, lang, key, locs in loc_twice:
        folder = "replace/" if replace else ""
        findings.append(Finding("dupes_loc_key_same" if _same_text(locs) else "dupes_loc_key", key,
                                f"{locs[0][0]}:{locs[0][1]}",
                                "; ".join(f"{r}:{l}" for r, l, _v in locs),
                                sources([("loc key", r, l) for r, l, _v in locs]),
                                {"target": f"dupes:loc/{lang}/{folder}{key}"}))
    for ns, key, rel, lines in on_action_syntax:
        findings.append(Finding("dupes_on_action_syntax", ns, f"{rel}:{lines[0]}",
                                f"`{key}` at lines " + ", ".join(map(str, lines)),
                                sources([(key, rel, l) for l in lines]),
                                {"target": f"dupes:{ON_ACTION}/{ns}/{key}", "mod": rel}))
    for ns, key, found in on_action_twice:
        findings.append(Finding("dupes_on_action_key", ns, f"{found[0][0]}:{found[0][2][0]}",
                                f"`{key}` in " + "; ".join(f"{r}:{lines[0]}" for r, _s, lines in found),
                                sources([(key, r, lines[0]) for r, _s, lines in found]),
                                {"target": f"dupes:{ON_ACTION}/{ns}/{key}"}))
    for t, name, rel, line, vfiles in plain_other:
        findings.append(Finding("dupes_plain_other_file", name, f"{rel}:{line}",
                                "vanilla: " + ", ".join(vfiles), sources([("", rel, line)]),
                                {"target": f"dupes:{t}/{name}", "files": sorted(vfiles),
                                 "mod": rel}))
    for rel, dropped in drops:
        shown = ", ".join(dropped[:8]) + (" …" if len(dropped) > 8 else "")
        findings.append(Finding("dupes_file_override_drops", rel, rel,
                                f"{len(dropped)} vanilla definitions not in the mod copy: {shown}",
                                None, {"target": f"dupesfile:{rel}", "dropped": dropped}))
    return findings
