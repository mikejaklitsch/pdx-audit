"""The intent store: why the mod differs from vanilla.

The store holds two kinds of records, each given by the user:

  rule    a system rule: one reason for many deviations. A matcher selects them
          across files by key, value, path, comment, change kind and file.
  entry   a one-off record for one deviation, at one address.

A **deviation** is one difference between a copy and vanilla's current text: one
diff3 Change of any kind, or one child that an INJECT sets. Its **address** names
the copy by vanilla identity (the content folder and block name, never the mod
file path) and the node inside it by the fields diff3 pairs siblings by. So an
address survives when vanilla or the mod moves a block to another file.

pdx-audit computes the state of each entry on each run and never stores it. It
never writes a reason that the user did not give, and it never writes into the
mod. The store is part of the per-user record (store.py), so it has the branch
semantics of the dismissals."""
import fnmatch
import hashlib
import io
import json
import re
import types
from contextlib import redirect_stdout
from dataclasses import dataclass, field

from . import changes, diff3, session
from .registry import registry

VERSION = 1
DISPOSITIONS = ("keep_mod", "take_vanilla", "merge", "banned")
SOURCE_KINDS = ("note", "commit", "user")
SCOPES = ("node", "subtree")
MATCH_FIELDS = ("audit", "content", "file", "block", "path", "vanilla_key", "mod_key",
                "vanilla_value", "mod_value", "name", "comment", "change", "vanilla_absent")
COPY_TYPES = ("replace", "inject", "file", "gui_def", "gui_file")


class IntentError(ValueError):
    """A rule or an entry that the store cannot hold."""


# --- the store --------------------------------------------------------------------

def of(state):
    """The intent part of a record state, made complete."""
    it = state.setdefault("intent", {})
    it.setdefault("version", VERSION)
    it.setdefault("rules", {})
    it.setdefault("entries", {})
    it.setdefault("baseline", None)
    return it


def validate_rule(rule, reg=None):
    """Raises IntentError when `rule` is not complete. A rule without a disposition
    is a grouping: its deviations always go to the user."""
    for k in ("id", "system", "reason"):
        if not isinstance(rule.get(k), str) or not rule[k].strip():
            raise IntentError(f"a rule needs a {k}")
    _check_common(rule, reg)
    match = rule.get("match")
    if not isinstance(match, dict) or not match:
        raise IntentError(f"rule {rule['id']} needs a match")
    unknown = set(match) - set(MATCH_FIELDS)
    if unknown:
        raise IntentError(f"rule {rule['id']}: unknown match fields {sorted(unknown)}")
    for k, v in match.items():
        if k == "vanilla_absent":
            if not isinstance(v, bool):
                raise IntentError(f"rule {rule['id']}: vanilla_absent is true or false")
        elif not (isinstance(v, list) and v and all(isinstance(x, str) for x in v)):
            raise IntentError(f"rule {rule['id']}: match field {k} is a list of strings")
        if k == "audit" and set(v) - set(COPY_TYPES):
            raise IntentError(f"rule {rule['id']}: audit is one of {', '.join(COPY_TYPES)}")
        for x in (v if isinstance(v, list) else []):
            if x.startswith("re:"):
                try:
                    re.compile(x[3:])
                except re.error as e:
                    raise IntentError(f"rule {rule['id']}: bad pattern {x}: {e}") from None
    tool = rule.get("tool")
    if tool is not None and reg is not None and reg.present and tool not in reg.tools:
        raise IntentError(f"rule {rule['id']}: {tool} is not a tool in pdx-maint.toml")


def validate_entry(entry, reg=None):
    for k in ("id", "system", "reason"):
        if not isinstance(entry.get(k), str) or not entry[k].strip():
            raise IntentError(f"an entry needs a {k}")
    _check_common(entry, reg)
    if entry.get("disposition") is None:
        raise IntentError(f"entry {entry['id']} needs a disposition")
    if entry.get("scope", "subtree") not in SCOPES:
        raise IntentError(f"entry {entry['id']}: scope is node or subtree")
    addr = entry.get("address")
    if not (isinstance(addr, dict) and isinstance(addr.get("identity"), str)
            and isinstance(addr.get("path"), list)):
        raise IntentError(f"entry {entry['id']} needs an address")


