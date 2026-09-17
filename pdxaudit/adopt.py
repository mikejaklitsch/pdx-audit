"""Adopted sources: mods whose code the mod absorbed, fully or partly, kept current
with their upstream.

Each unit the mod defines that exists anywhere in an adopted source's history is
compared, by diff3, with the source's texts of it in the source's own history order,
so the copy's baseline is the version it differs from least (the oldest on ties) and
every change upstream made after it is a finding. A whole file matches by path, and
the definitions inside a file matched that way are compared with the file; GUI
definitions, script definitions and localization keys match by name. A source's
rename rules apply first, on the source side, to identifiers and file names that
start with a rule's `from`, never to a name vanilla uses.

  upstream changed a unit you carry after your baseline
      diff3's changes, of kind gui_<change>_<priority> or override_<change>_<priority>
      (loc_changed for a localization key)
  upstream removed a unit you still carry        adopted_unit_removed
  upstream added a unit inside a file you carry  adopted_unit_added

Script blocks match within their kind: an INJECT, or a merging type's plain entry,
with either of those; a definition or REPLACE with a definition or REPLACE. A unit vanilla defines stays vanilla's when a
source stops overriding it, so it is neither removed nor compared once the source's
newest version no longer has it.

Every finding's key holds the source's id as `base`. An adopted source that declares
a dependency on one of the mod's foundations is first reduced to its own layer
(layer_report): its units identical to the flattened foundations belong to them."""
import hashlib
import re
import sys
from collections import defaultdict

from . import changes, diff3, flatten, session
from .config import should_skip
from .dupes import type_of
from .gui import gui_def_target, gui_file_target, mod_gui_files, parse_gui_defs
from .loc import is_replace_loc, loc_entries, loc_target, mod_loc_files
from .report import Finding
from .tracker import MODULE_ROOTS

_IDENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")


# --- the source's units ------------------------------------------------------------------------

def block_kind(prefix, merged=False):
    """'inject' for an INJECT, and for a merging type's plain entry, which the engine
    merges into the definition rather than defining it; 'def' otherwise."""
    return "inject" if prefix in flatten.INJECTS or (merged and not prefix) else "def"


def renamer(rules, protected=frozenset()):
    """(rename text, rename path) applying rename rules to identifiers and file names
    that start with a rule's `from`, leaving names in `protected` alone."""
    rules = [(r["from"], r["to"]) for r in rules or () if r.get("from")]
    if not rules:
        return (lambda s: s), (lambda p: p)

    def token(tok):
        if tok in protected:
            return tok
        for frm, to in rules:
            if tok.startswith(frm):
                return to + tok[len(frm):]
        return tok

    def text(s):
        return _IDENT.sub(lambda m: token(m.group(0)), s)

    def path(p):
        folder, _sep, name = p.rpartition("/")
        return f"{folder}/{token(name)}" if folder else token(name)
    return text, path


def _gui_path(path):
    parts = path.split("/")
    return len(parts) >= 3 and parts[0] in MODULE_ROOTS and parts[1] == "gui" and path.endswith(".gui")


def _common_path(path):
    parts = path.split("/")
    return len(parts) >= 4 and parts[0] in MODULE_ROOTS and parts[1] == "common" and path.endswith(".txt")


