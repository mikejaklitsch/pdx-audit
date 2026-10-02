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
from pathlib import Path

from . import diff3, intent, merge, proposer, session
from .registry import registry


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
        a = intent.attribute(dev, it)
        if a is None:
            return (merge.OPEN, None) if what == "conflict" else (merge.TAKE, None)
        if a.conflict:
            return merge.OPEN, "conflict:" + ",".join(a.conflict)
        if a.by.startswith("entry:") and states.get(a.by[6:], ("recorded",))[0] != "recorded":
            dev_template.stale.add(a.by)
            return merge.OPEN, a.by
        if a.disposition == "keep_mod":
            return merge.KEEP, a.by
        if a.disposition == "take_vanilla":
            return merge.TAKE, a.by
        if a.disposition == "merge" and what != "conflict":
            return merge.TAKE, a.by
        return merge.OPEN, a.by           # merge on a statement, banned, or a grouping
    return decide


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
    copies, devs = intent.collect(mod_root, base, commits, new_hash, new_msg, {file} if file else None, gen)
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
            skipped.append((c.file, f"{old_tag} is not in the history of this copy"))
            continue
        old_i = c.tags.index(old_tag)
        base_i = diff3.baseline(c.mod_text, c.versions[:old_i + 1], c.unwrap, c.dialect)
        if base_i is None:
            continue
        base_text, theirs = c.versions[base_i], c.versions[-1]
        if base_text == theirs:
            continue
        by_file[c.file].append((c, base_i, base_text, theirs))
    _add_injects(mod_root, base, commits, new_hash, old_tag, file, block, reg, by_file, regenerate)
    added = _new_definitions(mod_root, base, commits, new_hash, old_tag, file, block, reg, regenerate)
    files = []
    for rel in sorted(set(by_file) | set(added)):
        items = by_file.get(rel, [])
        path = Path(mod_root) / rel
        text, bom, crlf = read_mod_file(path)
        edits, decisions, unexplained, stale = [], [], [], set()
        for c, base_i, base_text, theirs in items:
            start = _locate(text, c)
            if start is None:
                skipped.append((rel, f"the copy {c.name} at line {c.line} does not read as the audit read it"))
                continue
            tpl = _Template(c, mod_root, reg)
            if c.audit == "inject":
                res = merge.merge_inject(base_text, c.mod_text, theirs, _decider(tpl, it, states, mod_root))
            else:
                res = merge.merge_texts(base_text, c.mod_text, theirs, c.dialect, c.unwrap,
                                        _decider(tpl, it, states, mod_root))
            # An op must not touch a character that the copy holds blank for another
            # definition. Such an op would overwrite that definition.
            if any(text[start + op.start:start + op.end] != c.mod_text[op.start:op.end] for op in res.ops):
                skipped.append((rel, f"the merge of {c.name} at line {c.line} touches another definition"))
                continue
            stale |= tpl.stale
            first_line = text.count("\n", 0, start)
            for d in res.decisions:
                if d.line is not None:
                    d.line += first_line
                decisions.append((d, c.name, c.tags[base_i]))
            unexplained += [(ln + first_line, s) for ln, s in res.unexplained]
            # The merge splices each op, not the whole copy. A copy of a repeated key
            # spans the definitions between its blocks, and they must stay.
            edits += [(start + op.start, start + op.end, op.text) for op in res.ops]
        inserts, new_decisions = added.get(rel, ([], []))
        for pos, ins, label in inserts:
            if any(s < pos < e for s, e, _t in edits):
                skipped.append((rel, f"the new definition {label} falls inside a merged node"))
                continue
            edits.append((pos, pos, ins))
        decisions += [(d, d.path[0]["key"], old_tag) for d in new_decisions]
        wanted = {d.line for d, _n, _b in decisions if d.action == merge.OPEN and d.line}
        blamed = proposer.blame(mod_root, rel, wanted) if wanted else {}
        decisions = [dict(d.to_json(), copy=name, base_version=btag,
                          **({"commit": (blamed.get(d.line) or (None,))[0]} if d.line in blamed else {}))
                     for d, name, btag in decisions]
        merged = text
        # Apply from the end of the file. Two insertions at one offset keep their order.
        for _k, (s, e, new) in sorted(enumerate(edits), key=lambda x: (x[1][0], x[1][1], x[0]), reverse=True):
            merged = merged[:s] + new + merged[e:]
        files.append({"file": rel, "before_sha": _sha(text), "merged": merged, "bom": bom, "crlf": crlf,
                      "diff": merge.unified(rel, text, merged), "decisions": decisions,
                      "open": sum(1 for d in decisions if d["action"] == merge.OPEN),
                      "removed_check": {"passed": not unexplained,
                                        "unexplained": [{"line": ln, "text": s} for ln, s in unexplained]},
                      "stale_entries": sorted(stale)})
    return {"old": old_tag, "new": tag_of(new_msg), "files": files,
            "regenerate": [{"file": f, "tool": t, "hint": reg.regenerate_hint(t)} for f, t in sorted(regenerate.items())],
            "skipped": [{"file": f, "why": w} for f, w in skipped]}


