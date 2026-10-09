"""The rows of the Merge page's bottom view, and the page's merge work in the app's
merge worker process. Nothing here uses Qt, so the worker loads it without the window.

The worker keeps each plan file that the page opens (`load`), with the memo of
merge_cli.build and the line compare of the file's last merged text. A choice then
sends only the choices (`build`), and a click on a row only the decision (`view`).
A call for a file that the worker does not hold returns MISSING; the page then loads
the file and calls again."""
import bisect
import difflib
import functools
import re
from typing import NamedTuple

from . import diff3, intent, merge, merge_cli, results
from .worddiff import word_spans

INDENT = "    "
_TOKEN = re.compile(r'"[^"]*"|[{}]|[<>!?]=|[<>=]|[^\s{}<>=!?"]+|[!?]')
_OPS = {"=", "<", ">", "<=", ">=", "!=", "?="}
# The bottom view shows the outermost block that holds a change and has no more than
# BLOCK_LINES lines. When no block is that short, it shows WINDOW lines on each side.
BLOCK_LINES = 150
WINDOW = 12
# The lines of context that the line compare reads around each changed part.
CONTEXT = 8


def layout(text):
    """Script text that the plan holds on one line, laid out with one statement on a
    line and the contents of each block indented. A short block of plain values, such
    as `size { 2 2 }`, stays on one line."""
    toks = _TOKEN.findall(text or "")
    out, line, depth, i = [], [], 0, 0

    def flush():
        if line:
            out.append(INDENT * depth + " ".join(line))
            line.clear()
    while i < len(toks):
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if t == "{":
            j, d = i + 1, 1
            while j < len(toks) and d:
                d += (toks[j] == "{") - (toks[j] == "}")
                j += 1
            inner = toks[i + 1:j - 1]
            if len(inner) <= 6 and not any(x in _OPS or x in "{}" for x in inner):
                line.append("{ " + " ".join(inner) + " }" if inner else "{ }")
                i = j
                continue
            line.append("{")
            flush()
            depth += 1
        elif t == "}":
            flush()
            depth = max(depth - 1, 0)
            line.append("}")
            flush()
        else:
            if line and t not in _OPS and line[-1] not in _OPS and (nxt in _OPS or nxt == "{"):
                flush()
            line.append(t)
        i += 1
    flush()
    return "\n".join(out)


def _lines(text):
    out = text.split("\n")
    return out[:-1] if out and out[-1] == "" else out


def _line(text, offset):
    return text.count("\n", 0, offset) + 1


def _offset(text, line):
    """The offset of the first character on `line` that is not blank, or None."""
    at = 0
    for _ in range(line - 1):
        at = text.find("\n", at) + 1
        if not at:
            return None
    while at < len(text) and text[at] in " \t":
        at += 1
    return at


class Row(NamedTuple):
    """One line of `diff_rows`. sign: " " a line that stays, "-" a line that Apply file
    deletes, "+" a line that it adds. mine and theirs: the line's number in your file
    and in the merged file. text: the line (your text for " "); after: the merged text
    of a " " line; partner: for "+", your line number of the line it replaces."""
    sign: str
    mine: int
    theirs: int
    text: str
    after: str = None
    partner: int = None


def diff_rows(ours, merged):
    """[Row] for each line of the file, in order. A fast compare of the raw lines finds
    the changed parts; each part, with CONTEXT lines around it, then compares the way the
    Audit page's side-by-side view compares lines (results._diff)."""
    a, b = _lines(ours), _lines(merged)
    rows, i, j = [], 0, 0
    for group in difflib.SequenceMatcher(None, a, b).get_grouped_opcodes(CONTEXT):
        i1, j1, i2, j2 = group[0][1], group[0][3], group[-1][2], group[-1][4]
        rows += [Row(" ", i + k + 1, j + k + 1, a[i + k], b[j + k]) for k in range(i1 - i)]
        rows += _part_rows(a, b, i1, i2, j1, j2)
        i, j = i2, j2
    rows += [Row(" ", i + k + 1, j + k + 1, a[i + k], b[j + k]) for k in range(len(a) - i)]
    return rows


def _part_rows(a, b, i1, i2, j1, j2):
    """[Row] for a[i1:i2] against b[j1:j2]."""
    a, b = a[i1:i2], b[j1:j2]
    same, _deleted, inserted, partner = results._diff(results._line_keys(a), results._line_keys(b))
    rows = []
    for i in range(len(a) + 1):
        rows += [Row("+", None, j1 + j + 1, b[j], partner=i1 + partner[j] + 1 if j in partner else None)
                 for j in inserted.get(i, [])]
        if i < len(a):
            rows.append(Row(" ", i1 + i + 1, j1 + same[i] + 1, a[i], b[same[i]]) if i in same
                        else Row("-", i1 + i + 1, None, a[i]))
    return rows


