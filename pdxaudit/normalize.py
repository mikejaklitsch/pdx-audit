"""Line normalization shared by containment checks and fork detection."""
import re

from pdx_utilities.script_parser import parse, tokenize

from . import session


def _norm_clean(line):
    """Whitespace / '='-spacing canonicalization for an already comment-free
    line (see _explode_braces, which strips comments up front)."""
    s = re.sub(r"\s+", " ", line)
    s = re.sub(r"\s*=\s*", " = ", s)
    return s.strip()


def _explode_braces(text):
    """Split `text` so each '{' and '}' outside strings and comments sits on its
    own logical line. Makes single-line (`a = { b }`) and multi-line brace
    formatting compare equal, so a formatter collapsing short blocks onto one
    line cannot masquerade as drift. String- and comment-aware: '#' outside a
    string starts a comment to end of line; braces and '#' inside a "..." literal
    are preserved as content (GUI colour codes like "#R ...#!" stay intact)."""
    out, buf = [], []
    in_str = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if in_str:
            buf.append(c)
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            buf.append(c)
        elif c == '#':  # comment: drop to end of line
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        elif c == '\n':
            out.append("".join(buf)); buf = []
        elif c in "{}":
            out.append("".join(buf)); buf = []
            out.append(c)
        else:
            buf.append(c)
        i += 1
    out.append("".join(buf))
    return out


def _explode_norms(text):
    """Normalized, non-empty logical lines of `text`, brace-granularity
    independent (short `{ }` blocks exploded onto their own lines). Vanilla's
    text for a block repeats across snapshots, so a run normalizes each text once."""
    norms = session.memo(("norms", text), lambda: tuple(
        n for n in (_norm_clean(l) for l in _explode_braces(text)) if n))
    return list(norms)


def statement_norms(text):
    """One normalized entry per statement of `text`, however it is laid out (see
    statement_entries)."""
    return [norm for _at, norm in statement_entries(text)]


def statement_entries(text):
    """[(offset, norm)] per statement of `text`, read with the shared script parser:
    `key = value`; a block's opening (`key =`) with its `{` and `}`; a run of bare
    values joined (`0.0 0.0 0.0 1.0`); a raw block with its spacing collapsed. The
    offset is where the entry starts. Line breaks, indentation, brace placement and
    comments never change an entry. The parser leaves out the contents of a block
    that has no key, which GUI files do not use. A run normalizes each text once."""
    return list(session.memo(("statements", text), lambda: tuple(_statement_entries(text))))


def _statement_entries(text):
    text = text or ""
    out = []

    def walk(nodes):
        run = []                        # consecutive bare values
        for node in nodes + [None]:
            if node is not None and node.get("type") == "comment":
                continue
            if node is not None and node.get("type") == "node" and node.get("val") is None:
                run.append(node)
                continue
            if run:
                out.append((run[0]["_start"], " ".join(n["key"] for n in run)))
                run = []
            if node is None:
                continue
            if node.get("type") == "raw_block":
                out.append((node["_start"], " ".join(node["val"].split())))
                continue
            op, val = node.get("op"), node["val"]
            head = " ".join(filter(None, (node["key"], node.get("mid_key"),
                                          node.get("val_key") if op is None else None)))
            if op:
                head = f"{head} {op}" + (f" {node['val_key']}" if node.get("val_key") else "")
            if isinstance(val, list):
                out.append((node["_start"], head))
                out.append((node.get("_open_end", node["_start"] + 1) - 1, "{"))
                walk(val)
                out.append((node["_end"] - 1, "}"))
            elif val == "PENDING_BLOCK":      # a block pattern its '{' never followed
                out.append((node["_start"], head))
            else:
                out.append((node["_start"], f"{head} {val}"))

    walk(parse(tokenize(text), text, strict=False, positions=True))
    return out