def units_of(source, commit, rules=(), protected=frozenset(), merging=frozenset()):
    """A source version's units after its rename rules:
    {"gui_files": {path: text}, "gui_defs": {key: (path, text)}, "script_files": {path: text},
     "blocks": {(kind, category, name): (path, text)}, "loc": {(language, key): (path, value)},
     "loc_files": {path: text}, "file_units": {path: {unit}}}. A block's kind is 'inject'
    for an INJECT and 'def' otherwise. A unit is ("gui", key), ("block", key) or
    ("loc", key). Within the source the first file's copy of a name wins."""
    def build():
        text_of, path_of = renamer(rules, protected)
        out = {"gui_files": {}, "gui_defs": {}, "script_files": {}, "blocks": {}, "loc": {}, "loc_files": {},
               "file_units": defaultdict(set)}
        for path, text in flatten.layer_texts(source, commit, _gui_path):
            p, t = path_of(path), text_of(text)
            out["gui_files"][p] = t
            for d in parse_gui_defs(t)[0]:
                if d["kind"] in ("template", "type"):
                    key = (p.split("/", 1)[0], d["kind"], d["name"])
                    out["gui_defs"].setdefault(key, (p, d["text"]))
                    out["file_units"][p].add(("gui", key))
        for path, text in flatten.layer_texts(source, commit, _common_path):
            p, t = path_of(path), text_of(text)
            out["script_files"][p] = t
            for prefix, name, block in flatten.top_entries(t):
                key = (block_kind(prefix, type_of(p) in merging), p.rsplit("/", 1)[0], name)
                out["blocks"].setdefault(key, (p, block))
                out["file_units"][p].add(("block", key))
        for path, text in flatten.layer_texts(source, commit, flatten._loc_file, flatten.loc_order):
            p, t = path_of(path), text_of(text)
            out["loc_files"][p] = t
            for lang, key, value, _line in loc_entries(t):
                out["loc"].setdefault((lang, key), (p, value))
                out["file_units"][p].add(("loc", (lang, key)))
        return out
    frozen = tuple((r["from"], r["to"]) for r in rules or ())
    return session.memo(("adopt.units", source.git_dir, commit, frozen, len(protected), tuple(sorted(merging))),
                        build)


# --- the source's own layer ----------------------------------------------------------------------

def layer_report(stack, source, commit, rules=(), protected=frozenset(), merging=frozenset()):
    """{unit: 'new' | 'same' | 'changed'} for each unit a source version defines: its
    own new name, a redefinition identical to the flattened foundations at some point
    of their history, or a redefinition with changes. Produces no findings."""
    units = units_of(source, commit, rules, protected, merging)
    history = [pid for pid, _m in reversed(stack.commits())]
    modules = sorted({k[0] for k in units["gui_defs"]} | {p.split("/", 1)[0] for p in units["gui_files"]})
    categories = sorted({k[1] for k in units["blocks"]})
    wanted = set(units["loc"])
    report = {}

    def classify(unit, text, texts_at, unwrap=False):
        seen = False
        dialect = diff3.GUI if unit[0] in ("gui", "guifile") else diff3.SCRIPT
        view = (lambda t: diff3.body(diff3.nodes(t))) if unwrap else diff3.nodes
        for pid in reversed(history):
            theirs = texts_at(pid)
            if theirs is None:
                continue
            seen = True
            if not diff3.distance(view(text), view(theirs), dialect):
                report[unit] = "same"
                return
        report[unit] = "changed" if seen else "new"

    for key, (_p, text) in units["gui_defs"].items():
        classify(("gui", key), text, lambda pid, key=key: (stack.gui_index(pid, modules)[0].get(key) or (None, None))[1])
    for key, (_p, text) in units["blocks"].items():
        classify(("block", key), text,
                 lambda pid, key=key: (stack.block_index(pid, categories).get(key[1:]) or (None, None))[1],
                 unwrap=True)
    for key, (_p, value) in units["loc"].items():
        values = [stack.loc(pid, wanted).get(key) for pid in history]
        report[("loc", key)] = ("same" if value in values else "changed" if any(v is not None for v in values)
                                else "new")
    return report


# --- the mod's units --------------------------------------------------------------------------

def mod_units(mod_root, merging=frozenset()):
    """The mod's units: {"gui_files": {rel: text}, "gui_defs": {key: def}, "script_files": {rel: text},
    "blocks": {(kind, category, name): (rel, line, prefix, text)}, "loc": {(language, key): (rel, value)},
    "loc_files": {rel: text}}."""
    out = {"gui_files": {}, "gui_defs": {}, "script_files": {}, "blocks": {}, "loc": {}, "loc_files": {}}
    for rel, text in mod_gui_files(mod_root):
        out["gui_files"][rel] = text
        for d in parse_gui_defs(text)[0]:
            if d["kind"] in ("template", "type"):
                out["gui_defs"].setdefault((rel.split("/", 1)[0], d["kind"], d["name"]), dict(d, file=rel))
    for fp in session.mod_paths(mod_root, ".txt"):
        rel = fp.relative_to(mod_root).as_posix()
        if not _common_path(rel) or should_skip(rel):
            continue
        try:
            text = session.read_text(fp)
        except (OSError, UnicodeDecodeError):
            continue
        out["script_files"][rel] = text
        pos = 0
        for prefix, name, block in flatten.top_entries(text):
            at = text.find(block, pos)
            pos = at + len(block) if at >= 0 else pos
            line = text.count("\n", 0, max(at, 0)) + 1
            out["blocks"].setdefault((block_kind(prefix, type_of(rel) in merging), rel.rsplit("/", 1)[0], name),
                                     (rel, line, prefix, block))
    for rel, text in sorted(mod_loc_files(mod_root), key=lambda f: not is_replace_loc(f[0])):
        out["loc_files"][rel] = text
        for lang, key, value, _line in loc_entries(text):
            out["loc"].setdefault((lang, key), (rel, value))
    return out