@functools.lru_cache(maxsize=4)
def _nodes(text):
    """diff3.nodes, kept for the last texts: the rows of one file share them."""
    return diff3.nodes(text)


def _holders(text, offset):
    """The nodes of `text` that hold `offset`, the top level first."""
    out, level = [], _nodes(text)
    while level:
        hit = next((n for n in level if n.start <= offset < n.end), None)
        if hit is None:
            break
        out.append(hit)
        level = hit.children
    return out


def _named(d, text, gui):
    """(node, exact) for the decision's node in `text`: exact is False when only the
    block of its copy is found. (None, False) when neither is found."""
    top = _nodes(text)
    copy = d.get("copy") or ""
    roots = [n for n in _blocks(top) if re.split(r"[\s:]", n.key)[-1] == copy]
    path = (d.get("address") or {}).get("path") or d.get("path") or []
    # A node that the text does not hold shows the block that holds its place.
    for n in range(len(path), 0, -1):
        for level in [r.children for r in roots] + [top]:
            node, _loose = intent.resolve(level, path[:n], diff3.GUI if gui else diff3.SCRIPT)
            if isinstance(node, diff3.Node):
                return node, n == len(path)
    return (roots[0], not path) if roots else (None, False)


def _blocks(level):
    """Each block of `level` and the blocks in it, in the order of the text."""
    for n in level or ():
        if n.kind == "block":
            yield n
            yield from _blocks(n.children)


def locate(d, ours, merged, gui):
    """(side, first line, last line) of the decision's node: side "ours" when the lines
    are in your file, "merged" when they are in the file that Apply file writes. A node
    that your file does not hold is at the line of your file where the merge puts it
    (`at`), where option_rows shows vanilla's text; a search by its path can find a
    node of the same name elsewhere. Else the block of its copy; None when no file
    holds that block."""
    if d.get("line"):
        at = _offset(ours, d["line"])
        if at is not None:
            here = [n for n in _holders(ours, at) if _line(ours, n.start) == d["line"]]
            return "ours", d["line"], max(_line(ours, here[0].end - 1) if here else 0, d["line"])
    if d.get("at"):
        return "ours", d["at"], d["at"]
    fallback = None
    for side, text in (("merged", merged), ("ours", ours)):
        node, exact = _named(d, text, gui)
        if node is None:
            continue
        first = _line(text, node.start)
        if exact:
            return side, first, _line(text, node.end - 1)
        fallback = fallback or (side, first, first)
    return fallback


def block_of(d, ours, merged, gui, rows=None):
    """(rows, selected): the `diff_rows` of the block that holds the decision's node, and
    the indexes in them of the node's own rows, with the lines that replace them or that
    they replace. None when the node is not found."""
    rows = diff_rows(ours, merged) if rows is None else rows
    at = locate(d, ours, merged, gui)
    if at is None:
        return None
    side, first, last = at
    text = ours if side == "ours" else merged
    col = 1 if side == "ours" else 2
    # The block must hold a line of your file, so a node that the merge adds whole
    # shows the block that holds it, not only its own new lines.
    yours = sorted(r[col] for r in rows if r[col] is not None and r.mine is not None)
    has_yours = lambda b: bisect.bisect_right(yours, b[1]) > bisect.bisect_left(yours, b[0])   # noqa: E731
    blocks = [(_line(text, n.start), _line(text, n.end - 1))
              for n in _holders(text, _offset(text, first) or 0) if n.kind == "block"]
    fit = [b for b in blocks if b[1] - b[0] < BLOCK_LINES and has_yours(b)]
    lo, hi = fit[0] if fit else (first - WINDOW, last + WINDOW)
    index = {r[col]: i for i, r in enumerate(rows) if r[col] is not None}
    if not index:
        return None
    end = max(index)
    lo, hi = max(min(lo, first), 1), min(max(hi, last), end)
    sel = {i for i, r in enumerate(rows) if r[col] is not None and first <= r[col] <= last}
    # A line that Apply file replaces shows its new line too, and the other way round.
    mine = ({rows[i].mine for i in sel} | {rows[i].partner for i in sel}) - {None}
    sel |= {i for i, r in enumerate(rows) if (r.sign == "+" and r.partner in mine)
            or (r.sign == "-" and r.mine in mine)}
    i_lo, i_hi = min([index[lo], *sel]), max([index[hi], *sel])
    return rows[i_lo:i_hi + 1], {i - i_lo for i in sel}


