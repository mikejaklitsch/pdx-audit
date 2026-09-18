"""One copy of vanilla text measured against vanilla's history: diff3's changes as
findings, the block view the app shows, and the per-target detail.

A target is a GUI definition, a replaced GUI file, or a REPLACE block. Every change
whose priority is high or mid is flagged. In a REPLACE each flagged change is one
finding, of kind `override_<change>_<priority>`. In a GUI copy the changes vanilla
made inside one block are one finding: a change joins the outermost block above it
in an unbroken run of blocks that each hold a flagged change of their own, and a
block with several changes is one `gui_block_changed_<priority>` finding. A finding's
key holds the target, the place, and each change's text on both sides with layout
collapsed, so a dismissal holds until any of it changes. Your own edits are info and
are shown nowhere but the counts."""
import json
from collections import namedtuple

from . import diff3, ledger
from .report import Finding, diff_lines, value_pair

FLAGGED = (diff3.HIGH, diff3.MID)
MARK = {diff3.HIGH: "stale", diff3.MID: "review"}

Audited = namedtuple("Audited", "findings flagged base block texts")
Audited.__doc__ = """findings: [Finding], one per flagged change or GUI block. flagged:
[(Change, Finding)] in diff3's order, each change with the finding it belongs to. base:
the version tag the copy matches. block: the block view payload, or None when it was
not asked for. texts: {id(Change): (yours, vanilla)} with layout collapsed."""


def normalized(text, node):
    """A node's text with its layout collapsed, or None."""
    return None if node is None else " ".join(text[node.start:node.end].split())


class _Lines:
    """Line numbers of offsets into one text whose first line is `first`."""

    def __init__(self, text, first):
        self.text, self.first = text, first

    def at(self, offset):
        return self.first + self.text.count("\n", 0, offset)

    def last(self, node):
        return self.at(max(node.end - 1, node.start))

    def col(self, offset):
        return offset - (self.text.rfind("\n", 0, offset) + 1)


def _outer(text, unwrap):
    """The block a REPLACE's top-level statements sit in, or None."""
    if not unwrap:
        return None
    return next((n for n in diff3.nodes(text) if n.kind == "block"), None)


def _enclosing(top):
    """{(start, end) of each Node: (start, end) of the block holding it, None at the top}."""
    out = {}

    def walk(ns, parent):
        for n in ns:
            here = (n.start, n.end)
            out[here] = parent
            if n.children is not None:
                walk(n.children, here)

    walk(top, None)
    return out


def _block_groups(flagged, top):
    """Flagged changes grouped by the block vanilla changed, in order of first change.
    A change joins the outermost block above it in an unbroken run of blocks that each
    hold a flagged change directly; a change at the top level stands alone."""
    enclosing = _enclosing(top)
    direct = {(c.parent.start, c.parent.end) for c in flagged if c.parent is not None}
    groups = {}
    for n, c in enumerate(flagged):
        if c.parent is None:
            key = ("top", n)
        else:
            key = (c.parent.start, c.parent.end)
            while enclosing.get(key) in direct:
                key = enclosing[key]
        groups.setdefault(key, []).append(c)
    return list(groups.values())


