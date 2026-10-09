"""`pdx-audit merge`: take vanilla patch changes into the mod's copies.

A dry run writes nothing into the mod. It prints the full diff of each file, the
decisions, and the removed-line check, and it saves a plan in the per-user data
folder. `--apply` takes that plan: it writes a file only when the file still reads as
it did at the dry run, its removed-line check passed, it has no open decision and
it used no stale entry. So no mod file changes without a reviewed dry run. After the
write, `--apply` reads each file back and plans it again with the same versions. A
change that the file took must not be a decision again; a file that fails this check
gets its old text back (check_applied).

Each file of the plan holds a gathered part that no choice changes: the mod's text,
the edits of each decision that takes vanilla's text, and the result of each choice
for each decision. `build` makes the merged text, the decisions and the checks from
the gathered part and the user's choices. The dry run, `--apply` with choices and the
app's Merge page all call `build`, so a choice gives the same file everywhere.

`--choices` names a file of the user's choices, one for each decision (see
choice_key): "take" takes vanilla's text, "keep" keeps the mod's. A file's "all"
choice applies to each decision of the file that has no choice of its own, and
`--choose` gives that choice to every file. A file's "own" list holds the user's own
texts: each one takes the place of a span of the mod file, with each change in it
(see own_place). The app writes the choices file. A choice applies only while its mod
file reads as it did when the user chose.

The `merge_default` setting gives the action of a change with no conflict, a change
that the merge takes with no rule and no intent store entry: "take" takes it, "ask"
makes it a decision for the user, "keep" keeps the mod's text (set_defaults). Each
decision has a cause, which names why it can be open (CAUSES)."""
import argparse
import bisect
import datetime
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

from . import config, diff3, intent, merge, proposer, session
from .registry import registry


# The base version of a text that vanilla did not hold at --old.
NO_BASE = "(none)"
# The choices a user can make for a decision.
CHOICES = (merge.TAKE, merge.KEEP)
# The file in the store that keeps the app's choices between sessions (see saved_choices).
SAVED_CHOICES = "merge-choices.json"


def is_choice(value):
    """True for a node choice: "take" or "keep"."""
    return value in CHOICES


def own_entries(value, text):
    """The valid own texts of a file record's "own" list for the mod text `text`:
    [{"span": [start, end], "text": your text, "edges": [before, after]}] in the order
    of the file. An entry whose span is not in the file, or overlaps an earlier entry,
    is dropped. `edges` (default [true, true]) tells merge.splice whether the text
    touches the blank run above it and below it (see merge_rows.own_block)."""
    out, end = [], 0
    rows = [o for o in value or () if isinstance(o, dict) and isinstance(o.get("text"), str)
            and isinstance(o.get("span"), list) and len(o["span"]) == 2
            and all(isinstance(x, int) for x in o["span"])]
    for o in sorted(rows, key=lambda o: tuple(o["span"])):
        s, e = o["span"]
        if 0 <= s <= e <= len(text) and s >= end and not (out and out[-1]["span"] == [s, e]):
            edges = o.get("edges")
            edges = [bool(x) for x in edges] if isinstance(edges, list) and len(edges) == 2 else [True, True]
            out.append({"span": [s, e], "text": o["text"], "edges": edges})
            end = e
    return out


def own_problem(text):
    """Why your own text cannot go into the file, or "": its blocks must close."""
    from pdx_utilities.script_parser import tokenize
    depth = 0
    for t in tokenize(text):
        if t["type"] == "op" and t["val"] in "{}":
            depth += 1 if t["val"] == "{" else -1
            if depth < 0:
                return f"line {t['line']} of your text closes a block that it did not open"
    return f"your text leaves {depth} block{'' if depth == 1 else 's'} open" if depth else ""


def inside(span, s, e):
    """True when the edit of the mod text at `span` falls in the place s..e of an own
    text: an insertion strictly inside it (or at it, for an empty place), another
    edit within it."""
    a, b = span
    if a == b:
        return s < a < e or s == a == e
    return s <= a and b <= e


def _sha(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def choice_key(decision):
    """The key of a decision in a choices file."""
    return _key(decision["address"]["identity"], decision["address"]["path"], decision["kind"])


def _key(identity, path, kind):
    return json.dumps([identity, path, kind], sort_keys=True, ensure_ascii=False)


def read_choices(path, mod_root, every=None, saved=None):
    """{mod path: {"all": action or None, "nodes": {choice key: choice}}} from a choices
    file, for the files that still read as they did when the user chose. `every`
    (--choose) is the choice for each decision that no node choice names, in every
    file; the key "*" holds it. `saved` (saved_choices' result) gives the choices of
    the files that the choices file does not name."""
    out = {"*": {"all": every, "nodes": {}}} if every in CHOICES else {}
    files = dict((saved or {}).get("files") or {})
    if path:
        files.update(json.loads(Path(path).read_text(encoding="utf-8")).get("files") or {})
    for rel, rec in files.items():
        f = Path(mod_root) / rel
        text = read_mod_file(f)[0] if f.is_file() else None
        if text is not None and _sha(text) == rec.get("sha"):
            out[rel] = {"all": rec.get("all") if rec.get("all") in CHOICES else None,
                        "nodes": {k: v for k, v in (rec.get("nodes") or {}).items() if is_choice(v)}}
            if own_entries(rec.get("own"), text):
                out[rel]["own"] = own_entries(rec.get("own"), text)
    return out


def saved_choices(store, old, new):
    """{"old", "new", "files"}: the choices that the app saved for --old and --new, in the
    format of a choices file. Empty when it saved none for these versions."""
    path = store.dir / SAVED_CHOICES
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or (data.get("old"), data.get("new")) != (old, new):
        data = {}
    return {"old": old, "new": new, "files": data.get("files") or {}}


def save_choices(store, old, new, files):
    """Keep the choices `files` ({mod path: {"sha", "all", "nodes"}}) for --old and --new,
    in place of the choices saved before. No choices: the saved file is deleted."""
    from .safety import remove_file
    path = store.dir / SAVED_CHOICES
    files = {rel: rec for rel, rec in files.items() if rec.get("all") or rec.get("nodes") or rec.get("own")}
    if not files:
        if path.is_file():
            remove_file(path, store.dir, re.escape(SAVED_CHOICES))
        return
    store.dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"old": old, "new": new, "files": files}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)


def file_choices(choices, rel):
    """The choices for the mod file `rel` (see read_choices), or None when it has none."""
    rec, every = (choices or {}).get(rel), (choices or {}).get("*")
    if every:
        rec = dict({"all": every["all"], "nodes": (rec or {}).get("nodes") or {}},
                   **({"own": rec["own"]} if (rec or {}).get("own") else {}))
    return rec if rec and (rec.get("all") or rec.get("nodes") or rec.get("own")) else None


def read_mod_file(path):
    """(text with \\n line ends, bom, crlf)."""
    raw = Path(path).read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    crlf = "\r\n" in text
    return text.replace("\r\n", "\n"), bom, crlf


def write_mod_file(path, text, bom, crlf):
    data = (text.replace("\n", "\r\n") if crlf else text).encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    restore_file(path, data)


def restore_file(path, data):
    """Write the bytes `data` to `path` in one step, with the file's mode."""
    import shutil
    tmp = Path(path).with_name(f".{Path(path).name}.pdx-audit-{os.getpid()}")
    tmp.write_bytes(data)
    if Path(path).exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def build_parser():
    ap = argparse.ArgumentParser(prog="pdx-audit merge",
                                 description="Merge vanilla patch changes into the mod's copies.")
    ap.add_argument("--mod-root")
    ap.add_argument("--vanilla-repo")
    ap.add_argument("--old", required=True, help="the version the copies were ported to")
    ap.add_argument("--new", required=True, help="the version to merge in")
    ap.add_argument("--file", help="only the copies in this mod file; with --apply, only this file")
    ap.add_argument("--block", help="only the copies of this block or definition name")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", metavar="PLAN", help="the plan file a dry run wrote")
    mode.add_argument("--clear-saved", action="store_true",
                      help="delete the choices that the app saved for --old and --new")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="print the counts and the plan's path, not the diffs")
    ap.add_argument("--plan-out", help="where the dry run writes its plan")
    ap.add_argument("--choices", help="the user's choices for single decisions, a JSON file")
    ap.add_argument("--saved", action="store_true",
                    help="use the choices that the app saved for --old and --new; --choices names the "
                         "files it changes")
    ap.add_argument("--choose", choices=CHOICES,
                    help="this choice for each decision that --choices does not name: take vanilla's text, "
                         "or keep the mod's; use --file to choose for one file")
    return ap


def main(argv):
    args = build_parser().parse_args(argv)
    with session.run():
        return _main(args)