def _norm(text):
    return " ".join((text or "").split())


# --- the audit ---------------------------------------------------------------------------------

def run_adopted_audit(mod_root, vanilla_repo, adopted, foundations, args, ctx=None, prior=()):
    """Compare the mod with each adopted source's history. `foundations` are the mod's
    chosen foundations, `prior` the findings of this run's other audits. Returns
    (findings, remedies), where remedies maps a target to the one remedy its group shows
    when an adopted source's newest version already has a vanilla change the mod lacks."""
    from .base import StackBase, VanillaBase
    from .sources import declared_dependencies
    vanilla = VanillaBase(vanilla_repo)
    merging = frozenset(vanilla.merge_types(vanilla.commits()[0][0]))
    mine = mod_units(mod_root, merging)
    want = bool(getattr(args, "results_file", None))
    limits = dict(getattr(args, "source_version", None) or [])
    findings, remedies, lines, details = [], {}, [], []
    protected = _vanilla_names(vanilla_repo) if any(s.rename for s in adopted) else frozenset()
    in_vanilla = _vanilla_units(vanilla_repo, mine)
    for src in adopted:
        versions = src.versions()
        if src.id in limits:
            tags = [t for _c, t, _p in versions]
            versions = versions[:tags.index(limits[src.id]) + 1] if limits[src.id] in tags else versions
        if not versions:
            lines.append(f"- **{src.id}**: no versions yet"
                         + (f"; take a snapshot with `pdx-audit --snapshot-source {src.id}`" if src.kind == "folder"
                            else ""))
            continue
        print(f"Comparing {mod_root.name} with {len(versions)} versions of {src.id}...", file=sys.stderr)
        tags = [t for _c, t, _p in versions]
        history = [units_of(src, c, src.rename, protected, merging) for c, _t, _p in versions]
        newest = history[-1]

        skip = set()
        deps = set(declared_dependencies(src.path))
        under = [f for f in foundations if f.id in deps or (f.metadata_id or "") in deps]
        if under:
            report = layer_report(StackBase(vanilla_repo, under), src, versions[-1][0], src.rename, protected,
                                  merging)
            skip = {u for u, status in report.items() if status == "same"}

        found, targets = _distinct(*_compare(src, mine, history, tags, skip, want, in_vanilla))
        findings += found
        details.append((src, tags, found, targets))
        lines.append(f"- **{src.id}** ({src.kind}), {len(versions)} versions up to {tags[-1]}: "
                     f"{len(found)} findings" + (f"; {len(skip)} units belong to its foundations" if skip else ""))

        for f in prior:
            k = f.key or {}
            if k.get("base") or not k.get("vanilla") or not f.kind.startswith(("gui_vanilla", "override_vanilla")):
                continue
            text = _unit_text(newest, k["target"])
            if text is not None and _norm(k["vanilla"]) in _norm(text):
                remedies[k["target"]] = f"take {src.id} {tags[-1]}, which already has vanilla's change"

    print("# Adopted Source Audit")
    print()
    print("Each unit the mod carries from an adopted source is compared with that source's history.")
    print()
    print("\n".join(lines) if lines else "No adopted sources.")
    print()
    dismissed = getattr(ctx, "dismissed", frozenset())
    for src, tags, found, targets in details:
        if targets:
            print(f"## Changes {src.id} Made After Your Copy ({len(targets)})")
            print()
            for title, facts, result, texts in targets:
                changes.print_target(title, facts, result, texts, tags, getattr(args, "diff", False), dismissed,
                                     upstream=True)
        units = [f for f in found if f.kind.startswith("adopted_unit")]
        if units:
            print(f"## Units {src.id} Removed or Added ({len(units)})")
            print()
            for f in units:
                print(f"- {'✗' if f.kind == 'adopted_unit_removed' else '⚠'} **{f.name}** `{f.location}`: {f.detail}")
            print()
    return findings, remedies


