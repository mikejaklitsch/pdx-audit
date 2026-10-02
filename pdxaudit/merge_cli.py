"""`pdx-audit merge`: take vanilla patch changes into the mod's copies.

A dry run writes nothing into the mod. It prints the full diff of each file, the
decisions, and the removed-line check, and it saves a plan in the per-user data
folder. `--apply` takes that plan: it writes a file only when the file still reads as
it did at the dry run, its removed-line check passed, it has no open decision and
it used no stale entry. So no mod file changes without a reviewed dry run."""
import argparse
import datetime
import hashlib
import json
import os
import sys
from collections import defaultdict
from pathlib import Path, PurePosixPath

from . import diff3, intent, merge, proposer, session
from .registry import registry


# The base version of a text that vanilla did not hold at --old.
NO_BASE = "(none)"


def _sha(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


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
    tmp = Path(path).with_name(f".{Path(path).name}.pdx-audit-{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def build_parser():
    ap = argparse.ArgumentParser(prog="pdx-audit merge",
                                 description="Merge vanilla patch changes into the mod's copies.")
    ap.add_argument("--mod-root")
    ap.add_argument("--vanilla-repo")
    ap.add_argument("--old", required=True, help="the version the copies were ported to")
    ap.add_argument("--new", required=True, help="the version to merge in")
    ap.add_argument("--file", help="only the copies in this mod file")
    ap.add_argument("--block", help="only the copies of this block or definition name")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", metavar="PLAN", help="the plan file a dry run wrote")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--plan-out", help="where the dry run writes its plan")
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


def _decider(dev_template, it, states, mod_root):
    """decide(path, kind, ours node, theirs node, theirs text, what) for one copy."""
    def decide(path, kind, o, t, _tt, what):
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


def decide_definition(rel, label, kind, theirs_text, it, states, reg, stale):
    """(action, by, reason) for a top-level definition of a same-path script file that
    vanilla added after --old (kind vanilla_added) or changed after the mod deleted it
    (kind removed_changed). The deviation has the definition as its identity and an
    empty node path, so a rule matches it by `change`, `content`, `file`, `block`
    (the definition name) and `vanilla_key`, and an entry by its address. Without a
    rule or an entry, a new definition is taken unless a system owns the file (see
    `owner`); a deleted definition that vanilla changed is always open."""
    content = rel.rsplit("/", 1)[0]
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
        text = copy.mod_text
        for n in _walk(diff3.nodes(text)):
            line = text[text.rfind("\n", 0, n.start) + 1:merge._line_end(text, n.end)]
            self.comments[n.start] = intent._comment_of(line)


def _walk(nodes):
    for n in nodes:
        yield n
        if n.children:
            yield from _walk(n.children)


def plan(mod_root, base, commits, old_tag, new_hash, new_msg, it, file=None, block=None):
    """{"files": [...], "regenerate": [...], "skipped": [...]} for a dry run."""
    from .tracker import tag_of
    reg = registry(mod_root)
    gen = {}
    from .gui import version_window
    order = [tag_of(m) for _h, m in version_window(commits, new_hash)]
    if old_tag not in order[:-1]:
        raise ValueError(f"--old {old_tag} is not a tracked version before the new one "
                         f"({', '.join(order[:-1])})")
    findings = []
    copies, devs = intent.collect(mod_root, base, commits, new_hash, new_msg, {file} if file else None, gen,
                                  findings)
    states = intent.entry_states(it, copies, devs)
    by_file = defaultdict(list)
    regenerate, skipped = dict(gen), []
    for c in copies:
        if file and c.file != file:
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
        base_i = diff3.baseline(c.mod_text, c.versions[:old_i + 1], c.unwrap, c.dialect)
        theirs = c.versions[-1]
        if base_i is None:
            # Vanilla held no version of this text at --old or before: vanilla added it
            # later. The base is empty, so each node vanilla holds is its addition.
            base_text, base_tag = "", NO_BASE
        else:
            base_text, base_tag = c.versions[base_i], c.tags[base_i]
        if base_text == theirs:
            continue
        later = c.versions[(base_i + 1 if base_i is not None else 0):-1]
        by_file[c.file].append((c, base_tag, base_text, theirs, later))
    _add_injects(mod_root, base, commits, new_hash, old_tag, file, block, reg, by_file, regenerate, skipped)
    added = _new_definitions(mod_root, base, commits, new_hash, old_tag, file, block, reg, regenerate, it,
                             states, skipped)
    whole = _whole_copies(mod_root, findings, order, old_tag, file, block, it, states, reg, skipped)
    files = []
    for rel in sorted(set(by_file) | set(added) | set(whole)):
        items = by_file.get(rel, [])
        path = Path(mod_root) / rel
        text, bom, crlf = read_mod_file(path)
        edits, decisions, unexplained, stale = [], [], [], set()
        for c, base_tag, base_text, theirs, later in items:
            start = _locate(text, c)
            if start is None:
                _not_merged(rel, c, base_tag, f"the copy {c.name} at line {c.line} does not read as the audit "
                            "read it", skipped, decisions)
                continue
            tpl = _Template(c, mod_root, reg)
            if c.audit == "inject":
                res = merge.merge_inject(base_text, c.mod_text, theirs, _decider(tpl, it, states, mod_root))
            else:
                old = None
                if base_tag not in (old_tag, NO_BASE) and old_tag in c.tags:
                    old = (c.versions[c.tags.index(old_tag)] or "", old_tag)
                res = merge.merge_texts(base_text, c.mod_text, theirs, c.dialect, c.unwrap,
                                        _decider(tpl, it, states, mod_root), later, old)
            # An op must not touch a character that the copy holds blank for another
            # definition. Such an op would overwrite that definition.
            if any(text[start + op.start:start + op.end] != c.mod_text[op.start:op.end] for op in res.ops):
                _not_merged(rel, c, base_tag, f"the merge of {c.name} at line {c.line} touches another "
                            "definition", skipped, decisions)
                continue
            stale |= tpl.stale
            first_line = text.count("\n", 0, start)
            for d in res.decisions:
                if d.line is not None:
                    d.line += first_line
                decisions.append((d, c.name, base_tag))
            unexplained += [(ln + first_line, s) for ln, s in res.unexplained]
            # The merge splices each op, not the whole copy. A copy of a repeated key
            # spans the definitions between its blocks, and they must stay.
            edits += [(start + op.start, start + op.end, op.text) for op in res.ops]
        inserts, new_decisions, new_stale = added.get(rel, ([], [], set()))
        stale |= new_stale
        for pos, ins, label in inserts:
            if any(s < pos < e for s, e, _t in edits):
                for d in new_decisions:
                    if d.path[0]["key"] == label and d.action == merge.TAKE:
                        d.action = merge.OPEN
                        d.reason += "; its place falls inside a merged node, so put it in by hand"
                continue
            edits.append((pos, pos, ins))
        decisions += [(d, d.path[0]["key"], old_tag) for d in new_decisions]
        w_edits, w_decisions, w_stale = whole.get(rel, ([], [], set()))
        for s_, e_, new_ in w_edits:
            if any(s_ < e and s < e_ for s, e, _t in edits):
                skipped.append((rel, "a deletion of a whole block that vanilla removed overlaps a merged node"))
                continue
            edits.append((s_, e_, new_))
        decisions += [(d, d.path[0]["key"], old_tag) for d in w_decisions]
        stale |= w_stale
        wanted = {d.line for d, _n, _b in decisions if d.action == merge.OPEN and d.line}
        blamed = proposer.blame(mod_root, rel, wanted) if wanted else {}
        decisions = [dict(d.to_json(), copy=name, base_version=btag,
                          **({"commit": (blamed.get(d.line) or (None,))[0]} if d.line in blamed else {}))
                     for d, name, btag in decisions]
        edits.sort(key=lambda x: (x[0], x[1]))
        for (s1, e1, _t1), (s2, e2, _t2) in zip(edits, edits[1:]):
            if s2 < e1:                  # two copies' edits overlap: the file fails
                unexplained.append((text.count("\n", 0, s2) + 1, "two edits of the merge overlap here"))
        merged, _spans = merge.splice(text, [x for k, x in enumerate(edits)
                                             if not any(x[0] < y[1] for y in edits[:k])])
        files.append({"file": rel, "before_sha": _sha(text), "merged": merged, "bom": bom, "crlf": crlf,
                      "ours": text, "decisions": decisions,
                      "open": sum(1 for d in decisions if d["action"] == merge.OPEN),
                      "removed_check": {"passed": not unexplained,
                                        "unexplained": [{"line": ln, "text": s} for ln, s in unexplained]},
                      "stale_entries": sorted(stale)})
    # A tool's output that the merge met but cannot compare goes to the regenerate
    # list, never to the skipped list: the tool reads vanilla again.
    for f, _w in skipped:
        generated, tool = reg.generated_by(f)
        if generated:
            regenerate.setdefault(f, tool)
    lay_out(files)
    for f in files:
        f["diff"] = merge.unified(f["file"], f.pop("ours"), f["merged"])
    return {"old": old_tag, "new": tag_of(new_msg), "files": files,
            "regenerate": [{"file": f, "tool": t, "hint": reg.regenerate_hint(t)} for f, t in sorted(regenerate.items())],
            "skipped": _distinct_skips([(f, w) for f, w in skipped if f not in regenerate])}


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


def lay_out(files):
    """Give each merged .txt and .gui file the layout of pdx-format, so `--apply`
    needs no format pass. Only a file that pdx-format leaves as it is before the
    merge gets it: the mod keeps some files out of pdx-format (map data, setup
    files, generated locators), and formatting them would change lines the merge did
    not touch. The formatted text must hold the same tokens and comments as the
    merge, or the file fails. Each file records the outcome in `format`."""
    todo = [f for f in files if PurePosixPath(f["file"]).suffix.lower() in FORMAT_EXTS
            and f["merged"] != f["ours"]]
    for f in files:
        f["format"] = {"applied": False, "why": "not a .txt or .gui file"
                       if PurePosixPath(f["file"]).suffix.lower() not in FORMAT_EXTS else "no change"}
    if not todo:
        return
    texts = {}
    for f in todo:
        texts["ours/" + f["file"]] = f["ours"]
        texts["merged/" + f["file"]] = f["merged"]
    try:
        done = FORMATTER(texts)
    except FileNotFoundError as e:
        for f in todo:
            f["format"] = {"applied": False, "why": f"{e}; run pdx-format on the file after --apply"}
        return
    for f in todo:
        ours_fmt, merged_fmt = done.get("ours/" + f["file"]), done.get("merged/" + f["file"])
        if ours_fmt != f["ours"]:
            f["format"] = {"applied": False, "why": "the mod file is not in pdx-format layout, so the merge "
                           "keeps its layout"}
        elif merged_fmt is None or not same_content(f["merged"], merged_fmt):
            f["format"] = {"applied": False, "failed": True,
                           "why": "pdx-format refused the merged text or changed its content"}
        else:
            f["merged"] = merged_fmt
            f["format"] = {"applied": True}


def _not_merged(rel, c, base_tag, why, skipped, decisions):
    """A copy the plan cannot splice into its file: listed in `skipped`, and an open
    decision, so --apply does not write the file without the copy's changes."""
    skipped.append((rel, why))
    decisions.append((merge.Decision([{"key": c.name}], "not_merged", merge.OPEN, reason=why, line=c.line),
                      c.name, base_tag))


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


def _whole_copies(mod_root, findings, order, old_tag, file, block, it, states, reg, skipped):
    """{mod path: (edits, decisions, stale entries)} for the cases the copy audits
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
        if file and rel != file:
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
                     it, states, reg)
    return out


def removed_copy(mod_root, out, rel, name, line, copy, identity, content, since, whole_file, it, states, reg):
    """Add to `out` the vanilla_removed decision for a copy whose vanilla text is gone
    (see _whole_copies), and the edit that deletes it when the store says
    take_vanilla."""
    dev = intent.Deviation(
        copy=copy, identity=identity, block=name, content=content, file=rel, vanilla_file=None, line=line,
        change="vanilla_removed", priority=diff3.MID, path=[], keys=(), systems=reg.systems_of(rel))
    rec = out.setdefault(rel, ([], [], set()))
    if intent.attribute(dev, it) is None:
        action, by = merge.OPEN, None
    else:
        action, by = _apply_intent(dev, it, states, "conflict", rec[2])
    what = "the file" if whole_file else f"the block {name}"
    why = f"vanilla removed {what} in {since}"
    edit = None
    if whole_file:
        why += "; --apply does not delete a file: delete it by hand, or keep it"
        if action == merge.TAKE:
            action = merge.OPEN
    elif action == merge.TAKE:
        edit = _block_at(read_mod_file(Path(mod_root) / rel)[0], line)
        if edit is None:
            action, why = merge.OPEN, why + "; the block is not a single top-level block at its line"
    rec[1].append(merge.Decision([{"key": name}], "vanilla_removed", action, by, reason=why,
                                 line=line or None))
    if edit is not None:
        rec[0].append(edit)


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


def _add_injects(mod_root, base, commits, new_hash, old_tag, file, block, reg, by_file, regenerate, skipped):
    """Add each INJECT whose target block vanilla changed between --old and --new to
    `by_file`."""
    from .changes import Copy
    from .gui import version_window
    from .overrides import block_history, extract_mod_block, find_overrides
    from .tracker import tag_of
    injects = [o for o in find_overrides(mod_root) if "INJECT" in o["type"]
               and (not file or o["file"] == file) and (not block or o["block"] == block)]
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


def _new_definitions(mod_root, base, commits, new_hash, old_tag, file, block, reg, regenerate, it, states,
                     skipped):
    """{mod path: (inserts, decisions, stale entries)} for the mod's script files at a vanilla file's
    path. An insert is (offset, text, label): a top-level definition that vanilla
    added to the file after --old and that the mod lacks. It goes after the nearest
    earlier definition of vanilla that the mod holds, else before the nearest later
    one, else at the end. A definition that --old held and the mod lacks is the
    mod's deletion. It stays deleted, and it is a decision when vanilla changed it.
    The intent store decides each one (decide_definition). A definition the mod
    keeps in another file of its folder is not missing. A generated file goes to
    `regenerate`."""
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
        if (file and r != file) or not new_files.get(r) or old_files.get(r) == new_files[r]:
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
        decisions, put, stale = [], set(), set()
        for lb in sorted(fresh | gone):
            if held(lb):
                continue
            kind = "vanilla_added" if lb in fresh else "removed_changed"
            theirs = " ".join(now[lb].sig.split())
            action, by, why = decide_definition(rel, lb, kind, theirs, it, states, reg, stale)
            decisions.append(merge.Decision([{"key": lb}], kind, action, by, reason=why,
                                            base=" ".join(was[lb].sig.split()) if lb in was else None,
                                            theirs=theirs))
            if action == merge.TAKE:
                put.add(lb)
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
                    sep = "\n" if text[:anchor].endswith("\n") else "\n\n"
                    inserts.append((anchor, sep + body, label))
            elif label != TOP_LEVEL and label in mine_items:
                members = mine_items[label]
                m = members[min(k, len(members) - 1)]
                if pending:
                    at = _comment_start(text, m[1])
                    inserts += [(at, body + "\n", lb) for lb, body in pending]
                    pending = []
                anchor = m[2]
        if pending:
            sep = "" if not text else "\n" if text.endswith("\n") else "\n\n"
            inserts += [(len(text), sep + body, lb) for lb, body in pending]
        if inserts or decisions:
            out[rel] = (inserts, decisions, stale)
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


def _locate(text, copy):
    """The offset of the copy's text in its file, near its first line."""
    lines = text.split("\n")
    if not 1 <= copy.line <= len(lines) + 1:
        return None
    offset = sum(len(x) + 1 for x in lines[:copy.line - 1])
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
        rc = f["removed_check"]
        print(f"- Removed-line check: {'passed' if rc['passed'] else 'FAILED'}")
        fm = f.get("format", {})
        print("- Layout: pdx-format" if fm.get("applied") else
              f"- Layout: {'FAILED, ' if fm.get('failed') else ''}not formatted ({fm.get('why')})")
        for u in rc["unexplained"]:
            print(f"  - line {u['line']} removed without a vanilla change: `{u['text'].strip()}`")
        for e in f["stale_entries"]:
            print(f"- Stale entry {e}: run `pdx-audit intent confirm {e[6:]}` after review")
        for d in f["decisions"]:
            if d["action"] == merge.OPEN:
                where = f"line {d['line']}" if d.get("line") else "new node"
                who = f", written by {d['commit']}" if d.get("commit") else ""
                print(f"  - OPEN {d['kind']} `{' > '.join(s['key'] for s in d['path'])}` ({where}{who})"
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
    if args.apply:
        return _apply(mod_root, args.apply)
    base, commits, new_hash, new_msg = _vanilla(mod_root, args)
    try:
        p = plan(mod_root, base, commits, args.old, new_hash, new_msg, it, args.file, args.block)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    out = Path(args.plan_out) if args.plan_out else (
        store.dir / "merge-plans" / f"{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    p["mod_root"] = str(mod_root)
    out.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.json:
        print(json.dumps({k: v for k, v in p.items()} | {"files": [
            {k: v for k, v in f.items() if k != "merged"} for f in p["files"]]}, ensure_ascii=False, indent=1))
    else:
        _print(p)
        print(f"Plan saved: {out}")
        print(f"Review the diff, then run `pdx-audit merge --old {args.old} --new {args.new} --apply {out}`.")
    return 0


def _apply(mod_root, plan_path):
    p = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    if Path(p.get("mod_root", "")) != Path(mod_root):
        print(f"Error: the plan is for {p.get('mod_root')}, not {mod_root}.", file=sys.stderr)
        return 1
    written, refused = [], []
    for f in p["files"]:
        path = Path(mod_root) / f["file"]
        why = []
        text, bom, crlf = read_mod_file(path)
        if _sha(text) != f["before_sha"]:
            why.append("the file changed after the dry run")
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
        write_mod_file(path, f["merged"], bom, crlf)
        written.append(f["file"])
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