def _apply_intent(dev, it, states, what, stale):
    """(action, by) for one deviation. `what` is "conflict" for a node both sides
    changed. Without a rule or an entry, a vanilla change is taken and a conflict is
    open. A stale entry, a grouping rule (no disposition), a banned rule and two rules
    that disagree make the node an open decision; `stale` receives each stale entry."""
    a = intent.attribute(dev, it)
    if a is None:
        return (merge.OPEN, None) if what == "conflict" else (merge.TAKE, None)
    if a.conflict:
        return merge.OPEN, "conflict:" + ",".join(a.conflict)
    if a.by.startswith("entry:") and states.get(a.by[6:], ("recorded",))[0] != "recorded":
        stale.add(a.by)
        return merge.OPEN, a.by
    if a.disposition == "keep_mod":
        return merge.KEEP, a.by
    if a.disposition == "take_vanilla":
        return merge.TAKE, a.by
    if a.disposition == "merge" and what != "conflict":
        return merge.TAKE, a.by
    return merge.OPEN, a.by           # merge on a statement, banned, or a grouping


def _decider(dev_template, it, states):
    """decide(path, kind, ours node, theirs node, theirs text, what) for one copy, from
    the intent store. The user's choices apply later, in build."""
    def decide(path, kind, o, t, _tt, what):
        path = path[dev_template.root_depth:]
        dev = intent.Deviation(
            copy=dev_template.copy, identity=dev_template.identity, block=dev_template.block,
            content=dev_template.content, file=dev_template.file, vanilla_file=dev_template.vanilla_file,
            line=0, change=kind, priority=diff3.MID, path=path, keys=tuple(s["key"] for s in path),
            mod_key=o.key if o is not None else None, vanilla_key=t.key if t is not None else None,
            mod_value=intent._value(o), vanilla_value=intent._value(t),
            mod_text=intent.canon(o), vanilla_text=intent.canon(t),
            comment=dev_template.comments.get(o.start, "") if o is not None else "",
            systems=dev_template.systems)
        return _apply_intent(dev, it, states, what, dev_template.stale)
    return decide


def _take_all(*_args):
    """A decide callback that takes vanilla's text for each decision, as a choice of
    the user's: a choice skips the intent store and the gates of the merge."""
    return merge.TAKE, "choice"


def owner(rel, it, reg):
    """What owns the mod file `rel`, or None: a pdx-maint system that lists the file,
    or a rule whose `content` or `file` field selects it. A system rewrote such a file
    for its own design, so a definition that vanilla adds to it waits for the user."""
    systems = reg.systems_of(rel)
    if systems:
        return "system " + ", ".join(systems)
    content = rel.rsplit("/", 1)[0]
    for r in sorted(it["rules"].values(), key=lambda r: r["id"]):
        m = r.get("match") or {}
        if any(intent._pat(p, content) for p in m.get("content", ())) or \
                any(intent._pat(p, rel) for p in m.get("file", ())):
            return f"rule {r['id']}"
    return None


def decide_definition(rel, label, kind, theirs_text, it, states, reg, stale, chosen=None):
    """(action, by, reason) for a top-level definition of a same-path script file that
    vanilla added after --old (kind vanilla_added) or changed after the mod deleted it
    (kind removed_changed). The deviation has the definition as its identity and an
    empty node path, so a rule matches it by `change`, `content`, `file`, `block`
    (the definition name) and `vanilla_key`, and an entry by its address. Without a
    rule or an entry, a new definition is taken unless a system owns the file (see
    `owner`); a deleted definition that vanilla changed is always open."""
    content = rel.rsplit("/", 1)[0]
    choice = (chosen or {}).get(_key(f"{content}/{label}", [], kind))
    if choice:
        return choice, "choice", ("the user took vanilla's definition" if choice == merge.TAKE
                                  else "the user keeps the mod's file without it")
    dev = intent.Deviation(
        copy="file", identity=f"{content}/{label}", block=label, content=content, file=rel,
        vanilla_file=rel, line=0, change=kind, priority=diff3.MID, path=[], keys=(),
        vanilla_key=label.split(" ", 1)[0], vanilla_text=theirs_text, systems=reg.systems_of(rel))
    a = intent.attribute(dev, it)
    if a is None:
        if kind == "removed_changed":
            return merge.OPEN, None, "the mod deleted this definition and vanilla changed it"
        who = owner(rel, it, reg)
        if who:
            return merge.OPEN, None, f"vanilla added this definition after --old; no rule decides it, and {who} owns the file"
        return merge.TAKE, None, "vanilla added this definition after --old"
    action, by = _apply_intent(dev, it, states, "insert", stale)
    why = {merge.TAKE: "vanilla's definition goes in", merge.KEEP: "the mod's file stays without it",
           merge.OPEN: "the rule or entry leaves it to the user"}[action]
    return action, by, f"{kind.replace('_', ' ')}: {why}"


class _Template:
    def __init__(self, copy, mod_root, reg):
        self.copy = intent.copy_type(copy)
        self.identity = intent.identity_of(copy)
        self.content, _sep, self.block = self.identity.rpartition("/")
        self.file, self.vanilla_file = copy.file, copy.vanilla_file
        self.systems = reg.systems_of(copy.file)
        self.comments = {}
        self.stale = set()
        # The intent store addresses a definition copy (a file definition, a GUI
        # template or type) from inside its one top block (intent.views). The merge
        # walks the copy's text from the top, so its paths carry that block first.
        self.root_depth = 1 if not copy.unwrap and intent.views(copy)[2] is not None else 0
        text = copy.mod_text
        for n in _walk(diff3.nodes(text)):
            line = text[text.rfind("\n", 0, n.start) + 1:merge._line_end(text, n.end)]
            self.comments[n.start] = intent._comment_of(line)


def _walk(nodes):
    for n in nodes:
        yield n
        if n.children:
            yield from _walk(n.children)


def gui_templates(mod_root, base, new_hash):
    """{template name: {key: value}} of the GUI templates the game loads at `new_hash`:
    vanilla's, with the mod's own template in place of vanilla's of the same name."""
    from .gui import mod_gui_files
    from .tracker import MODULE_ROOTS
    _defs, files, _bad = base.gui_index(new_hash, MODULE_ROOTS)
    out = {}
    for _rel, text in sorted(files.items()):
        for name, keys in merge.template_keys(text).items():
            out.setdefault(name, keys)
    for _rel, text in mod_gui_files(mod_root):
        out.update(merge.template_keys(text))
    return out


def plan(mod_root, base, commits, old_tag, new_hash, new_msg, it, file=None, block=None, choices=None,
         bases=None):
    """{"files": [...], "regenerate": [...], "skipped": [...]} for a dry run. Each file
    holds its gathered part (see build) and the merge that `build` makes from it with
    `choices` (read_choices' result). `file`: one mod path, or a set of them. `bases`
    ({(mod path, copy name): base version}) gives a copy its base in place of the
    version nearest to its text (see check_applied)."""
    from .tracker import tag_of
    reg = registry(mod_root)
    gen = {}
    from .gui import version_window
    order = [tag_of(m) for _h, m in version_window(commits, new_hash)]
    if old_tag not in order[:-1]:
        raise ValueError(f"--old {old_tag} is not a tracked version before the new one "
                         f"({', '.join(order[:-1])})")
    findings = []
    only = {file} if isinstance(file, str) else set(file) if file else None
    copies, devs = intent.collect(mod_root, base, commits, new_hash, new_msg, only, gen,
                                  findings)
    states = intent.entry_states(it, copies, devs)
    by_file = defaultdict(list)
    regenerate, skipped = dict(gen), []
    for c in copies:
        if only and c.file not in only:
            continue
        if block and c.name != block and not intent.identity_of(c).endswith("/" + block):
            continue
        generated, tool = reg.generated_by(c.file)
        if generated:
            regenerate[c.file] = tool
            continue
        if old_tag not in c.tags:
            skipped.append((c.file, untracked_reason(c.file, old_tag)))
            continue
        old_i = c.tags.index(old_tag)
        pinned = (bases or {}).get((c.file, c.name))
        if pinned == NO_BASE:
            base_i = None
        elif pinned in c.tags[:old_i + 1]:
            base_i = c.tags.index(pinned)
        else:
            base_i = diff3.baseline(c.mod_text, c.versions[:old_i + 1], c.unwrap, c.dialect)
        theirs = c.versions[-1]
        if base_i is None:
            # Vanilla held no version of this text at --old or before: vanilla added it
            # later. The base is empty, so each node vanilla holds is its addition.
            base_text, base_tag = "", NO_BASE
        else:
            base_text, base_tag = c.versions[base_i], c.tags[base_i]
        # Vanilla changed nothing since the base. The mod can still hold vanilla's text
        # from a version before the base (merge._Merge.older_change), so such a copy
        # merges too.
        if base_text == theirs and not any(v and v != base_text for v in c.versions[:base_i or 0]):
            continue
        later = c.versions[(base_i + 1 if base_i is not None else 0):-1]
        by_file[c.file].append((c, base_tag, base_text, theirs, later))
    _add_injects(mod_root, base, commits, new_hash, old_tag, only, block, reg, by_file, regenerate, skipped)
    added = _new_definitions(mod_root, base, commits, new_hash, old_tag, only, block, reg, regenerate, it,
                             states, skipped)
    whole = _whole_copies(mod_root, findings, order, old_tag, only, block, it, states, reg, skipped)
    files, templates = [], None
    for rel in sorted(set(by_file) | set(added) | set(whole)):
        text, bom, crlf = read_mod_file(Path(mod_root) / rel)
        starts = _line_starts(text)
        new_rows, stale = added.get(rel, ({"decisions": [], "inserts": []}, set()))
        removed, w_stale = whole.get(rel, ([], set()))
        g = {"ours": text, "copies": [], "added": new_rows, "removed": removed}
        stale = stale | w_stale
        for c, base_tag, base_text, theirs, later in by_file.get(rel, []):
            start = _locate(text, c, starts)
            if start is None:
                g["copies"].append({"fixed": _not_merged(
                    rel, c.name, c.line, base_tag, f"the copy {c.name} at line {c.line} does not read as the audit "
                    "read it", skipped)})
                continue
            if c.dialect == diff3.GUI and c.audit != "inject" and templates is None:
                templates = gui_templates(mod_root, base, new_hash)
            tpl = _Template(c, mod_root, reg)
            g["copies"].append(_gather_copy(text, starts, start, c, base_tag, base_text, theirs, later, old_tag,
                                            templates, tpl, it, states))
            stale |= tpl.stale
        g["stale"] = sorted(stale)
        g["blame"] = _blame(mod_root, rel, g)
        files.append({"file": rel, "before_sha": _sha(text), "bom": bom, "crlf": crlf, "gathered": g})
    ours_layout(files)
    set_defaults(files, config.setting("merge_default")[0])
    skipped += build(files, choices)
    # A tool's output that the merge met but cannot compare goes to the regenerate
    # list, never to the skipped list: the tool reads vanilla again.
    for f, _w in skipped:
        generated, tool = reg.generated_by(f)
        if generated:
            regenerate.setdefault(f, tool)
    return {"old": old_tag, "new": tag_of(new_msg), "files": files,
            "regenerate": [{"file": f, "tool": t, "hint": reg.regenerate_hint(t)} for f, t in sorted(regenerate.items())],
            "skipped": _distinct_skips([(f, w) for f, w in skipped if f not in regenerate])}