SELECTED = "selected"           # the fid of the rows of the selected row's decision


@functools.lru_cache(maxsize=4)
def _node_ends(text):
    """{line: last line} of the outermost node that starts on each line of `text`."""
    starts = merge_cli._line_starts(text)
    line = lambda offset: bisect.bisect_right(starts, offset)   # noqa: E731
    ends = {}

    def walk(level):
        for n in level or ():
            ends.setdefault(line(n.start), line(n.end - 1))
            walk(n.children)
    walk(_nodes(text))
    return ends


def _ident(d):
    return d.get("kind"), d.get("line"), d.get("at"), repr(d.get("address"))


@functools.lru_cache(maxsize=4)
def _starts(text):
    return merge_cli._line_starts(text)


def _text_lines(text):
    """The lines of `text`; none for blank text, and no empty line after a last newline."""
    if not text.strip():
        return []
    return _lines(text)


def option_rows(f, part, d=None):
    """The BlockView side-by-side rows of `part`, the diff_rows of a stretch of plan
    file `f`. The left side holds the options: your lines, with vanilla's text of each
    decision under the lines of its node (or at the line where the merge puts a node
    your file does not hold). Your lines of a decision are red (-) and vanilla's text
    green (+). The right side holds the outcome: the file that Apply file writes, with
    each line that your file does not hold green. Lines that read the same share a
    row. The rows of decision `d` get the fid SELECTED, which the view outlines."""
    text = f["gathered"]["ours"]
    ours, starts = _lines(text), _starts(text)
    line_of = lambda offset: bisect.bisect_right(starts, offset)   # noqa: E731
    mine = [r.mine for r in part if r.sign != "+"]
    lo, hi = (mine[0], mine[-1]) if mine else (1, 0)
    ends, unit = _node_ends(text), results._indent_unit(ours[lo - 1:hi])
    want = _ident(d) if d else None
    covered, before, after = {}, {}, {}      # {line: [(vanilla's lines, selected, one line)]}

    def cover(first, last, sel):
        for ln in range(max(first, lo), min(last, hi) + 1):
            covered[ln] = covered.get(ln, False) or sel
    for x in f["decisions"]:
        sel = want is not None and _ident(x) == want
        if x.get("span") is not None and x.get("vanilla") is not None:
            # Vanilla's text as the merge writes it: the lines of the span, with the
            # span replaced by the op's text.
            a, b = x["span"]
            if a == b:
                ln = x.get("at") or line_of(a)
                if lo <= ln <= hi + 1:
                    before.setdefault(ln, []).append((_text_lines(x["vanilla"].strip("\n")), sel, None))
                continue
            first, last = line_of(a), line_of(b - 1)
            if last < lo or first > hi:
                continue
            cover(first, last, sel)
            end = starts[last] - 1 if last < len(starts) else len(text)
            new = text[starts[first - 1]:a] + x["vanilla"] + text[b:max(end, b)]
            after.setdefault(min(last, hi), []).append((_text_lines(new), sel, first if first == last else None))
        elif x.get("line") and lo <= x["line"] <= hi:
            last = min(max(ends.get(x["line"], x["line"]), x["line"]), hi)
            cover(x["line"], last, sel)
            if x.get("theirs"):
                base = results._indent(ours[x["line"] - 1])
                after.setdefault(last, []).append(([base + t for t in layout(x["theirs"]).split("\n")], sel,
                                                   x["line"] if x["line"] == last else None))
        elif x.get("at") and lo <= x["at"] <= hi + 1 and x.get("theirs"):
            # The node goes after the line above its place: a sibling of a statement or of
            # a block that line closes, or the first child of a block that line opens.
            prev = next((ours[k] for k in range(x["at"] - 2, -1, -1) if ours[k].strip()), "")
            base = results._indent(prev) + (unit if prev.rstrip().endswith("{") else "")
            before.setdefault(x["at"], []).append(([base + t for t in layout(x["theirs"]).split("\n")], sel, None))
    left = []                        # [state, line, text, selected, emph]

    def ghosts(items):
        for texts, sel, one_line in items:
            emph = None
            if one_line and len(texts) == 1 and left and left[-1][1] == one_line:
                left[-1][4], emph = word_spans(left[-1][2], texts[0])
            left.extend(["add", None, t, sel, emph] for t in texts)
    for ln in range(lo, hi + 1):
        ghosts(before.get(ln, ()))
        left.append(["del" if ln in covered else "same", ln, ours[ln - 1], covered.get(ln, False), None])
        ghosts(after.get(ln, ()))
    ghosts(before.get(hi + 1, ()))       # a node that the merge puts after the last line
    right = [("same", r.theirs, r.after) if r.sign == " " else ("add", r.theirs, r.text)
             for r in part if r.sign != "-"]

    def cell(n, text, state, emph=None):
        return {"n": n, "text": text, "state": state, "quiet": not text.split("#")[0].strip(), "emph": emph}
    pairs = []
    keys = lambda texts: [results._line_key(t) for t in texts]   # noqa: E731
    matcher = difflib.SequenceMatcher(None, keys(x[2] for x in left), keys(x[2] for x in right))
    for _tag, i1, i2, j1, j2 in matcher.get_opcodes():
        for m in range(max(i2 - i1, j2 - j1)):
            pairs.append((left[i1 + m] if i1 + m < i2 else None, right[j1 + m] if j1 + m < j2 else None))
    rows = []
    for a, b in pairs:
        rows.append({"right": a and cell(a[1], a[2], a[0], a[4]), "left": b and cell(b[1], b[2], b[0]),
                     "mark": None, "fid": SELECTED if a and a[3] else None, "id": None, "cause": None,
                     "lead": False})
    return rows