def _add_injects(mod_root, base, commits, new_hash, old_tag, file, block, reg, by_file, regenerate):
    """Add each INJECT whose target block vanilla changed between --old and --new."""
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
    if old_tag not in tags:
        return
    old_i = tags.index(old_tag)
    hist = block_history(base, [window[old_i], window[-1]], sorted({o["category"] for o in injects}),
                         {(o["category"], o["block"]) for o in injects})
    for ov in injects:
        generated, tool = reg.generated_by(ov["file"])
        if generated:
            regenerate[ov["file"]] = tool
            continue
        old_text, new_text = hist.get((ov["category"], ov["block"]), [None, None])
        if new_text is None or old_text == new_text:
            continue
        text = extract_mod_block(mod_root, ov)
        if text is None:
            continue
        c = Copy("inject", ov["block"], f"inject:{ov['category']}/{ov['block']}", text,
                 [old_text, new_text], [tags[old_i], tags[-1]], ov["file"], ov["line"], True,
                 diff3.SCRIPT, None, None, {})
        by_file[ov["file"]].append((c, 0, old_text, new_text))


def _comment_start(text, start):
    """`start`, moved up over the comment lines directly above it."""
    while start > 0:
        prev = text.rfind("\n", 0, start - 1) + 1
        if not text[prev:start - 1].strip().startswith("#"):
            break
        start = prev
    return start


def _new_definitions(mod_root, base, commits, new_hash, old_tag, file, block, reg, regenerate):
    """{mod path: (inserts, decisions)} for the mod's script files at a vanilla file's
    path. An insert is (offset, text, label): a top-level definition that vanilla
    added to the file after --old and that the mod lacks. It goes after the nearest
    earlier definition of vanilla that the mod holds, else before the nearest later
    one, else at the end. A definition that --old held and the mod lacks is the
    mod's deletion. It stays deleted. It is an open decision when vanilla changed it.
    A definition the mod keeps in another file of its folder is not missing. A
    generated file goes to `regenerate`."""
    from pathlib import PurePosixPath
    from .files import SCRIPT_EXTS, TOP_LEVEL, _names, _split, decode, definitions, mod_files, scope_of
    from .gui import version_window
    from .tracker import tag_of
    window = version_window(commits, new_hash)
    old = next((h for h, m in window if tag_of(m) == old_tag), None)
    if old is None:
        return {}
    old_files, new_files = base.files(old), base.files(window[-1][0])
    scripts = [r for r in mod_files(mod_root) if PurePosixPath(r).suffix.lower() in SCRIPT_EXTS]
    pairs = [(r, old_files[r], new_files[r]) for r in scripts if (not file or r == file)
             and old_files.get(r) and new_files.get(r) and old_files[r] != new_files[r]]
    blobs = base.blobs({i for _r, a, b in pairs for i in (a, b)})
    out = {}
    for rel, a, b in pairs:
        if a not in blobs or b not in blobs:
            continue
        old_text = decode(blobs[a]).replace("\r\n", "\n")
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
            if fresh:
                regenerate[rel] = tool
            continue
        elsewhere = set()
        for other in scripts:
            if other != rel and scope_of(other) == scope_of(rel):
                elsewhere |= _names(Path(mod_root) / other)
        held = lambda lb: lb in elsewhere or lb.split(" ", 1)[0] in elsewhere   # noqa: E731
        fresh = {lb for lb in fresh if not held(lb)}
        decisions = [merge.Decision([{"key": lb}], "removed_changed", merge.OPEN,
                                    reason="the mod deleted this definition and vanilla changed it",
                                    base=" ".join(was[lb].sig.split()), theirs=" ".join(now[lb].sig.split()))
                     for lb in sorted(gone) if not held(lb)]
        mine_items = {}
        for it in _split(text):
            mine_items.setdefault(it[0], []).append(it)
        inserts, pending, anchor, count = [], [], None, defaultdict(int)
        for label, start, end, *_rest in _split(new_text):
            k, count[label] = count[label], count[label] + 1
            if label in fresh:
                body = new_text[_comment_start(new_text, start):end].rstrip("\n") + "\n"
                if anchor is None:
                    pending.append((label, body))
                else:
                    sep = "\n" if text[:anchor].endswith("\n") else "\n\n"
                    inserts.append((anchor, sep + body, label))
                decisions.append(merge.Decision([{"key": label}], "vanilla_added", merge.TAKE,
                                                reason="vanilla added this definition after --old",
                                                theirs=body.split("\n", 1)[0].strip()))
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
            out[rel] = (inserts, decisions)
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
    p = plan(mod_root, base, commits, args.old, new_hash, new_msg, it, args.file, args.block)
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
        if f["merged"] == text:
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
    if written:
        print("Run pdx-format on the written files.")
    return 1 if refused else 0