# Why a decision can be open, in the words of a row.
CAUSES = {"conflict": "Merge Conflict", "review": "Needs Review", "clean": "No Conflict"}
CONFLICT_KINDS = {"both_changed", "removed_changed", "both_added", "inject_overlap"}


def gathered_rows(g):
    """Each gathered decision of the gathered part `g` of a plan file."""
    for cp in g.get("copies", ()):
        yield from cp.get("decisions", ())
    yield from (g.get("added") or {}).get("decisions", ())
    yield from g.get("removed", ())


def set_defaults(files, default):
    """Give each gathered decision of plan files `files` its cause (CAUSES), and give a
    change with no conflict the action of `default`, the merge_default setting. A change
    with no conflict is one that the merge takes with no rule and no intent store entry,
    and that a choice can change. "conflict": you and vanilla both changed the text.
    "review": the merge cannot take vanilla's change safely, and opens it."""
    reason = {merge.OPEN: "a change with no conflict; the merge_default setting asks you",
              merge.KEEP: "a change with no conflict; the merge_default setting keeps your text"}
    for f in files:
        for row in gathered_rows(f["gathered"]):
            d = row["d"]
            clean = d["action"] == merge.TAKE and not d.get("by") and row["take"] is not None
            d["cause"] = ("conflict" if d["kind"] in CONFLICT_KINDS else "clean" if clean else "review")
            action = {"ask": merge.OPEN, "keep": merge.KEEP}.get(default)
            if clean and action:
                d["action"], d["reason"] = action, reason[action]


def _row(d, take=None, keep=None):
    """A gathered decision: the decision as the intent store decides it, its choice
    key, and [action, by, reason] when the user takes vanilla's text and when the user
    keeps the mod's text (None when the merge asks no choice for it)."""
    return {"d": d, "key": choice_key(d) if d.get("address") else None, "take": take, "keep": keep}


def _variants(took):
    """(take, keep) for one decision of a copy's merge (see _row). `took` is the
    decision from the merge that takes vanilla's text for each decision as a choice of
    the user's (_take_all). A choice skips the intent store and the gates, so the
    decision of a "keep" choice differs from it only in its action. A decision that
    never asks decide (a block the mod moved, which holds vanilla's new text) takes no
    choice."""
    if took.by != "choice":
        return None, None
    return [took.action, "choice", took.reason], [merge.KEEP, "choice", took.reason]


def _paired(decisions, ops, took, all_ops):
    """True when the two runs of a copy's merge make the same decisions, and the ops of
    the first run are the ops of the second run that it takes (see merge.merge_ops)."""
    if [(d.path, d.kind, d.line) for d in decisions] != [(d.path, d.kind, d.line) for d in took]:
        return False
    taken = {i for i, d in enumerate(decisions) if d.action == merge.TAKE}
    sig = lambda op: (op.start, op.end, op.text, op.removes, op.decision)   # noqa: E731
    return [sig(op) for op in ops] == [sig(op) for op in all_ops if op.decision is None or op.decision in taken]


def _gather_copy(text, starts, start, c, base_tag, base_text, theirs, later, old_tag, templates, tpl, it, states):
    """The gathered part of one copy: its place in the file, its own text when the file
    holds other definitions in its blanks, the ops of its merge in the order the merge
    makes them, and its decisions (_row). The merge runs two times: with the intent
    store, and with vanilla's text taken for each decision (_take_all). An op row is
    [start, end, text, removes, decision index or None, why]; `why` is None for an op
    that only a choice takes."""
    if c.audit == "inject":
        run = lambda decide: merge.inject_ops(base_text, c.mod_text, theirs, decide)   # noqa: E731
    else:
        old = None
        if base_tag != NO_BASE and old_tag in c.tags:
            old = (c.versions[c.tags.index(old_tag)] or "", old_tag)
        older = c.versions[:c.tags.index(base_tag)] if base_tag in c.tags else ()
        gui = templates if c.dialect == diff3.GUI else None
        run = lambda decide: merge.merge_ops(base_text, c.mod_text, theirs, c.dialect, c.unwrap,   # noqa: E731
                                             decide, later, old, older, gui)
    (decisions, ops), (took, all_ops) = run(_decider(tpl, it, states)), run(_take_all)
    first_line = bisect.bisect_right(starts, start) - 1
    for d in decisions + took:
        d.path = d.path[tpl.root_depth:]          # the intent store's address
        d.address = {"identity": tpl.identity, "path": d.path}
        if d.line is not None:
            d.line += first_line
    rows, op_rows = copy_parts(decisions, ops, took, all_ops, {"copy": c.name, "base_version": base_tag})
    own = text.startswith(c.mod_text, start)
    return {"start": start, "size": len(c.mod_text), "first_line": first_line, "name": c.name, "line": c.line,
            "base": base_tag, "text": None if own else c.mod_text, "inject": c.audit == "inject",
            "ops": op_rows, "decisions": rows}


def copy_parts(decisions, ops, took, all_ops, extra):
    """(gathered decisions, op rows) of one copy from the two runs of its merge: with
    the intent store (`decisions`, `ops`) and with vanilla's text taken for each
    decision (`took`, `all_ops`). `extra`: fields for each decision. When the runs do
    not pair, no decision takes a choice and the ops are those of the first run."""
    paired = _paired(decisions, ops, took, all_ops)
    rows = [_row(dict(d.to_json(), **extra), *(_variants(took[i]) if paired else (None, None)))
            for i, d in enumerate(decisions)]
    if paired:
        # The why of an op that the intent store takes; a choice's op says "choice".
        why = {op.decision: op.why for op in ops if op.decision is not None}
        op_rows = [[op.start, op.end, op.text, op.removes, op.decision,
                    op.why if op.decision is None else why.get(op.decision)] for op in all_ops]
    else:
        op_rows = [[op.start, op.end, op.text, op.removes, op.decision, op.why] for op in ops]
    return rows, op_rows


def _blame(mod_root, rel, g):
    """{line: commit} for each line of the file that a decision on it can leave open,
    with any choice."""
    lines = set()
    for cp in g["copies"]:
        if "fixed" in cp:
            lines.add(cp["fixed"].get("line"))
            continue
        if cp["text"] is not None:
            lines.add(cp["line"])                # the line of a copy the merge cannot splice
        for r in cp["decisions"]:
            if merge.OPEN in (r["d"]["action"], (r["take"] or [None])[0]):
                lines.add(r["d"].get("line"))
    lines |= {r["d"].get("line") for r in g["removed"]}
    lines.discard(None)
    blamed = proposer.blame(mod_root, rel, lines) if lines else {}
    return {str(ln): (hit or (None,))[0] for ln, hit in blamed.items()}