def _check_common(rec, reg):
    disp = rec.get("disposition")
    if disp is not None and disp not in DISPOSITIONS:
        raise IntentError(f"{rec['id']}: the disposition is one of {', '.join(DISPOSITIONS)}, or none")
    src = rec.get("source") or {}
    if src.get("kind") not in SOURCE_KINDS:
        raise IntentError(f"{rec['id']}: the source kind is one of {', '.join(SOURCE_KINDS)}")
    if reg is not None and reg.present and rec["system"] not in reg.systems:
        raise IntentError(f"{rec['id']}: {rec['system']} is not a system in pdx-maint.toml")


def add_rule(state, rule, reg=None):
    validate_rule(rule, reg)
    of(state)["rules"][rule["id"]] = rule


def add_entry(state, entry, reg=None):
    validate_entry(entry, reg)
    of(state)["entries"][entry["id"]] = entry


def remove(state, rid):
    it = of(state)
    for table in (it["rules"], it["entries"]):
        if rid in table:
            return table.pop(rid)
    raise IntentError(f"no rule or entry has id {rid}")


# --- canonical text and addresses ------------------------------------------------

def canon(node):
    """The text of a node without layout or comments, keyword case normalized (diff3
    already reads `not` as `NOT`). Equal nodes give equal text in every run."""
    if node is None:
        return None
    if node.kind == "items":
        return " ".join(node.value)
    if node.kind == "raw":
        return node.value
    if node.children is None:
        op, value = node.value
        if getattr(node, "weight", False):      # `10 = army_heavy_cavalry`, read as name and weight
            return f"{value} {op} {node.key}"
        return f"{node.key} {op or ''} {value if value is not None else ''}".strip()
    op, value = node.value
    head = " ".join(x for x in (node.key, op, value) if x)
    return head + " { " + " ".join(canon(c) for c in node.children) + " }"


def digest(text):
    return None if text is None else hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _ident(node, dialect):
    """The fields diff3 pairs a sibling by, strongest first, as a dict."""
    seg = {"key": node.key}
    if node.label != node.key:
        seg["name"] = node.label[len(node.key) + 1:]
    if node.children is not None:
        for c in node.children:
            if c.key in diff3.SELECTORS[dialect]:
                seg["sel"] = f"{c.key}#{digest(canon(c))}"
                break
    val = diff3._distinctive(node)
    if val is not None:
        seg["val"] = val
    return seg


def segment(node, siblings, dialect, cache=None):
    """The address segment of `node` among `siblings`. `cache` ({}) keeps the fields
    of each level, for many segments of one tree."""
    if cache is None:
        cache = {}
    key = (id(siblings), dialect)
    if key not in cache:
        idents = [_ident(s, dialect) for s in siblings]
        groups = {}
        for i, (s, ident) in enumerate(zip(siblings, idents)):
            groups.setdefault(json.dumps(ident, sort_keys=True), []).append(i)
        where = {(s.start, s.end): i for i, s in enumerate(siblings)}
        cache[key] = (siblings, idents, groups, where)
    _sibs, idents, groups, where = cache[key]
    i = where[(node.start, node.end)]
    seg = dict(idents[i])
    same = groups[json.dumps(seg, sort_keys=True)]
    if len(same) > 1:
        seg["n"] = same.index(i) + 1
    return seg


def chain(top, node):
    """[(node, siblings)] from the top level down to `node`, found by offsets."""
    out, level = [], top
    while level:
        hit = next((n for n in level if n.start <= node.start and node.end <= n.end), None)
        if hit is None:
            return None
        out.append((hit, level))
        if hit.start == node.start and hit.end == node.end and hit.kind == node.kind:
            return out
        level = hit.children
    return None