def _unit_text(units, target):
    kind, _sep, rest = target.partition(":")
    if kind == "gui":
        module, gkind, name = rest.split("/", 2)
        return (units["gui_defs"].get((module, gkind, name)) or (None, None))[1]
    if kind == "guifile":
        return units["gui_files"].get(rest)
    if kind in ("override", "script"):
        cat, _sep, name = rest.rpartition("/")
        return (units["blocks"].get(("def", cat, name)) or units["blocks"].get(("inject", cat, name))
                or (None, None))[1]
    return None


def _first_gone(texts, tags):
    last = max(i for i, t in enumerate(texts) if t is not None)
    return tags[last + 1] if last + 1 < len(tags) else tags[-1]


def _distinct(found, targets):
    """found and targets with no finding id shared across the units' copies (changes.distinct)."""
    fixed = changes.distinct([result for _title, _facts, result, _texts in targets])
    swap = {id(old): new for t, r in zip(targets, fixed) for old, new in zip(t[2].findings, r.findings)}
    return ([swap.get(id(f), f) for f in found],
            [(title, facts, r, texts) for (title, facts, _result, texts), r in zip(targets, fixed)])


def _compare(src, mine, history, tags, skip, want, in_vanilla=lambda kind, key: False):
    """(findings, targets): targets lists (title, facts, Audited, texts) for each unit
    with changes, for the detail."""
    findings, carried, targets = [], set(), []
    removed = lambda name, loc, since, target: Finding(
        "adopted_unit_removed", name, loc, f"{src.id} removed it in {since}", None,
        {"target": target, "base": src.id}, since)

    def audit(audit_name, name, target, text, texts, file, line, unwrap=False, block_type=None, unit=None):
        if not any(t is not None for t in texts):
            return False
        if texts[-1] is None:
            if unit is not None and in_vanilla(*unit):
                return False                   # the source stopped overriding vanilla's unit
            findings.append(removed(name, f"{file}:{line}" if line else file, _first_gone(texts, tags), target))
            return True
        result = changes.audit(audit_name, name, target, text, texts, tags, file, line or 1, unwrap=unwrap,
                               want=want, block_type=block_type, vanilla_file=file, owner=src.id,
                               dialect=diff3.GUI if audit_name == "gui" else diff3.SCRIPT)
        findings.extend(result.findings)
        if result.flagged:
            where = f"`{file}:{line}`" if line else f"`{file}`"
            targets.append((name, [("Mod", where), ("Compared with", f"{src.id}'s versions")], result, texts))
        return True

    for rel, text in sorted(mine["gui_files"].items()):
        if audit("gui", rel, gui_file_target(rel), text, [h["gui_files"].get(rel) for h in history], rel, 0,
                 unit=("guifile", rel)):
            carried.add(rel)
    for rel, text in sorted(mine["script_files"].items()):
        if audit("override", rel, f"file:{rel}", text, [h["script_files"].get(rel) for h in history], rel, 0,
                 unit=("scriptfile", rel)):
            carried.add(rel)
    for rel, text in sorted(mine["loc_files"].items()):
        if any(h["loc_files"].get(rel) is not None for h in history):
            carried.add(rel)

    for key, d in sorted(mine["gui_defs"].items()):
        if d["file"] in carried or ("gui", key) in skip:
            continue
        audit("gui", d["name"], gui_def_target(key), d["text"],
              [(h["gui_defs"].get(key) or (None, None))[1] for h in history], d["file"], d["line"],
              unit=("gui", key))
    for key, (rel, line, prefix, block) in sorted(mine["blocks"].items()):
        if rel in carried or ("block", key) in skip:
            continue
        _kind, cat, name = key
        target = f"override:{cat}/{name}" if prefix else f"script:{cat}/{name}"
        audit("override", name, target, block, [(h["blocks"].get(key) or (None, None))[1] for h in history],
              rel, line, unwrap=True, block_type=prefix or "definition", unit=("block", key))
    for key, (rel, value) in sorted(mine["loc"].items()):
        if ("loc", key) in skip:
            continue
        values = [(h["loc"].get(key) or (None, None))[1] for h in history]
        if not any(v is not None for v in values):
            continue
        if values[-1] is None:
            if not in_vanilla("loc", key):
                findings.append(removed(key[1], rel, _first_gone(values, tags), loc_target(key)))
            continue
        if value not in values:
            continue
        base_i = values.index(value)
        after = next((i for i in range(base_i + 1, len(values)) if values[i] != value), None)
        if after is not None and values[-1] != value:
            findings.append(Finding("loc_changed", key[1], rel, key[0], None,
                                    {"target": loc_target(key), "old": value, "new": values[-1], "mod": value,
                                     "base": src.id}, tags[after], tags[base_i]))

    # Units upstream added to a file the mod carries units from, which the mod does not define.
    # A unit vanilla defines is vanilla's, so it does not make a file one the mod carries.
    defined = ({("gui", k) for k in mine["gui_defs"]} | {("block", k) for k in mine["blocks"]}
               | {("loc", k) for k in mine["loc"]})
    carried_units = {u for u in defined if not in_vanilla(*u)}
    newest = history[-1]
    for path, units in sorted(newest["file_units"].items()):
        if path in carried:
            continue
        present = [h["file_units"].get(path, set()) for h in history]
        ours = [i for i, us in enumerate(present) if us & carried_units]
        if not ours:
            continue
        start = min(ours)
        for unit in sorted(units - defined - skip, key=repr):
            if unit in present[start]:
                continue
            since = tags[next(i for i in range(start, len(tags)) if unit in present[i])]
            text = _unit_body(newest, unit)
            name = unit[1][-1] if unit[0] != "loc" else unit[1][1]
            findings.append(Finding("adopted_unit_added", name, path, f"{src.id} added it in {since}", None,
                                    {"target": f"adopted:{src.id}:{path}/{name}", "base": src.id,
                                     "unit": hashlib.sha1(_norm(text).encode("utf-8")).hexdigest()}, since))
    return findings, targets