def build(files, choices=None, memo=None):
    """Make each plan file from its gathered part and the user's choices (read_choices'
    result): the merged text, the decisions, the open count, the removed-line check,
    the stale entries, the layout and the diff. The dry run, --apply with choices and
    the app's Merge page all call it. `memo` ({}) keeps the merge of each copy for the
    next call, so a call after one choice merges only that choice's copy again.
    Returns the skipped rows (file, why) that the choices make."""
    skipped = []
    for f in files:
        skipped += _build_file(f, file_choices(choices, f["file"]), memo)
    lay_out(files)
    for f in files:
        f["diff"] = merge.unified(f["file"], f["gathered"]["ours"], f["merged"])
    return skipped


def _chosen(row, rec, missed):
    """(decision, source) for a gathered decision and the file's choices `rec`. source:
    "own" when the user's choice for this decision decides it, "all" when the file's
    choice for all decisions decides it, None when the intent store decides it.
    `missed` receives the reason when the file's choice for all decisions does not give
    the decision that action."""
    d = dict(row["d"])
    nodes = (rec or {}).get("nodes") or {}
    own = nodes.get(row["key"]) if row["key"] else None
    choice = own or (rec or {}).get("all")
    variant = row.get(choice) if choice else None
    if variant:
        d["action"], d["by"] = variant[0], variant[1]
        if variant[2]:
            d["reason"] = variant[2]
        else:
            d.pop("reason", None)
    if choice and not own and d["action"] != choice:
        missed.append(_why_not(d, variant))
    return d, ("own" if own else "all") if variant else None


def _why_not(d, variant):
    """Why a decision does not take the file's choice for all decisions."""
    if d["kind"] == "not_merged":
        return "the merge cannot splice the copy into the file; merge it by hand"
    if variant is None:
        return d.get("reason") or "the merge asks no choice for this change; merge it by hand"
    return d.get("reason", "").split("; ", 1)[-1]


def _copy_merge(text, cp, rows, memo, at):
    """merge.finish for one copy, with the ops of the decisions in `rows` that take
    vanilla's text. `memo` keeps the last result of each copy under the key `at`."""
    taken = [d["action"] == merge.TAKE for d, _chosen in rows]
    sig = tuple(zip(taken, (chosen for _d, chosen in rows)))
    hit = memo.get(at) if memo is not None else None
    if hit is not None and hit[0] == sig:
        return hit[1]
    ops = [merge.Op(s, e, t, "choice" if i is not None and rows[i][1] else why, removes, i)
           for s, e, t, removes, i, why in cp["ops"] if i is None or taken[i]]
    ours = cp["text"] if cp["text"] is not None else text[cp["start"]:cp["start"] + cp["size"]]
    res = merge.finish(ours, ops, inject=cp["inject"])
    if memo is not None:
        memo[at] = (sig, res)
    return res


def file_edits(f, rec, memo=None):
    """The merge of plan file `f` with the choices `rec` (file_choices' result), before
    the user's own texts: {"edits": [(start, end, text)] in the mod text, "decisions",
    "spans": [(decision, [span])], "unexplained", "skipped", "missed", "starts"}. The
    spans of a decision are the parts of the mod text that its ops or its deletion
    replace, in either action. `missed` leaves out each decision that an own text
    holds."""
    g, rel = f["gathered"], f["file"]
    text = g["ours"]
    owns = own_entries((rec or {}).get("own"), text)
    held = lambda spans: any(inside(sp, *o["span"]) for sp in spans for o in owns)   # noqa: E731
    edits, decisions, unexplained, skipped, missed, spans = [], [], [], [], [], []
    starts = memo.setdefault((rel, "starts"), _line_starts(text)) if memo is not None else _line_starts(text)
    def at(offset):
        """The line that text put in at `offset` comes before: the offset's line, or the
        next line when the offset follows text on its line."""
        line = bisect.bisect_right(starts, offset)
        return line + 1 if text[starts[line - 1]:offset].strip() else line
    for k, cp in enumerate(g["copies"]):
        if "fixed" in cp:
            decisions.append(dict(cp["fixed"]))
            if (rec or {}).get("all"):
                missed.append(_why_not(cp["fixed"], None))
            continue
        start = cp["start"]
        op_spans = defaultdict(list)
        for s_, e_, _t, _removes, i, _why in cp["ops"]:
            if i is not None:
                op_spans[i].append([start + s_, start + e_])
        mine = []
        rows = [_chosen(r, rec, [] if held(op_spans[i]) else mine) for i, r in enumerate(cp["decisions"])]
        res = _copy_merge(text, cp, rows, memo, (rel, k))
        # An op must not touch a character that the copy holds blank for another
        # definition. Such an op would overwrite that definition.
        if cp["text"] is not None and any(text[start + op.start:start + op.end] != cp["text"][op.start:op.end]
                                          for op in res.ops):
            decisions.append(_not_merged(rel, cp["name"], cp["line"], cp["base"],
                                         f"the merge of {cp['name']} at line {cp['line']} touches another "
                                         "definition", skipped))
            if (rec or {}).get("all"):
                missed.append(_why_not(decisions[-1], None))
            continue
        missed += mine
        # Each decision's op, in either action: `span`, the offsets of your file that it
        # replaces, and `vanilla`, the text it puts there, as the merge writes it. A node
        # that your file does not hold has no line; `at` is the line where the merge
        # puts it.
        for s_, e_, t_, _removes, i, _why in cp["ops"]:
            if i is not None:
                d = rows[i][0]
                d["span"], d["vanilla"] = [start + s_, start + e_], t_
                if not d.get("line"):
                    d["at"] = at(start + s_)
        decisions += [d for d, _source in rows]
        spans += [(d, op_spans[i]) for i, (d, _source) in enumerate(rows) if op_spans[i]]
        unexplained += [(ln + cp["first_line"], s) for ln, s in res.unexplained]
        # The merge splices each op, not the whole copy. A copy of a repeated key
        # spans the definitions between its blocks, and they must stay.
        edits += [(start + op.start, start + op.end, op.text) for op in res.ops]
    places = {i: pos for pos, _ins, i in g["added"]["inserts"]}
    added = [_chosen(r, rec, [] if i in places and held([[places[i]] * 2]) else missed)
             for i, r in enumerate(g["added"]["decisions"])]
    for pos, ins, i in g["added"]["inserts"]:
        added[i][0]["span"], added[i][0]["vanilla"] = [pos, pos], ins
        spans.append((added[i][0], [[pos, pos]]))
        if not added[i][0].get("line"):
            added[i][0]["at"] = at(pos)
    put = {i for i, (d, _source) in enumerate(added) if d["action"] == merge.TAKE}
    for pos, ins, i in g["added"]["inserts"]:
        if i not in put:
            continue
        if any(s < pos < e for s, e, _t in edits):
            label = added[i][0]["path"][0]["key"]
            for d, source in added:
                if d["path"][0]["key"] == label and d["action"] == merge.TAKE:
                    d["action"] = merge.OPEN
                    d["reason"] = d.get("reason", "") + "; its place falls inside a merged node, so put it in by hand"
                    if source == "all" and not held([d["span"]]):
                        missed.append(_why_not(d, True))
            continue
        edits.append((pos, pos, ins))
    decisions += [d for d, _source in added]
    for r in g["removed"]:
        held_r = bool(r["edit"]) and held([r["edit"]])
        d, source = _chosen(r, rec, [] if held_r else missed)
        if r["edit"]:
            spans.append((d, [list(r["edit"])]))
        if d["action"] == merge.TAKE and r["edit"]:
            s_, e_ = r["edit"]
            if any(s_ < e and s < e_ for s, e, _t in edits):
                d["action"] = merge.OPEN
                d["reason"] = d.get("reason", "") + "; the deletion overlaps a merged node, so delete the block by hand"
                if source == "all" and not held_r:
                    missed.append(_why_not(d, True))
            else:
                edits.append((s_, e_, ""))
        decisions.append(d)
    return {"edits": edits, "decisions": decisions, "spans": spans, "unexplained": unexplained,
            "skipped": skipped, "missed": missed, "starts": starts, "owns": owns}


def kept_edits(edits):
    """(edits in the order of the text, the offsets where two edits overlap). An edit
    that overlaps an earlier one is not made, and the file fails."""
    edits = sorted(edits, key=lambda x: (x[0], x[1]))
    overlaps = [y[0] for x, y in zip(edits, edits[1:]) if y[0] < x[1]]
    kept, end = [], 0
    for x in edits:
        if x[0] >= end:
            kept.append(x)
        end = max(end, x[1])
    return kept, overlaps


def own_check(m, s, e):
    """Why an own text in the place s..e of the mod text cannot take the place of the
    merge there, or "", for the merge `m` (file_edits' result): an edit of the merge
    crosses an edge of the place, or a change that the place holds and Apply file
    makes also edits text outside it (a moved block)."""
    if any(x[0] < e and x[1] > s and not inside(x[:2], s, e) for x in m["edits"]):
        return "an edit of the merge crosses an edge of your text"
    taken = {id(x) for x in m["decisions"] if x["action"] == merge.TAKE}
    for d, spans in m["spans"]:
        if id(d) in taken and any(inside(sp, s, e) for sp in spans) and not all(inside(sp, s, e) for sp in spans):
            return "a change in it also edits text outside it"
    return ""