def path_of(top, node, dialect, cache=None):
    links = chain(top, node)
    return None if links is None else [segment(n, sibs, dialect, cache) for n, sibs in links]


def resolve(top, path, dialect):
    """(node, loose) for an address path in a tree, or (None, False). `loose` is true
    when a level matched by its key only, because its other fields changed."""
    if not path:
        return _Level(top), False
    level, node, loose = top, None, False
    for seg in path:
        if not level:
            return None, False
        want = {k: v for k, v in seg.items() if k != "n"}
        same = [n for n in level if _ident(n, dialect) == want]
        idx = seg.get("n", 1) - 1
        if 0 <= idx < len(same):
            node = same[idx]
        else:
            by_key = [n for n in level if n.key == seg["key"]]
            if len(by_key) != 1:
                return None, False
            node, loose = by_key[0], True
        level = node.children
    return node, loose


class _Level:
    """A whole level of nodes, as one node for canon(): the address [] of a copy."""
    kind, key, value = "block", "", (None, None)

    def __init__(self, children):
        self.children = list(children)


def covers(entry_path, path, scope):
    if scope == "node":
        return entry_path == path
    return path[:len(entry_path)] == entry_path


# --- deviations ---------------------------------------------------------------------

@dataclass
class Deviation:
    copy: str                    # replace, inject, file, gui_def, gui_file
    identity: str
    block: str
    content: str
    file: str
    vanilla_file: str
    line: int
    change: str
    priority: str
    path: list                   # address segments
    keys: tuple                  # the keys of the path
    mod_key: str = None
    vanilla_key: str = None
    mod_value: str = None
    vanilla_value: str = None
    mod_text: str = None
    vanilla_text: str = None
    comment: str = ""
    since: str = None
    finding: str = None
    generated: bool = False
    tool: str = None
    systems: list = field(default_factory=list)

    @property
    def id(self):
        payload = json.dumps([self.identity, self.path, self.change, self.mod_text, self.vanilla_text],
                             sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    @property
    def address(self):
        return {"identity": self.identity, "path": self.path}

    def to_json(self):
        return {"id": self.id[:12], "copy": self.copy, "identity": self.identity, "file": self.file,
                "line": self.line, "change": self.change, "address": self.address,
                "mod": self.mod_text, "vanilla": self.vanilla_text, "since": self.since,
                "systems": self.systems, "tool": self.tool}


def copy_type(copy):
    if copy.audit == "override":
        return "replace"
    if copy.audit == "inject":
        return "inject"
    if copy.audit == "file":
        return "file"
    return "gui_file" if copy.target.startswith("guifile:") else "gui_def"


def identity_of(copy):
    """Vanilla identity: the content folder and block name for script, the GUI
    target for GUI."""
    if copy.audit in ("override", "inject"):
        return copy.target.split(":", 1)[1]
    if copy.audit == "file":
        rel, _sep, name = copy.target.split(":", 1)[1].partition("#")
        return f"{rel.rsplit('/', 1)[0]}/{name}"
    return copy.target


def _comment_of(line):
    in_str = False
    for i, c in enumerate(line):
        if c == '"':
            in_str = not in_str
        elif c == "#" and not in_str:
            return line[i + 1:].strip()
    return ""


def _value(node):
    if node is None or node.kind != "stmt":
        return None
    return node.value[1]


def views(copy):
    """(mod nodes, vanilla nodes, root) of a copy at the level its addresses start.
    A REPLACE starts inside its block. A definition copy (a file definition, or a GUI
    template or type) also starts inside its one top block, because the identity
    names the definition already. root: that top block on each side, or None."""
    if copy.unwrap:
        return diff3.body(diff3.nodes(copy.mod_text)), diff3.body(diff3.nodes(copy.versions[-1])), None
    mine, theirs = diff3.nodes(copy.mod_text), diff3.nodes(copy.versions[-1])
    if (copy_type(copy) in ("file", "gui_def") and len(mine) == 1 == len(theirs)
            and mine[0].children is not None and theirs[0].children is not None):
        return mine[0].children, theirs[0].children, (mine[0], theirs[0])
    return mine, theirs, None


_PER_FILE = ("file", "vanilla_file", "line", "generated", "tool", "systems")


def _to_cache(devs, copy):
    """The deviations of one copy without what depends on its place in the mod."""
    out = []
    for d in devs:
        rec = {k: v for k, v in d.__dict__.items() if k not in _PER_FILE}
        rec["keys"] = list(d.keys)
        rec["row"] = d.line - copy.line
        out.append(rec)
    return out


def _from_cache(recs, copy, reg):
    generated, tool = reg.generated_by(copy.file) if reg is not None else (False, None)
    systems = reg.systems_of(copy.file) if reg is not None else []
    out = []
    for rec in recs:
        rec = dict(rec)
        row = rec.pop("row")
        rec["keys"] = tuple(rec["keys"])
        out.append(Deviation(file=copy.file, vanilla_file=copy.vanilla_file, line=copy.line + row,
                             generated=generated, tool=tool, systems=systems, **rec))
    return out


def deviations_of(copy, mod_root=None):
    """[Deviation] for one Copy that changes.audit compared, or that the cache gave."""
    if copy.cached is not None:
        return _from_cache(copy.cached, copy, registry(mod_root) if mod_root is not None else None)
    mod_top, van_top, root = views(copy)
    mod_lines = copy.mod_text.split("\n")
    ident = identity_of(copy)
    content, _sep, block = ident.rpartition("/")
    reg = registry(mod_root) if mod_root is not None else None
    generated, tool = reg.generated_by(copy.file) if reg is not None else (False, None)
    systems = reg.systems_of(copy.file) if reg is not None else []
    out, cache = [], {}
    for c in copy.changes:
        node, top = (c.new, van_top) if c.new is not None else (c.mod, mod_top)
        if root is not None and any(node.start == r.start and node.end == r.end for r in root):
            path = []                # the definition's own head
        else:
            path = (path_of(top, node, copy.dialect, cache)
                    or [{"key": k} for k in c.path] + [{"key": node.key}])
        if c.mod is not None:
            row = copy.mod_text.count("\n", 0, c.mod.start)
        elif c.after is not None:
            row = copy.mod_text.count("\n", 0, c.after.start)
        elif c.parent is not None:
            row = copy.mod_text.count("\n", 0, c.parent.start)
        else:
            row = 0
        comment = ""
        if c.mod is not None:
            last = copy.mod_text.count("\n", 0, max(c.mod.end - 1, c.mod.start))
            parts = [_comment_of(mod_lines[last])]
            if row > 0 and mod_lines[row - 1].strip().startswith("#"):
                parts.insert(0, mod_lines[row - 1].strip().lstrip("#").strip())
            comment = " | ".join(p for p in parts if p)
        out.append(Deviation(
            copy=copy_type(copy), identity=ident, block=block, content=content, file=copy.file,
            vanilla_file=copy.vanilla_file, line=copy.line + row, change=c.kind, priority=c.priority,
            path=path, keys=tuple(s["key"] for s in path),
            mod_key=c.mod.key if c.mod is not None else None,
            vanilla_key=c.new.key if c.new is not None else None,
            mod_value=_value(c.mod), vanilla_value=_value(c.new),
            mod_text=canon(c.mod), vanilla_text=canon(c.new), comment=comment,
            since=copy.tags[c.since] if c.since is not None else None,
            finding=copy.finding_ids.get(id(c)), generated=generated, tool=tool, systems=systems))
    return out


def inject_deviations(mod_root, base, window, overrides, reg=None):
    """[Deviation] for the children that each INJECT sets: a child vanilla's block
    does not have is mod_added, a child that differs from vanilla's child of the
    same key is mod_changed."""
    from .overrides import block_history, extract_mod_block
    from .tracker import tag_of
    injects = [o for o in overrides if "INJECT" in o["type"]]
    if not injects:
        return []
    categories = sorted({o["category"] for o in injects})
    hist = block_history(base, window, categories, {(o["category"], o["block"]) for o in injects})
    out = []
    for ov in injects:
        texts = hist.get((ov["category"], ov["block"])) or []
        if not texts or texts[-1] is None:
            continue
        mod_text = extract_mod_block(mod_root, ov)
        if mod_text is None:
            continue
        mine, theirs = diff3.body(diff3.nodes(mod_text)), diff3.body(diff3.nodes(texts[-1]))
        lines = mod_text.split("\n")
        ident = f"{ov['category']}/{ov['block']}"
        generated, tool = reg.generated_by(ov["file"]) if reg is not None else (False, None)
        systems = reg.systems_of(ov["file"]) if reg is not None else []
        for m in mine:
            same_key = [v for v in theirs if v.key == m.key]
            if any(v.sig == m.sig for v in same_key):
                continue
            v = same_key[0] if same_key else None
            node, top = (v, theirs) if v is not None else (m, mine)
            row = mod_text.count("\n", 0, m.start)
            out.append(Deviation(
                copy="inject", identity=ident, block=ov["block"], content=ov["category"], file=ov["file"],
                vanilla_file=None, line=ov["line"] + row,
                change="mod_changed" if v is not None else "mod_added", priority=diff3.INFO,
                path=path_of(top, node, diff3.SCRIPT), keys=(m.key,), mod_key=m.key,
                vanilla_key=v.key if v is not None else None, mod_value=_value(m), vanilla_value=_value(v),
                mod_text=canon(m), vanilla_text=canon(v), comment=_comment_of(lines[row]),
                since=tag_of(window[-1][1]), generated=generated, tool=tool, systems=systems))
    return out


def collect(mod_root, base, commits, new_hash, new_msg, only=None, generated=None, findings=None):
    """(copies, deviations) of the whole mod against vanilla at `new_hash`, measured
    across every tracked version up to it. The copy audits run with their output
    discarded. `only`: a set of mod paths; the other files are not read. `generated`
    ({}): receives {mod path: tool id} for the generated files whose vanilla source
    changed; the audits skip their copies. `findings` ([]): receives the findings of
    the three copy audits, for the cases that are not a copy (a block or file that
    vanilla removed, a text too large or not script)."""
    from .files import run_file_audit
    from .gui import run_gui_audit, version_window
    from .overrides import find_overrides, run_override_audit
    from .tracker import tag_of
    old_hash, old_msg = commits[-1]
    args = types.SimpleNamespace(diff=False, block=None, category=None, full=False, old=None, new=None,
                                 results_file=None)
    ctx = types.SimpleNamespace(commits=commits, new_tag=tag_of(new_msg), base=base, fixed_window=False,
                                bases={}, scanned={}, dismissed=set(), only_files=only,
                                generated=generated if generated is not None else {})
    path = _cache_path(base, new_hash)
    cache = _read_cache(path)
    with changes.collect(cache) as copies, redirect_stdout(io.StringIO()):
        found = (run_override_audit(mod_root, base, old_hash, old_msg, new_hash, new_msg, args, ctx) or [])
        found += run_gui_audit(mod_root, base, old_hash, old_msg, new_hash, new_msg, args, ctx) or []
        found += run_file_audit(mod_root, base, old_hash, old_msg, new_hash, new_msg, args, ctx) or []
    if findings is not None:
        findings += found
    devs, fresh = [], {}
    for c in copies:
        found = deviations_of(c, mod_root)
        devs += found
        if c.key:
            fresh[c.key] = c.cached if c.cached is not None else _to_cache(found, c)
    if path is not None and only is not None:
        fresh = {**cache, **fresh}                   # a part of the mod: keep the rest
    if path is not None and fresh.keys() != cache.keys():
        _write_cache(path, fresh)
    window = version_window(commits, new_hash)
    overrides = [o for o in find_overrides(mod_root) if only is None or o["file"] in only]
    devs += inject_deviations(mod_root, base, window, overrides, registry(mod_root))
    return list(copies), devs


# --- the deviation cache --------------------------------------------------------------

DEV_CACHE_VERSION = 1


def _cache_path(base, new_hash):
    """The deviation cache of one vanilla version, in the cache folder of the tracker.
    A copy's key hashes its text and vanilla's history, so an entry never goes stale;
    each run keeps only the entries of the copies it read."""
    from .tracker import cache_path, full_hash
    repo = getattr(base, "repo", None)
    full = full_hash(repo, new_hash) if repo else ""
    return cache_path(repo, f"devs-v{DEV_CACHE_VERSION}-{full}.json") if full else None


def _read_cache(path):
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(path, data):
    import os
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        tmp.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


# --- matching -----------------------------------------------------------------------

def _pat(pattern, text):
    if text is None:
        return False
    if pattern.startswith("re:"):
        return re.fullmatch(pattern[3:], text) is not None
    return fnmatch.fnmatchcase(text, pattern)


def _path_match(pattern, keys):
    """True when the node path `keys` matches `pattern`: `*` is one segment, `**` is
    any number. The empty pattern matches only the empty path: a deviation of a
    whole copy, such as a definition that vanilla added to a same-path file."""
    if pattern == "":
        return not keys
    parts = pattern.split(".")

    def go(i, j):
        if i == len(parts):
            return j == len(keys)
        if parts[i] == "**":
            return any(go(i + 1, k) for k in range(j, len(keys) + 1))
        return j < len(keys) and fnmatch.fnmatchcase(keys[j], parts[i]) and go(i + 1, j + 1)
    return go(0, 0)


def rule_matches(rule, dev):
    """True when every field of the rule's matcher matches the deviation."""
    m = rule.get("match") or {}
    checks = {
        "audit": lambda v: dev.copy in v,
        "content": lambda v: any(_pat(p, dev.content) for p in v),
        "file": lambda v: any(_pat(p, dev.file) or _pat(p, dev.vanilla_file) for p in v),
        "block": lambda v: any(_pat(p, dev.block) for p in v),
        "path": lambda v: any(_path_match(p, list(dev.keys)) for p in v),
        "vanilla_key": lambda v: any(_pat(p, dev.vanilla_key) for p in v),
        "mod_key": lambda v: any(_pat(p, dev.mod_key) for p in v),
        "vanilla_value": lambda v: any(_pat(p, dev.vanilla_value) for p in v),
        "mod_value": lambda v: any(_pat(p, dev.mod_value) for p in v),
        "name": lambda v: any(re.search(rf"(?<![\w.]){re.escape(p)}(?![\w])", t or "")
                              for p in v for t in (dev.mod_text, dev.vanilla_text)),
        "comment": lambda v: any(re.search(p[3:] if p.startswith("re:") else re.escape(p), dev.comment)
                                 for p in v),
        "change": lambda v: dev.change in v,
        "vanilla_absent": lambda v: (dev.vanilla_text is None) == v,
    }
    return all(checks[k](v) for k, v in m.items())


@dataclass
class Attribution:
    by: str = None               # "rule:<id>" or "entry:<id>"
    disposition: str = None
    system: str = None
    conflict: list = None        # rule ids that disagree


def attribute(dev, intent, states=None):
    """The rule or the entry that explains a deviation, or None. An entry wins over a
    rule; of two entries the one with the longer address wins. Only a `recorded`
    entry counts, when `states` gives the entry states."""
    best = None
    for e in intent["entries"].values():
        addr = e["address"]
        if addr["identity"] != dev.identity or not covers(addr["path"], dev.path, e.get("scope", "subtree")):
            continue
        if states is not None and states.get(e["id"], ("recorded",))[0] != "recorded":
            continue
        if best is None or len(addr["path"]) > len(best["address"]["path"]):
            best = e
    if best is not None:
        return Attribution(f"entry:{best['id']}", best.get("disposition"), best["system"])
    hits = [r for r in intent["rules"].values() if rule_matches(r, dev)]
    if not hits:
        return None
    disps = {r.get("disposition") for r in hits}
    if len(disps) > 1:
        return Attribution(None, None, None, conflict=sorted(r["id"] for r in hits))
    r = hits[0]
    return Attribution(f"rule:{r['id']}", r.get("disposition"), r["system"])


# --- entry states -------------------------------------------------------------------

def entry_states(intent, copies, devs):
    """{entry id: (state, detail)}. recorded: the address resolves on both sides as
    the user saw it. stale: vanilla or the mod changed the node since. lost: the
    address resolves on neither side."""
    wanted = {e["address"]["identity"] for e in intent["entries"].values()}
    trees = {}
    for c in copies:
        ident = identity_of(c)
        if ident in wanted:
            mine, theirs, _root = views(c)
            trees[ident] = (mine, theirs, c.dialect)
    inject_text = {}
    for d in devs:
        if d.copy == "inject":
            inject_text.setdefault((d.identity, json.dumps(d.path, sort_keys=True)), d)
    out = {}
    for e in intent["entries"].values():
        addr, seen = e["address"], e.get("seen") or {}
        if addr["identity"] in trees:
            mine, theirs, dialect = trees[addr["identity"]]
            m, mloose = resolve(mine, addr["path"], dialect)
            v, vloose = resolve(theirs, addr["path"], dialect)
            mod_sig, van_sig = digest(canon(m)), digest(canon(v))
        else:
            d = inject_text.get((addr["identity"], json.dumps(addr["path"], sort_keys=True)))
            if d is None:
                out[e["id"]] = ("lost", "the copy or the node is gone")
                continue
            m, v, mloose, vloose = True, d.vanilla_text, False, False
            mod_sig, van_sig = digest(d.mod_text), digest(d.vanilla_text)
        if m is None and v is None:
            out[e["id"]] = ("lost", "the node is gone on both sides")
            continue
        sides = []
        if van_sig != seen.get("vanilla_sig") or vloose:
            sides.append("vanilla")
        if mod_sig != seen.get("mod_sig") or mloose:
            sides.append("mod")
        out[e["id"]] = ("stale", " and ".join(sides) + " changed") if sides else ("recorded", "")
    return out


def entry_from(dev, entry_id, system, reason, source, disposition, scope="subtree", depth=None,
               today=None):
    """An entry for a deviation; `depth` cuts the address to that many segments."""
    path = dev.path[:depth] if depth else dev.path
    return {"id": entry_id, "address": {"identity": dev.identity, "path": path}, "scope": scope,
            "system": system, "reason": reason, "source": source, "disposition": disposition,
            "seen": None, "created": today, "confirmed_by": "user"}


def seal(entry, copies, devs):
    """Fill `seen` with the current hashes of the entry's node on both sides."""
    for c in copies:
        if identity_of(c) != entry["address"]["identity"]:
            continue
        mine, theirs, _root = views(c)
        m, _l = resolve(mine, entry["address"]["path"], c.dialect)
        v, _l = resolve(theirs, entry["address"]["path"], c.dialect)
        entry["seen"] = {"vanilla": c.tags[-1], "mod_sig": digest(canon(m)), "vanilla_sig": digest(canon(v))}
        return entry
    for d in devs:
        if d.identity == entry["address"]["identity"] and d.path == entry["address"]["path"]:
            entry["seen"] = {"vanilla": d.since, "mod_sig": digest(d.mod_text),
                             "vanilla_sig": digest(d.vanilla_text)}
            return entry
    return entry


def run_collect(mod_root, base, commits, new_hash, new_msg, only=None):
    """collect() inside a session run, so the parsed texts are shared."""
    with session.run():
        return collect(mod_root, base, commits, new_hash, new_msg, only)