def _unit_body(units, unit):
    kind, key = unit
    if kind == "gui":
        return units["gui_defs"][key][1]
    if kind == "block":
        return units["blocks"][key][1]
    return units["loc"][key][1]


def _vanilla_units(vanilla_repo, mine):
    """in_vanilla(kind, key): whether vanilla's newest snapshot defines a unit of the mod's.
    Kinds are 'gui', 'guifile', 'block', 'scriptfile' and 'loc'; an INJECT is never
    vanilla's. Each index is read once, when first asked."""
    from .base import VanillaBase
    from .dupes import type_of
    base = VanillaBase(vanilla_repo)
    held = {}

    def once(name, build):
        if name not in held:
            held[name] = build()
        return held[name]

    def newest():
        return once("newest", lambda: base.commits()[0][0])

    def gui():
        modules = sorted({k[0] for k in mine["gui_defs"]} | {p.split("/", 1)[0] for p in mine["gui_files"]})
        return once("gui", lambda: base.gui_index(newest(), modules))

    def in_vanilla(kind, key):
        if kind == "gui":
            return key in gui()[0]
        if kind == "guifile":
            return key in gui()[1]
        if kind == "block":
            block, cat, name = key
            names = once("defs", lambda: base.definitions(newest()))["names"]
            return block == "def" and name in names.get(type_of(f"{cat}/x.txt"), {})
        if kind == "scriptfile":
            return key in once("defs", lambda: base.definitions(newest()))["files"]
        if kind == "loc":
            return key in once("loc", lambda: base.loc(newest(), set(mine["loc"])))
        return False
    return in_vanilla


def _vanilla_names(vanilla_repo):
    """Names vanilla uses, which rename rules never touch: its `name =` keys and its
    script definition names at the newest snapshot."""
    from .base import VanillaBase
    base = VanillaBase(vanilla_repo)
    newest = base.commits()[0][0]
    names = set(base.vocab(newest))
    for type_names in base.definitions(newest)["names"].values():
        names.update(type_names)
    return frozenset(names)