def _own_lines(text, s, e):
    """The first and last line of the place s..e of `text`."""
    first = text.count("\n", 0, s) + 1
    return first, first + text.count("\n", s, max(s, e - 1))


def _build_file(f, rec, memo=None):
    """Make plan file `f` from its gathered part and its choices `rec` (file_choices'
    result). Returns the skipped rows that the choices make.

    Each own text of `rec` takes the place of its span of the mod text, and the edits
    of the merge in that place are not made. Each decision with an op or a deletion in
    the place takes your text: its action is take, and `own` holds the span. When the
    text cannot go in (own_check, own_problem), the file is as without it and these
    decisions are open, so --apply does not write the file."""
    g = f["gathered"]
    text = g["ours"]
    m = file_edits(f, rec, memo)
    edits, decisions, unexplained, starts = m["edits"], m["decisions"], m["unexplained"], m["starts"]
    own_rows = []
    for o in m["owns"]:
        s, e = o["span"]
        lines = _own_lines(text, s, e)
        why = own_problem(o["text"]) or own_check(m, s, e)
        # A text that cannot go in holds open each change that it touches; with none,
        # a decision of its own.
        touch = (lambda sp: inside(sp, s, e) or (sp[0] < e and s < sp[1])) if why else (lambda sp: inside(sp, s, e))
        mine = [d for d, spans in m["spans"] if any(touch(sp) for sp in spans)]
        where = f"lines {lines[0]} to {lines[1]}" if lines[1] > lines[0] else f"line {lines[0]}"
        if why and not mine:
            mine = [merge.Decision([{"key": where}], "own_text", merge.OPEN, line=lines[0]).to_json()]
            decisions.append(mine[0])
        own_rows.append({"span": [s, e], "lines": list(lines), "text": o["text"],
                         "decisions": 0 if why else len(mine), "problem": why or None})
        for d in mine:
            d["own"], d["by"] = [s, e], "choice"
            d.pop("commit", None)
            if why:
                d["action"], d["reason"] = merge.OPEN, f"your own text for {where} cannot go in: {why}"
            else:
                d["action"], d["reason"] = merge.TAKE, f"your own text for {where} takes the place of this change"
        if why:
            continue
        edits = [x for x in edits if not inside(x[:2], s, e)] + [(s, e, o["text"], tuple(o["edges"]))]
        # Your text decides each line in its place, so the removed-line check skips them.
        unexplained = [(ln, t) for ln, t in unexplained if not lines[0] <= ln <= lines[1]]
    # The decisions in the order of the file. A sort keeps the order of equal places.
    decisions.sort(key=lambda d: d.get("line") or d.get("at") or len(starts) + 1)
    wanted = {d["line"] for d in decisions if d["action"] == merge.OPEN and d.get("line")}
    for d in decisions:
        if d.get("line") in wanted and str(d["line"]) in g["blame"]:
            d["commit"] = g["blame"][str(d["line"])]
    kept, overlaps = kept_edits(edits)
    unexplained += [(text.count("\n", 0, at) + 1, "two edits of the merge overlap here") for at in overlaps]
    merged, _spans = merge.splice(text, kept)
    stale = set(g["stale"])
    f.update(merged=merged, decisions=decisions, open=sum(1 for d in decisions if d["action"] == merge.OPEN),
             removed_check={"passed": not unexplained,
                            "unexplained": [{"line": ln, "text": s} for ln, s in unexplained]},
             stale_entries=sorted({d["by"] for d in decisions if d.get("by") in stale}))
    for key in ("choices", "not_set", "own"):
        f.pop(key, None)
    if rec:
        f["choices"] = rec
    if own_rows:
        f["own"] = own_rows
    if (rec or {}).get("all"):
        counts = {}
        for why in m["missed"]:
            counts[why] = counts.get(why, 0) + 1
        f["not_set"] = [[why, n] for why, n in counts.items()]
    return m["skipped"]


FORMAT_EXTS = (".txt", ".gui")