def changed(row):
    """True for a side row with a line that Apply file deletes or adds."""
    return any(c and c["state"] != "same" and not c["quiet"] for c in (row["left"], row["right"]))


# --- your own text ---------------------------------------------------------------------

def own_place(text, span):
    """(start, end): the place of the mod text `text` that your own text for a change
    at `span` takes. It is the outermost block that holds the change and has fewer
    than BLOCK_LINES lines, the block that the bottom view shows. When no block is that
    short, it is the whole lines of the change; for a node that your file does not
    hold, the point where the merge puts it."""
    a, b = span
    blocks = [n for n in _holders(text, a) if n.kind == "block" and n.start <= a and b <= n.end
              and (a < b or n.start < a)]
    fit = [n for n in blocks if _line(text, n.end - 1) - _line(text, n.start) < BLOCK_LINES]
    if fit:
        return fit[0].start, fit[0].end
    if a == b:
        return a, b
    end = text.find("\n", b) if text[b - 1:b] != "\n" else b
    return text.rfind("\n", 0, a) + 1, len(text) if end < 0 else end


def own_frame(text, start):
    """(indent, mid) for a place that starts at `start` of `text`: the indentation of
    its first line, and True when the place starts after that indentation or after
    other text, so that its first line holds no indentation of its own."""
    first = text.rfind("\n", 0, start) + 1
    end = text.find("\n", first)
    line = text[first:end if end >= 0 else len(text)]
    return line[:len(line) - len(line.lstrip(" \t"))], start > first


def to_body(raw, frame):
    """`raw` without the indentation of its place, for the editor. from_body puts it
    back. A line with only white space stays as it is."""
    indent, mid = frame
    return "\n".join(ln if (k == 0 and mid) or not ln.strip() or not ln.startswith(indent) else ln[len(indent):]
                     for k, ln in enumerate(raw.split("\n")))


def from_body(body, frame):
    """The text for the file from the editor's `body` (see to_body)."""
    indent, mid = frame
    return "\n".join(ln if (k == 0 and mid) or not ln.strip() else indent + ln
                     for k, ln in enumerate(body.split("\n")))


def _region(text, s, e, edits):
    """(text, edges): the text that the place s..e of `text` becomes in the file that
    the merge makes with `edits` (file_edits' edits), as merge.splice writes it, and
    [before, after]: True when an edit in the place starts on its first line or ends
    on its last line, so that it touches the blank run above or below the place."""
    kept, _overlaps = merge_cli.kept_edits(edits)
    holds = lambda x: merge_cli.inside(x[:2], s, e)   # noqa: E731
    a, b = merge.spliced_offsets(kept, s, e, holds)
    raw, spans, order = merge._splice_raw(text, kept)
    inner = [spans[n] for n, k in enumerate(order) if holds(kept[k])]
    line = lambda pos: raw.count("\n", 0, pos)   # noqa: E731
    edges = [any(line(x) <= line(a) for x, _z in inner), any(line(z) >= line(b) for _x, z in inner)]
    out, _spans, (a, b) = merge.splice(text, kept, [a, b])
    return out[a:b], edges


# --- the page's merge work in the worker process ----------------------------------------