def audit(audit_name, name, target, mod_text, versions, tags, file, line, *,
          unwrap=False, want=False, block_type=None, vanilla_file=None,
          dialect=diff3.SCRIPT):
    """Compares one copy with the versions of vanilla, and returns an Audited.

    The oldest version is first, and the last version is the current one. `tags`
    gives the name of each version. `line` is the first line of the copy in `file`.
    `dialect` tells the audit if the text is script or GUI.

    The dialect controls how siblings pair, and how the audit names a place (refer
    to diff3). No two findings of one copy have the same id (refer to
    ledger.distinct). `distinct` does the same for all the copies of one audit."""
    changes = diff3.compare(mod_text, versions, unwrap, dialect)
    base_i = diff3.baseline(mod_text, versions, unwrap, dialect)
    base = tags[base_i] if base_i is not None else None
    new_text = versions[-1]
    lines, outer = _Lines(mod_text, line), _outer(mod_text, unwrap)

    def at(c):
        parent = c.parent or outer
        if c.mod is not None:
            return lines.at(c.mod.start)
        if c.after is not None:
            return lines.at(c.after.start)
        return lines.at(parent.start) if parent is not None else line

    texts = {id(c): (normalized(mod_text, c.mod), normalized(new_text, c.new)) for c in changes}
    flagged = [c for c in changes if c.priority in FLAGGED]
    groups = (_block_groups(flagged, diff3.nodes(mod_text)) if audit_name == "gui"
              else [[c] for c in flagged])
    findings, finding_of = [], {}
    for group in groups:
        first = min(group, key=at)
        since = max((c.since for c in group if c.since is not None), default=None)
        since = tags[since] if since is not None else None
        if len(group) == 1:
            c = group[0]
            yours, vanilla = texts[id(c)]
            key = {"target": target, "path": list(c.path), "yours": yours, "vanilla": vanilla}
            if c.old is not None:
                key["was"] = normalized(versions[c.since - 1], c.old)
            kind, detail = f"{audit_name}_{c.kind}_{c.priority}", " > ".join(c.path)
        else:
            priority = diff3.HIGH if any(c.priority == diff3.HIGH for c in group) else diff3.MID
            path = min((c.path for c in group), key=len)
            key = {"target": target, "path": list(path),
                   "changes": sorted(([f"{c.kind}_{c.priority}", *texts[id(c)]] for c in group),
                                     key=json.dumps)}
            kind, detail = f"{audit_name}_block_changed_{priority}", f"{len(group)} changes"
        f = Finding(kind, name, f"{file}:{at(first)}", detail, None, key, since, base)
        findings.append(f)
        for c in group:
            finding_of[id(c)] = f
    renamed = dict(zip(map(id, findings), ledger.distinct(findings)))
    finding_of = {k: renamed[id(f)] for k, f in finding_of.items()}
    findings = [renamed[id(f)] for f in findings]

    block = None
    if want:
        rows, vlines = [], _Lines(new_text, 1)
        for c in changes:
            f = finding_of.get(id(c))
            row = {"kind": c.kind, "mark": MARK.get(c.priority),
                   "fid": ledger.finding_id(f) if f is not None else None,
                   "since": tags[c.since] if c.since is not None else None,
                   "first": None, "last": None, "cols": None, "anchor": None, "inside": False,
                   "vanilla": None if c.new is None else new_text[c.new.start:c.new.end]}
            if c.new is not None:
                row["vfirst"], row["vlast"] = vlines.at(c.new.start), vlines.last(c.new)
            parent = c.parent or outer
            if c.mod is not None:
                row["first"], row["last"] = lines.at(c.mod.start), lines.last(c.mod)
                row["cols"] = [lines.col(c.mod.start), lines.col(c.mod.end)]
            elif c.after is not None:
                row["anchor"] = lines.last(c.after)
            elif parent is not None:
                row["anchor"], row["inside"] = lines.at((parent.open_end or parent.start + 1) - 1), True
            else:
                row["anchor"] = line - 1
            rows.append(row)
        block = {"type": block_type, "file": file, "line": line, "lines": mod_text.split("\n"),
                 "vanilla_file": vanilla_file, "changes": rows,
                 "vanilla_lines": new_text.split("\n"), "vanilla_tag": tags[-1],
                 "base_lines": (versions[base_i] if base_i is not None else new_text).split("\n"),
                 "base_tag": base or tags[-1]}
        with_data = {id(f): f._replace(data=block) for f in findings}
        finding_of = {k: with_data[id(f)] for k, f in finding_of.items()}
        findings = [with_data[id(f)] for f in findings]
    return Audited(findings, [(c, finding_of[id(c)]) for c in flagged], base, block, texts)


def distinct(results):
    """The Audited copies of one audit with no finding id shared (ledger.distinct), in
    the same order; each copy's findings, flagged changes and block view follow."""
    renamed = iter(ledger.distinct([f for r in results for f in r.findings]))
    out = []
    for r in results:
        swap = {id(f): next(renamed) for f in r.findings}
        findings = [swap[id(f)] for f in r.findings]
        block = next((f.data for f in findings if f.data is not None), r.block)
        out.append(r._replace(findings=findings, flagged=[(c, swap[id(f)]) for c, f in r.flagged], block=block))
    return out


def print_target(title, facts, result, versions, tags, show_diff, dismissed=frozenset()):
    """Prints the detail for one target.

    The output starts with the facts of the target. Then it shows the findings in
    groups by priority, with the high priority group first. Each finding shows its
    location, the version in which vanilla made the change, and the text of both
    sides."""
    print(f"### {title}")
    for label, value in facts:
        print(f"- **{label}:** {value}")
    if result.base:
        print(f"- **Your copy matches:** {result.base}")
    if show_diff and result.base in tags:
        d = diff_lines(versions[tags.index(result.base)], versions[-1], title)
        if len(d) > 1:
            print("```diff")
            print("".join(d).rstrip("\n"))
            print("```")
    held = {}
    for c, f in result.flagged:
        held.setdefault(ledger.finding_id(f), (f, []))[1].append(c)
    shown = {fid: item for fid, item in held.items() if fid not in dismissed}
    hidden = len(held) - len(shown)
    for priority, sym, heading in ((diff3.HIGH, "✗", "high priority"), (diff3.MID, "⚠", "mid priority")):
        group = [(fid, f, cs) for fid, (f, cs) in shown.items() if f.kind.endswith(f"_{priority}")]
        if not group:
            continue
        print(f"  {sym} **{heading}**")
        for fid, f, cs in group:
            where = " > ".join(f.key["path"])
            when = f" *(since {f.since})*" if f.since else ""
            print(f"    [{ledger.short_id(fid)}] `{f.location}`" + (f" {where}" if where else "") + when)
            for c in cs:
                was = normalized(versions[c.since - 1], c.old) if c.old is not None else None
                yours, vanilla = value_pair(c.kind, *result.texts[id(c)], tags[c.since] if c.since is not None else None,
                                            was)
                print(f"        yours:    {yours}")
                print(f"        vanilla:  {vanilla}")
    if hidden:
        print(f"  {hidden} dismissed finding(s) hidden; list them with `pdx-audit --show-dismissed`")
    if not shown:
        print("  ✓ **nothing to do here**")
    print()