def pdx_format(texts):
    """{name: formatted text, or None when pdx-format refused it} for {name: text}.
    Each text goes through `pdx-format -` (standard input to standard output), in a
    few threads; pdx-format reports a refusal on standard error and returns the text
    as it was. Raises FileNotFoundError when pdx-format is not installed."""
    import shutil
    import subprocess
    from concurrent.futures import ThreadPoolExecutor
    exe = shutil.which("pdx-format") or str(Path.home() / ".local" / "bin" / "pdx-format")
    if not Path(exe).is_file():
        raise FileNotFoundError("pdx-format is not installed")

    def one(text):
        r = subprocess.run([exe, "-"], input=text, capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        if r.returncode != 0 or "Error" in r.stderr:
            return None
        return r.stdout.lstrip("\ufeff").replace("\r\n", "\n")
    names = list(texts)
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(zip(names, pool.map(one, (texts[n] for n in names))))


# The formatter the plan uses; a test replaces it.
FORMATTER = pdx_format


def same_content(a, b):
    """True when two texts hold the same code tokens in order (letter case aside, as
    pdx-format changes the case of some keywords) and the same comments."""
    from pdx_utilities.script_parser import tokenize
    ta, tb = tokenize(a), tokenize(b)
    code = lambda ts: [t["val"].lower() for t in ts if t["type"] != "comment"]   # noqa: E731
    notes = lambda ts: sorted(" ".join(t["val"].split()) for t in ts if t["type"] == "comment")   # noqa: E731
    return code(ta) == code(tb) and notes(ta) == notes(tb)


LAYOUT_WHY = "the mod file is not in pdx-format layout, so the merge keeps its layout"


def ours_layout(files):
    """Record in each plan file's gathered part whether the mod file is in pdx-format
    layout: `layout` is None when it is, else the `format` outcome of the file. The
    mod keeps some files out of pdx-format (map data, setup files, generated
    locators), and formatting them would change lines the merge did not touch."""
    todo = [f for f in files if PurePosixPath(f["file"]).suffix.lower() in FORMAT_EXTS]
    for f in files:
        f["gathered"]["layout"] = {"applied": False, "why": "not a .txt or .gui file"}
    if not todo:
        return
    try:
        done = FORMATTER({"ours/" + f["file"]: f["gathered"]["ours"] for f in todo})
    except FileNotFoundError as e:
        for f in todo:
            f["gathered"]["layout"] = {"applied": False, "why": f"{e}; run pdx-format on the file after --apply"}
        return
    for f in todo:
        same = done.get("ours/" + f["file"]) == f["gathered"]["ours"]
        f["gathered"]["layout"] = None if same else {"applied": False, "why": LAYOUT_WHY}


def lay_out(files):
    """Give each merged .txt and .gui file the layout of pdx-format, so `--apply`
    needs no format pass. Only a file that pdx-format leaves as it is before the
    merge gets it (ours_layout). The formatted text must hold the same tokens and
    comments as the merge, or the file fails. Each file records the outcome in
    `format`."""
    todo = []
    for f in files:
        lay = f["gathered"]["layout"]
        if PurePosixPath(f["file"]).suffix.lower() not in FORMAT_EXTS:
            f["format"] = {"applied": False, "why": "not a .txt or .gui file"}
        elif f["merged"] == f["gathered"]["ours"]:
            f["format"] = {"applied": False, "why": "no change"}
        elif lay is not None:
            f["format"] = dict(lay)
        else:
            todo.append(f)
    if not todo:
        return
    try:
        done = FORMATTER({"merged/" + f["file"]: f["merged"] for f in todo})
    except FileNotFoundError as e:
        for f in todo:
            f["format"] = {"applied": False, "why": f"{e}; run pdx-format on the file after --apply"}
        return
    for f in todo:
        merged_fmt = done.get("merged/" + f["file"])
        if merged_fmt is None or not same_content(f["merged"], merged_fmt):
            f["format"] = {"applied": False, "failed": True,
                           "why": "pdx-format refused the merged text or changed its content"}
        else:
            f["merged"] = merged_fmt
            f["format"] = {"applied": True}


def _not_merged(rel, name, line, base_tag, why, skipped):
    """The decision for a copy the plan cannot splice into its file: open, so --apply
    does not write the file without the copy's changes. `skipped` lists it too."""
    skipped.append((rel, why))
    return dict(merge.Decision([{"key": name}], "not_merged", merge.OPEN, reason=why, line=line).to_json(),
                copy=name, base_version=base_tag)


def _distinct_skips(skipped):
    """[{file, why, copies}]: one row per file and reason, with the number of copies."""
    rows = {}
    for f, w in skipped:
        rows.setdefault((f, w), 0)
        rows[(f, w)] += 1
    return [{"file": f, "why": w, "copies": n} for (f, w), n in rows.items()]


# Audit findings for a copy that vanilla removed after --old as a whole: the copy
# type, and True when the copy is a whole file.
WHOLE_REMOVED = {"override_orphaned": ("replace", False), "gui_van_removed": ("gui_def", False),
                 "file_def_removed": ("file", False), "file_review": ("file", True),
                 "gui_file_review": ("gui_file", True)}


def _whole_copies(mod_root, findings, order, old_tag, only, block, it, states, reg, skipped):
    """{mod path: (gathered decisions, stale entries)} for the cases the copy audits
    report as findings and not as copies, when vanilla made them after --old:

      vanilla removed the block of a REPLACE, a shadowed GUI template or type, or a
      definition or a whole file at the mod's path: a vanilla_removed decision. The
      intent store decides it; with no rule it is open, because the mod's copy now
      defines the text alone. take_vanilla deletes the block from the mod file; a
      whole file stays open, as --apply never deletes a file.

    A text the merge cannot compare node by node goes to `skipped` with the reason:
    a definition too large to compare statement by statement, a text that is not
    script, a REPLACE the audit could not read, a file with no vanilla history."""
    from .overrides import find_overrides
    after = set(order[order.index(old_tag) + 1:])
    out = {}
    for f in findings:
        rel, _sep, line = f.location.rpartition(":")
        if not line.isdigit():
            rel, line = f.location, "0"
        if only and rel not in only:
            continue
        if block and f.name != block:
            continue
        target = (f.key or {}).get("target", "")
        if f.kind == "file_untracked":
            skipped.append((rel, f"the tracker holds no version of this file ({f.detail}); compare it with "
                                 "vanilla by hand"))
            continue
        if f.kind == "override_unreadable":
            skipped.append((rel, f"the audit could not read the REPLACE of {f.name} at line {line}"))
            continue
        if f.since not in after:
            continue                      # vanilla made this change at or before --old
        if f.kind == "file_bulk_changed":
            skipped.append((rel, f"{f.name} is too large to merge statement by statement; {f.detail} "
                                 f"after {f.base}; take them by hand"))
            continue
        if f.kind.startswith("file_") and f.kind.endswith("_mid") and "#" not in target:
            skipped.append((rel, "the file is not script; vanilla changed lines in it (see the file audit); "
                                 "take them by hand"))
            continue
        if f.kind not in WHOLE_REMOVED or (f.kind.endswith("file_review") and (f.key or {}).get("change") != "removed"):
            continue
        copy, whole_file = WHOLE_REMOVED[f.kind]
        if f.kind == "override_orphaned" and any(
                "INJECT" in o["type"] for o in find_overrides(mod_root)
                if o["file"] == rel and o["line"] == int(line) and o["block"] == f.name):
            copy = "inject"
        content = rel.rsplit("/", 1)[0] if copy in ("file", "gui_file") else target.split(":", 1)[-1].rsplit("/", 1)[0]
        identity = f"{content}/{f.name}" if copy != "gui_def" else target
        removed_copy(mod_root, out, rel, f.name, int(line), copy, identity, content, f.since, whole_file,
                     it, states, reg, old_tag)
    return out


def removed_copy(mod_root, out, rel, name, line, copy, identity, content, since, whole_file, it, states, reg,
                 base_version):
    """Add to `out` the vanilla_removed decision for a copy whose vanilla text is gone
    (see _whole_copies), as a gathered decision (_row) with `edit`: the (start, end)
    that deletes the block when the decision takes vanilla's text."""
    dev = intent.Deviation(
        copy=copy, identity=identity, block=name, content=content, file=rel, vanilla_file=None, line=line,
        change="vanilla_removed", priority=diff3.MID, path=[], keys=(), systems=reg.systems_of(rel))
    rows, stale = out.setdefault(rel, ([], set()))
    edit = None if whole_file else _block_at(read_mod_file(Path(mod_root) / rel)[0], line)

    def decide(choice):
        if choice:
            action, by = choice, "choice"
        elif intent.attribute(dev, it) is None:
            action, by = merge.OPEN, None
        else:
            action, by = _apply_intent(dev, it, states, "conflict", stale)
        what = "the file" if whole_file else f"the block {name}"
        why = f"vanilla removed {what} in {since}"
        if whole_file:
            why += "; --apply does not delete a file: delete it by hand, or keep it"
            if action == merge.TAKE:
                action = merge.OPEN
        elif action == merge.TAKE and edit is None:
            action, why = merge.OPEN, why + "; the block is not a single top-level block at its line"
        return [action, by, why]
    action, by, why = decide(None)
    d = merge.Decision([{"key": name}], "vanilla_removed", action, by, reason=why, line=line or None,
                       address={"identity": identity, "path": []})
    row = _row(dict(d.to_json(), copy=name, base_version=base_version), decide(merge.TAKE), decide(merge.KEEP))
    rows.append(dict(row, edit=list(edit[:2]) if edit else None))


def _block_at(text, line):
    """(start, end, "") that deletes the one top-level block that starts on `line` of
    `text`, with the comment lines above it, or None."""
    hits = [n for n in diff3.nodes(text) if text.count("\n", 0, n.start) + 1 == line]
    if len(hits) != 1 or hits[0].kind != "block":
        return None
    s, e = merge._delete_span(text, hits[0])
    return s, e, ""


def untracked_reason(rel, old_tag):
    """Why the merge skips a file whose type the tracker did not record at --old. The
    tracker began to record some file types (.map and .csv from 1.4.0) after older
    versions; vanilla held the file then, but no version of it is known. Without a
    base the merge cannot tell the mod's edits from vanilla's changes. An empty base
    would call every vanilla line an addition, so the file is merged by hand."""
    ext = PurePosixPath(rel).suffix.lower() or "(no extension)"
    return (f"the tracker holds no {ext} files at {old_tag}, so no base version of this file is known; "
            "compare it with vanilla by hand")


def _add_injects(mod_root, base, commits, new_hash, old_tag, only, block, reg, by_file, regenerate, skipped):
    """Add each INJECT whose target block vanilla changed between --old and --new to
    `by_file`."""
    from .changes import Copy
    from .gui import version_window
    from .overrides import block_history, extract_mod_block, find_overrides
    from .tracker import tag_of
    injects = [o for o in find_overrides(mod_root) if "INJECT" in o["type"]
               and (not only or o["file"] in only) and (not block or o["block"] == block)]
    if not injects:
        return
    window = version_window(commits, new_hash)
    tags = [tag_of(m) for _h, m in window]
    old_i = tags.index(old_tag)                      # plan() checked --old
    hist = block_history(base, [window[old_i], window[-1]], sorted({o["category"] for o in injects}),
                         {(o["category"], o["block"]) for o in injects})
    for ov in injects:
        generated, tool = reg.generated_by(ov["file"])
        if generated:
            regenerate[ov["file"]] = tool
            continue
        old_text, new_text = hist.get((ov["category"], ov["block"]), [None, None])
        if old_text == new_text:
            continue                     # vanilla did not change the target after --old
        if new_text is None:
            continue    # vanilla removed the target: the override audit's override_orphaned (_whole_copies)
        text = extract_mod_block(mod_root, ov)
        if text is None:
            skipped.append((ov["file"], f"the INJECT of {ov['block']} at line {ov['line']} could not be read"))
            continue
        c = Copy("inject", ov["block"], f"inject:{ov['category']}/{ov['block']}", text,
                 [old_text, new_text], [tags[old_i], tags[-1]], ov["file"], ov["line"], True,
                 diff3.SCRIPT, None, None, {})
        by_file[ov["file"]].append((c, tags[old_i] if old_text is not None else NO_BASE, old_text or "", new_text,
                                    []))


def _comment_start(text, start):
    """`start`, moved up over the comment lines directly above it."""
    while start > 0:
        prev = text.rfind("\n", 0, start - 1) + 1
        if not text[prev:start - 1].strip().startswith("#"):
            break
        start = prev
    return start


def _new_definitions(mod_root, base, commits, new_hash, old_tag, only, block, reg, regenerate, it, states,
                     skipped):
    """{mod path: ({"decisions": gathered decisions, "inserts": inserts}, stale
    entries)} for the mod's script files at a vanilla file's path. An insert is
    [offset, text, decision index]: a top-level definition that vanilla added to the
    file after --old and that the mod lacks, put in when its decision takes vanilla's
    text. It goes after the nearest earlier definition of vanilla that the mod holds,
    else before the nearest later one, else at the end. A definition that --old held
    and the mod lacks is the mod's deletion. It stays deleted, and it is a decision
    when vanilla changed it. The intent store decides each one (decide_definition). A
    definition the mod keeps in another file of its folder is not missing. A
    generated file goes to `regenerate`."""
    from pathlib import PurePosixPath
    from .files import SCRIPT_EXTS, TOP_LEVEL, _names, _split, decode, definitions, mod_files, scope_of
    from .gui import version_window
    from .tracker import tag_of
    window = version_window(commits, new_hash)
    old = next((h for h, m in window if tag_of(m) == old_tag), None)
    if old is None:
        raise ValueError(f"--old {old_tag} is not a tracked version before the new one")
    old_files, new_files = base.files(old), base.files(window[-1][0])
    scripts = [r for r in mod_files(mod_root) if PurePosixPath(r).suffix.lower() in SCRIPT_EXTS]
    old_exts = {PurePosixPath(p).suffix.lower() for p in old_files}
    pairs = []
    for r in scripts:
        if (only and r not in only) or not new_files.get(r) or old_files.get(r) == new_files[r]:
            continue
        if old_files.get(r):
            pairs.append((r, old_files[r], new_files[r]))
        elif PurePosixPath(r).suffix.lower() in old_exts:
            pairs.append((r, None, new_files[r]))          # vanilla added the file after --old
        else:
            skipped.append((r, untracked_reason(r, old_tag)))
    blobs = base.blobs({i for _r, a, b in pairs for i in (a, b) if i})
    out = {}
    for rel, a, b in pairs:
        if (a and a not in blobs) or b not in blobs:
            skipped.append((rel, "git could not read vanilla's version of the file"))
            continue
        old_text = decode(blobs[a]).replace("\r\n", "\n") if a else ""
        new_text = decode(blobs[b]).replace("\r\n", "\n")
        was, now = definitions(old_text), definitions(new_text)
        wanted = (lambda label: label == block or label.split(" ", 1)[0] == block) if block else (lambda label: True)
        fresh = {lb for lb in now if lb not in was and lb != TOP_LEVEL and wanted(lb)}
        changed = {lb for lb in was if lb in now and lb != TOP_LEVEL and was[lb].sig != now[lb].sig
                   and wanted(lb)}
        if not fresh and not changed:
            continue
        text = read_mod_file(Path(mod_root) / rel)[0]
        mine = definitions(text)
        fresh -= set(mine)
        gone = {lb for lb in changed if lb not in mine}
        if not fresh and not gone:
            continue
        generated, tool = reg.generated_by(rel)
        if generated:
            regenerate[rel] = tool
            continue
        elsewhere = set()
        for other in scripts:
            if other != rel and scope_of(other) == scope_of(rel):
                elsewhere |= _names(Path(mod_root) / other)
        held = lambda lb: lb in elsewhere or lb.split(" ", 1)[0] in elsewhere   # noqa: E731
        decisions, put, stale = [], {}, set()
        for lb in sorted(fresh | gone):
            if held(lb):
                continue
            kind = "vanilla_added" if lb in fresh else "removed_changed"
            theirs = " ".join(now[lb].sig.split())
            identity = f"{rel.rsplit('/', 1)[0]}/{lb}"
            action, by, why = decide_definition(rel, lb, kind, theirs, it, states, reg, stale)
            d = merge.Decision([{"key": lb}], kind, action, by, reason=why,
                               base=" ".join(was[lb].sig.split()) if lb in was else None,
                               theirs=theirs, address={"identity": identity, "path": []})
            take, keep = (list(decide_definition(rel, lb, kind, theirs, it, states, reg, set(),
                                                 {_key(identity, [], kind): choice})) for choice in CHOICES)
            put[lb] = len(decisions)
            decisions.append(_row(dict(d.to_json(), copy=lb, base_version=old_tag), take, keep))
        mine_items = {}
        for item in _split(text):
            mine_items.setdefault(item[0], []).append(item)
        inserts, pending, anchor, count = [], [], None, defaultdict(int)
        for label, start, end, *_rest in _split(new_text):
            k, count[label] = count[label], count[label] + 1
            if label in put:
                body = new_text[_comment_start(new_text, start):end].rstrip("\n") + "\n"
                if anchor is None:
                    pending.append((label, body))
                else:
                    sep = "\n" if text[anchor - 1:anchor] == "\n" else "\n\n"
                    inserts.append([anchor, sep + body, put[label]])
            elif label != TOP_LEVEL and label in mine_items:
                members = mine_items[label]
                m = members[min(k, len(members) - 1)]
                if pending:
                    at = _comment_start(text, m[1])
                    inserts += [[at, body + "\n", put[lb]] for lb, body in pending]
                    pending = []
                anchor = m[2]
        if pending:
            sep = "" if not text else "\n" if text.endswith("\n") else "\n\n"
            inserts += [[len(text), sep + body, put[lb]] for lb, body in pending]
        if inserts or decisions:
            out[rel] = ({"decisions": decisions, "inserts": inserts}, stale)
    return out


def _reads_as(text, at, mod_text):
    """True when the file holds the copy's text at `at`. A key that occurs in more
    than one top-level block is one definition (files.definitions). Its text keeps
    the newlines of the definitions between the blocks and makes their other
    characters spaces."""
    if text.startswith(mod_text, at):
        return True
    seg = text[at:at + len(mod_text)]
    if len(seg) != len(mod_text) or seg.count("\n") != mod_text.count("\n"):
        return False
    return all(a == b or (b == " " and a != "\n") for a, b in zip(seg, mod_text))


def _line_starts(text):
    """The offset of each line of `text`."""
    out, at = [0], text.find("\n")
    while at >= 0:
        out.append(at + 1)
        at = text.find("\n", at + 1)
    return out


def _locate(text, copy, starts):
    """The offset of the copy's text in its file, near its first line. `starts`:
    _line_starts(text)."""
    if not 1 <= copy.line <= len(starts) + 1:
        return None
    offset = starts[copy.line - 1] if copy.line <= len(starts) else len(text) + 1
    if _reads_as(text, offset, copy.mod_text):
        return offset
    at = text.find(copy.mod_text, max(0, offset - 2000))
    return at if at >= 0 else None


def _print(p):
    for f in p["files"]:
        print(f"## {f['file']}")
        print()
        taken = sum(1 for d in f["decisions"] if d["action"] == merge.TAKE)
        kept = sum(1 for d in f["decisions"] if d["action"] == merge.KEEP)
        print(f"- {taken} vanilla changes taken, {kept} kept by a rule or an entry, {f['open']} open decisions")
        for why, n in f.get("not_set") or []:
            word = "Take vanilla" if f["choices"]["all"] == merge.TAKE else "Keep mine"
            print(f"- {word} for all does not apply to {n} decision{'' if n == 1 else 's'}: {why}")
        rc = f["removed_check"]
        print(f"- Removed-line check: {'passed' if rc['passed'] else 'FAILED'}")
        fm = f.get("format", {})
        print("- Layout: pdx-format" if fm.get("applied") else
              f"- Layout: {'FAILED, ' if fm.get('failed') else ''}not formatted ({fm.get('why')})")
        for u in rc["unexplained"]:
            print(f"  - line {u['line']} removed without a vanilla change: `{u['text'].strip()}`")
        for e in f["stale_entries"]:
            print(f"- Stale entry {e}: run `pdx-audit intent confirm {e[6:]}` after review")
        for o in f.get("own") or []:
            first, last = o["lines"]
            where = f"lines {first} to {last}" if last > first else f"line {first}"
            state = f"NOT USED, {o['problem']}" if o["problem"] else f"{o['decisions']} changes in it"
            print(f"- Your own text for {where} ({state}):")
            print("  " + "\n  ".join(o["text"].strip("\n").split("\n")[:20]))
        for d in f["decisions"]:
            if d["action"] == merge.OPEN:
                where = (f"line {d['line']}" if d.get("line") else f"new node near line {d['at']}"
                         if d.get("at") else "new node")
                who = f", written by {d['commit']}" if d.get("commit") else ""
                print(f"  - OPEN ({CAUSES.get(d.get('cause'), 'Needs Review')}) {d['kind']} "
                      f"`{' > '.join(s['key'] for s in d['path'])}` ({where}{who})"
                      + (f" [{d['by']}]" if d.get("by") else ""))
                for side in ("base", "ours", "theirs"):
                    if d.get(side):
                        print(f"      {side}: {d[side][:200]}")
        print()
        print("```diff")
        print(f["diff"].rstrip())
        print("```")
        print()
    for r in p["regenerate"]:
        print(f"- {r['file']}: not merged; {r['hint']}")
    for s in p["skipped"]:
        print(f"- {s['file']}: skipped; {s['why']}")


def _main(args):
    from .intent_cli import _vanilla
    from .store import open_store
    from .tracker import find_mod_root
    mod_root = find_mod_root(args.mod_root)
    store, err = open_store(mod_root)
    if store is None:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    it = intent.of(store.state)
    if args.clear_saved:
        save_choices(store, args.old, args.new, {})
        print(f"Deleted the saved choices for {args.old} to {args.new}.")
        return 0
    saved = saved_choices(store, args.old, args.new) if args.saved else None
    base, commits, new_hash, new_msg = _vanilla(mod_root, args)
    if args.apply:
        from .tracker import tag_of
        try:
            versions = json.loads(Path(args.apply).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"Error: cannot read the plan: {e}", file=sys.stderr)
            return 1
        if (versions.get("old"), versions.get("new")) != (args.old, tag_of(new_msg)):
            print(f"Error: the plan merges {versions.get('old')} to {versions.get('new')}, not "
                  f"{args.old} to {tag_of(new_msg)}.", file=sys.stderr)
            return 1
        del versions
        chosen = (read_choices(args.choices, mod_root, args.choose, saved)
                  if args.choices or args.choose or saved else None)
        return _apply(mod_root, args.apply, args.file, chosen,
                      lambda files: plan(mod_root, base, commits, args.old, new_hash, new_msg, it, {f["file"] for f in files},
                                         bases=plan_bases(files)))
    try:
        p = plan(mod_root, base, commits, args.old, new_hash, new_msg, it, args.file, args.block,
                 read_choices(args.choices, mod_root, args.choose, saved))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    out = Path(args.plan_out) if args.plan_out else (
        store.dir / "merge-plans" / f"{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    p["mod_root"] = str(mod_root)
    out.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.quiet:
        n = len(p["files"])
        ready = sum(1 for f in p["files"] if ready_to_apply(f))
        print(f"{n} file{'' if n == 1 else 's'}: {ready} ready, "
              f"{sum(1 for f in p['files'] if f['open'])} with open decisions. Plan saved: {out}")
    elif args.json:
        print(json.dumps({k: v for k, v in p.items()} | {"files": [
            {k: v for k, v in f.items() if k not in ("merged", "gathered")} for f in p["files"]]},
            ensure_ascii=False, indent=1))
    else:
        _print(p)
        print(f"Plan saved: {out}")
        print(f"Review the diff, then run `pdx-audit merge --old {args.old} --new {args.new} --apply {out}`.")
    return 0


def ready_to_apply(f):
    """True when --apply writes the plan's file `f` as it stands."""
    return (not f["open"] and f["removed_check"]["passed"] and not f["stale_entries"]
            and not f.get("format", {}).get("failed"))


def _place(d):
    """The address of decision `d` without the count of its siblings (`n`), which a
    write changes when it deletes or adds a sibling of the same key."""
    path = [{k: v for k, v in seg.items() if k != "n"} for seg in d["address"]["path"]]
    return d["address"]["identity"], json.dumps(path, sort_keys=True)


def _taken(d):
    return d["action"] == merge.TAKE and not d.get("own")


def _kept_copies(f):
    """{copy name: decision} of the copies of plan file `f` that the written file must
    still hold: each copy with a change that it takes, unless the changes that it takes
    at the copy's own address only delete it. The decision names the copy."""
    out = {}
    for cp in f["gathered"]["copies"]:
        if "fixed" in cp:
            continue
        taken = [d for d in f["decisions"] if d.get("copy") == cp["name"] and d["action"] == merge.TAKE
                 and d.get("address") and not d.get("own")]
        whole = [d for d in taken if not d["address"]["path"]]
        if taken and not (whole and all(not d.get("theirs") for d in whole)):
            out[cp["name"]] = taken[0]
    return out


def _where(d):
    where = " > ".join(str(s["key"]) for s in d["path"]) or d.get("copy") or d["kind"]
    return where + (f" (line {d['line']})" if d.get("line") else f" (near line {d['at']})" if d.get("at") else "")


def plan_bases(files):
    """{(mod path, copy name): base version} of the copies of plan files `files`."""
    return {(f["file"], cp["name"]): cp["base"] for f in files for cp in f["gathered"]["copies"] if "fixed" not in cp}


def _name(key):
    """The name of a copy or a node key: its last word, without a prefix such as
    REPLACE: or `type`, and without the level that a copy name adds (" > ...")."""
    return re.split(r"[\s:]", key.split(" > ")[0].strip())[-1]


def _defined(text, gui):
    """The names that `text` defines: its top-level keys, and in a GUI file also the
    keys in its top-level blocks (types and templates)."""
    top = diff3.nodes(text)
    nodes = list(top) + ([c for n in top for c in (n.children or ())] if gui else [])
    return {_name(n.key) for n in nodes if n.key}


def _known_texts(f):
    """The texts (intent.canon) that the nodes of plan file `f` can hold after the
    write without a fault: each node of the mod text, and vanilla's text of each
    decision."""
    out = {d.get("theirs") for d in f["decisions"]}
    stack = list(diff3.nodes(f["gathered"]["ours"]))
    while stack:
        n = stack.pop()
        out.add(intent.canon(n))
        stack.extend(n.children or ())
    return out


def check_applied(after, files):
    """{mod path: [why]} for the written plan files `files` against `after`, a plan of
    the same files with the same versions and the same base of each copy (plan_bases),
    made after the write. A change that a file took holds vanilla's text after the
    write, so the plan of the written file has no decision for its node. Addresses
    compare without the count of siblings (_place), which the write changes. A node
    text at the address of a taken change is a fault when the plan after the write
    holds it more often than the decisions that the file does not take explain, or
    when no node held it and vanilla does not hold it. Each copy with a taken change
    must stay defined in the written text (_kept_copies), apart from a copy of the
    whole file; the plan after the write does not list a copy that now reads as
    vanilla.

    The base must stay: the written text can be nearer to a later version, and with
    that version as its base a change that the write missed reads as the mod's own
    edit."""
    again = {f["file"]: f for f in after["files"]}
    out = {}
    for f in files:
        now = again.get(f["file"]) or {}
        mine = [d for d in f["decisions"] if d.get("address")]
        taken = {(_place(d), d.get("ours")): d for d in mine if _taken(d)}
        places = {place: d for (place, _ours), d in taken.items()}
        left = Counter((_place(d), d.get("ours")) for d in mine if not _taken(d))
        found = Counter((_place(d), d.get("ours")) for d in now.get("decisions", ()) if d.get("address"))
        hits = {_where(taken[sig]) for sig, n in found.items() if sig in taken and n > left[sig]}
        unknown = [sig for sig in found if sig[0] in places and sig not in taken]
        if unknown:
            known = _known_texts(f)
            hits |= {_where(places[place]) for place, ours in unknown if ours not in known}
        why = [f"after the write, the change at {w} is still a decision" for w in sorted(hits)]
        held = _defined(f["merged"], f["file"].endswith(".gui"))
        why += [f"after the write, the file does not hold {name}, which takes the change at {_where(d)}"
                for name, d in sorted(_kept_copies(f).items()) if name != f["file"] and _name(name) not in held]
        if why:
            out[f["file"]] = why
    return out


def _apply(mod_root, plan_path, only=None, choices=None, replan=None):
    """Write the plan's files. With `choices` (read_choices' result), build each file
    again from its gathered part with these choices first. A plan file with `expect`
    (the sha of the merged text that the app showed) is written only when the build
    gives that text. `replan(files)` plans the written files again (see check_applied);
    each file that fails the check, or does not read back as written, gets its old
    text back."""
    p = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    if only:
        p["files"] = [f for f in p["files"] if f["file"] == only]
    if Path(p.get("mod_root", "")) != Path(mod_root):
        print(f"Error: the plan is for {p.get('mod_root')}, not {mod_root}.", file=sys.stderr)
        return 1
    if choices is not None:
        if any("gathered" not in f for f in p["files"]):
            print("Error: the plan holds no gathered parts. Run the dry run again.", file=sys.stderr)
            return 1
        build(p["files"], choices)
    written, refused, olds = [], [], {}
    for f in p["files"]:
        path = Path(mod_root) / f["file"]
        why = []
        text, bom, crlf = read_mod_file(path)
        if _sha(text) != f["before_sha"]:
            why.append("the file changed after the dry run")
        if f.get("expect") and _sha(f["merged"]) != f["expect"]:
            why.append("the choices give another text than the page showed; plan the merge again")
        if not f["removed_check"]["passed"]:
            why.append("the removed-line check failed")
        if f["open"]:
            why.append(f"{f['open']} open decisions")
        if f["stale_entries"]:
            why.append("it uses stale entries")
        if f.get("format", {}).get("failed"):
            why.append("pdx-format refused the merged text")
        if f["merged"] == text and not why:
            continue
        if why:
            refused.append((f["file"], why))
            continue
        olds[f["file"]] = path.read_bytes()
        write_mod_file(path, f["merged"], bom, crlf)
        written.append(f["file"])
    bad = {rel: ["the file does not read back as written"] for rel in written
           if read_mod_file(Path(mod_root) / rel)[0] != next(f["merged"] for f in p["files"] if f["file"] == rel)}
    rest = [f for f in p["files"] if f["file"] in written and f["file"] not in bad]
    if replan is not None and rest:
        print(f"Checking {len(rest)} written file{'' if len(rest) == 1 else 's'} with a new plan of them...",
              file=sys.stderr)
        bad.update(check_applied(replan(rest), rest))
    for rel, why in bad.items():
        restore_file(Path(mod_root) / rel, olds[rel])
        written.remove(rel)
        refused.append((rel, why + ["its old text is back"]))
    for rel in written:
        print(f"Wrote {rel}")
    for rel, why in refused:
        print(f"Refused {rel}: {'; '.join(why)}", file=sys.stderr)
    unformatted = [f["file"] for f in p["files"] if f["file"] in written
                   and PurePosixPath(f["file"]).suffix.lower() in FORMAT_EXTS
                   and not f.get("format", {}).get("applied")]
    if unformatted:
        print("These files keep the mod's own layout: " + ", ".join(unformatted))
    return 1 if refused else 0