MISSING = "missing"
_files = {}                   # {(plan token, file): {"f": plan file, "memo": {}, "rows": (merged, rows)}}


def load(key, f):
    """Keep plan file `f` under `key` (plan token, file), and compare its merged text
    with the file now, ready for the first view."""
    _files[key] = {"f": f, "memo": {}, "rows": None}
    _rows(_files[key])
    return True


def forget(token):
    """Drop the files of the plan `token`."""
    for key in [k for k in _files if k[0] == token]:
        del _files[key]
    return True


def build(key, choices):
    """merge_cli.build for the file `key` and the page's choices: (the plan file
    without its gathered part, the skipped rows), or MISSING."""
    st = _files.get(key)
    if st is None:
        return MISSING
    work = dict(st["f"])
    skipped = merge_cli.build([work], choices, st["memo"])
    st["f"] = work
    return {k: v for k, v in work.items() if k != "gathered"}, skipped


def view(key, d):
    """The bottom view of the file `key`: ("file", rows, visible rows) for the whole
    file when `d` is None, ("block", rows, rows) for the block of decision `d`,
    ("none", None, None) when no file holds its text, or MISSING."""
    st = _files.get(key)
    if st is None:
        return MISSING
    f, rows = st["f"], _rows(st)
    if d is None:
        sides = option_rows(f, rows)
        return "file", sides, results.fold_rows(sides, anchor=changed)
    found = block_of(d, f["gathered"]["ours"], f["merged"], f["file"].endswith(".gui"), rows)
    if found is None:
        return "none", None, None
    sides = option_rows(f, found[0], d)
    return "block", sides, sides


def own_block(key, d):
    """What the editor of your own text for decision `d` of the file `key` shows, or
    MISSING: {"span", "lines", "frame", "mine", "result", "vanilla", "own", "problem", "edges"}.
    It edits your own text that holds the change, else a new one at own_place, made
    larger until no edit of the merge crosses its edges. The texts are in the editor's
    form (to_body): your lines, the lines as Apply file writes them with your other
    choices, your lines with each vanilla change in them taken, and your own text.
    `problem` is why the text cannot go in, or "". `edges`: see merge_cli.own_entries;
    with them, the result saved as it is gives the file of your other choices."""
    st = _files.get(key)
    if st is None:
        return MISSING
    f = st["f"]
    text = f["gathered"]["ours"]
    rec = dict(f.get("choices") or {"all": None, "nodes": {}})
    owns = merge_cli.own_entries(rec.get("own"), text)
    span = d.get("own") or d["span"]
    hit = next((o for o in owns if o["span"] == span or merge_cli.inside(span, *o["span"])), None)
    s, e = hit["span"] if hit else own_place(text, span)
    rec["own"] = []
    now = merge_cli.file_edits(f, rec, st["memo"])
    problem = ""
    if hit is None:
        while True:
            cross = [x for x in now["edits"] if x[0] < e and x[1] > s and not merge_cli.inside(x[:2], s, e)]
            if not cross:
                break
            s, e = min([s] + [x[0] for x in cross]), max([e] + [x[1] for x in cross])
        other = next((o for o in owns if o["span"][0] < e and s < o["span"][1]), None)
        if other is not None:
            first, last = merge_cli._own_lines(text, *other["span"])
            problem = f"it overlaps your own text for lines {first} to {last}; remove that text first"
    problem = problem or merge_cli.own_check(now, s, e)
    vanilla = merge_cli.file_edits(f, {"all": merge.TAKE, "nodes": {}, "own": []}, st["memo"])
    result, edges = _region(text, s, e, now["edits"])
    texts = {"mine": text[s:e], "result": result, "vanilla": _region(text, s, e, vanilla["edits"])[0],
             "own": hit["text"] if hit else None}
    frame = own_frame(text, s)
    if any(t is not None and from_body(to_body(t, frame), frame) != t for t in texts.values()):
        frame = ("", False)                  # the editor shows the text as the file holds it
    out = {k: None if t is None else to_body(t, frame) for k, t in texts.items()}
    return dict(out, span=[s, e], lines=list(merge_cli._own_lines(text, s, e)), frame=frame, problem=problem,
                edges=hit["edges"] if hit else edges)


def _rows(st):
    """diff_rows of the file and its merged text, made again when the text changes."""
    f = st["f"]
    if st["rows"] is None or st["rows"][0] != f["merged"]:
        st["rows"] = (f["merged"], diff_rows(f["gathered"]["ours"], f["merged"]))
    return st["rows"][1]
