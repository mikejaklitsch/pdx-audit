"""Three-way classification of a REPLACE block: vanilla before, the mod's copy,
and vanilla after.

Blocks are parsed into statements keyed by (key path, key), so a value is
matched wherever it sits in the block and formatting never matters. Each vanilla
change is classified by what the mod did with the same key:

  frozen         mod value equals vanilla's old value; vanilla changed it
  new_line       vanilla added a line (or whole sub-block) the mod lacks
  kept_removed   mod carries, unchanged, a line vanilla deleted
  both_changed   mod customized the value and vanilla changed it too
  commented_out  the missing line exists as a comment in the mod block
  key_removed    the mod deleted the key vanilla changed
  merged         the mod already has vanilla's new line
  unclassified   a changed value among repeated keys that cannot be matched

Repeated keys in one block (list members such as `religion ?= x`) compare as a
multiset, so their order never matters."""
import re
from collections import Counter, defaultdict, namedtuple
from decimal import Decimal, InvalidOperation

from . import session

LineResult = namedtuple("LineResult", "cls path key op old new mod text ops")
LineResult.__new__.__defaults__ = ((None, None, None),)   # (old op, new op, mod op)

ACTIONABLE = {"frozen": "stale", "new_line": "stale", "kept_removed": "stale",
              "both_changed": "review", "unclassified": "review"}

_TWO_CHAR_OPS = ("?=", "==", "!=", "<=", ">=")
OPS = set(_TWO_CHAR_OPS) | {"=", "<", ">"}
_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)")


def norm_value(v):
    """Canonical spelling for a value: numbers lose insignificant zeros."""
    if _NUMBER.fullmatch(v):
        try:
            s = format(Decimal(v).normalize(), "f")
        except InvalidOperation:
            return v
        return "0" if s in ("-0", "+0") else s.lstrip("+")
    return v


def tokenize(text):
    """Tokens of Paradox script: braces, operators, quoted strings and words.
    '#' outside a quoted string starts a comment to the end of the line."""
    toks = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "#":
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if c.isspace():
            i += 1
            continue
        if c in "{}":
            toks.append(c)
            i += 1
            continue
        if c == '"':
            j = text.find('"', i + 1)
            j = n - 1 if j == -1 else j
            toks.append(text[i:j + 1])
            i = j + 1
            continue
        if text[i:i + 2] in _TWO_CHAR_OPS:
            toks.append(text[i:i + 2])
            i += 2
            continue
        if c in "=<>":
            toks.append(c)
            i += 1
            continue
        j = i
        while (j < n and not text[j].isspace() and text[j] not in '{}=<>#"'
               and text[j:j + 2] not in _TWO_CHAR_OPS):
            j += 1
        if j == i:
            j = i + 1
        toks.append(text[i:j])
        i = j
    return toks


def parse(tokens):
    """Nested entries: a list of (key, op, value), where value is a string or a
    list of entries; bare list members are (None, None, token)."""
    pos = 0

    def block():
        nonlocal pos
        out = []
        while pos < len(tokens):
            t = tokens[pos]
            if t == "}":
                pos += 1
                return out
            if t == "{":
                pos += 1
                out.append((None, None, block()))
                continue
            if pos + 1 < len(tokens) and tokens[pos + 1] in OPS and t not in OPS:
                key, op = t, tokens[pos + 1]
                pos += 2
                if pos < len(tokens) and tokens[pos] == "{":
                    pos += 1
                    out.append((key, op, block()))
                elif pos < len(tokens) and tokens[pos] != "}":
                    out.append((key, op, tokens[pos]))
                    pos += 1
                else:
                    out.append((key, op, ""))
                continue
            if t not in OPS:
                out.append((None, None, t))
            pos += 1
        return out

    return block()


def body(text):
    """The entries inside a `name = { ... }` block text (the first block entry),
    or the top-level entries when the text holds no block."""
    entries = parse(tokenize(text or ""))
    for _key, _op, val in entries:
        if isinstance(val, list):
            return val
    return entries


class Flat:
    """A parsed block flattened to scalar statements and bare list members,
    each keyed by the path of enclosing block keys."""

    def __init__(self, entries):
        self.scalars = defaultdict(list)   # (path, key) -> [(op, value)]
        self.items = defaultdict(list)     # path -> [token]
        self.paths = {()}
        self._walk(entries, ())

    def _walk(self, entries, path):
        for key, op, val in entries:
            if isinstance(val, list):
                seg = key if key is not None else "*"
                if op not in (None, "="):
                    seg = f"{seg} {op}"
                sub = path + (seg,)
                self.paths.add(sub)
                self._walk(val, sub)
            elif key is None:
                self.items[path].append(norm_value(val))
            else:
                self.scalars[(path, key)].append((op, norm_value(val)))

    def subtree(self, prefix):
        n = len(prefix)
        scalars = sorted((p[n:], k, tuple(sorted(v)))
                         for (p, k), v in self.scalars.items() if p[:n] == prefix)
        items = sorted((p[n:], tuple(sorted(v)))
                       for p, v in self.items.items() if p[:n] == prefix)
        paths = sorted(p[n:] for p in self.paths if p[:n] == prefix)
        return tuple(scalars), tuple(items), tuple(paths)

    def line_count(self, prefix):
        n = len(prefix)
        return (sum(len(v) for (p, _k), v in self.scalars.items() if p[:n] == prefix)
                + sum(len(v) for p, v in self.items.items() if p[:n] == prefix))


def _comment_part(line):
    in_str = False
    for i, c in enumerate(line):
        if c == '"':
            in_str = not in_str
        elif c == "#" and not in_str:
            return line[i + 1:]
    return None


def comment_statements(text):
    """(statements, list members) written in comments of a mod block, so a line
    the modder commented out can be recognized."""
    stmts, items = set(), set()
    for line in (text or "").split("\n"):
        comment = _comment_part(line)
        if not comment:
            continue
        flat = Flat(parse(tokenize(comment)))
        for (_p, key), vals in flat.scalars.items():
            for op, val in vals:
                stmts.add((key, op, val))
        for vals in flat.items.values():
            items.update(vals)
    return stmts, items


def _seg_key(seg):
    return seg.split(" ")[0]


def _single(path, key, o, n, m, comments):
    """Classify one key whose old, new and mod values are each a single
    (op, value) or None."""
    op = (n or o or m)[0]
    shown = n or o
    text = f"{key} {shown[0]} {shown[1]}"
    if o and n:
        if m == n:
            cls = "merged"
        elif m == o:
            cls = "frozen"
        elif m is None:
            cls = ("commented_out" if (key,) + n in comments or (key,) + o in comments
                   else "key_removed")
        else:
            cls = "both_changed"
    elif n:
        if m == n:
            cls = "merged"
        elif m is not None:
            cls = "both_changed"
        elif (key,) + n in comments:
            cls = "commented_out"
        else:
            cls = "new_line"
    else:
        if m == o:
            cls = "kept_removed"
        elif m is not None:
            cls = "both_changed"
        else:
            return None
    return LineResult(cls, path, key, op, o[1] if o else None, n[1] if n else None,
                      m[1] if m else None, text,
                      (o[0] if o else None, n[0] if n else None, m[0] if m else None))


def classify(mod_text, old_text, new_text):
    """LineResults for every vanilla change between old_text and new_text,
    classified against the mod's copy. Informational classes are included."""
    # Adoption scoring classifies the same texts against each other at every
    # snapshot, so a run parses each text once. Nothing below mutates them.
    M, O, N = (session.memo(("flat", t or ""), lambda t=t: Flat(body(t)))
               for t in (mod_text, old_text, new_text))
    comments, comment_items = session.memo(("comments", mod_text or ""),
                                           lambda: comment_statements(mod_text))
    results = []

    def topmost(paths, pred):
        chosen = [p for p in paths if pred(p)]
        chosen_set = set(chosen)
        return sorted(p for p in chosen
                      if not any(p[:k] in chosen_set for k in range(1, len(p))))

    new_groups = topmost(N.paths - O.paths, lambda p: p not in M.paths)
    kept_groups = topmost(O.paths - N.paths, lambda p: p in M.paths)
    grouped = set(new_groups) | set(kept_groups)

    for p in new_groups:
        key = _seg_key(p[-1])
        results.append(LineResult("new_line", p[:-1], key, None, None, None, None,
                                  f"{p[-1]} = {{ … }} ({N.line_count(p)} lines)"))
    for p in kept_groups:
        key = _seg_key(p[-1])
        cls = "kept_removed" if M.subtree(p) == O.subtree(p) else "both_changed"
        results.append(LineResult(cls, p[:-1], key, None, None, None, None,
                                  f"{p[-1]} = {{ … }} ({O.line_count(p)} lines)"))

    def covered(path):
        return any(path[:k] in grouped for k in range(1, len(path) + 1))

    for (p, key) in sorted(set(O.scalars) | set(N.scalars)):
        if covered(p):
            continue
        ov, nv, mv = O.scalars.get((p, key), []), N.scalars.get((p, key), []), \
            M.scalars.get((p, key), [])
        oc, nc, mc = Counter(ov), Counter(nv), Counter(mv)
        if oc == nc:
            continue
        if len(ov) <= 1 and len(nv) <= 1 and len(mv) <= 1:
            r = _single(p, key, ov[0] if ov else None, nv[0] if nv else None,
                        mv[0] if mv else None, comments)
            if r:
                results.append(r)
            continue
        added, removed = nc - oc, oc - nc
        for op, val in sorted(added):
            if mc[(op, val)]:
                cls = "merged"
            elif (key, op, val) in comments:
                cls = "commented_out"
            elif removed:
                cls = "unclassified"
            else:
                cls = "new_line"
            results.append(LineResult(cls, p, key, op, None, val, None, f"{key} {op} {val}",
                                      (None, op, None)))
        if not added:
            for op, val in sorted(removed):
                if mc[(op, val)]:
                    results.append(LineResult("kept_removed", p, key, op, val, None, val,
                                              f"{key} {op} {val}", (op, None, op)))

    for p in sorted(set(O.items) | set(N.items)):
        if covered(p):
            continue
        oc, nc, mc = Counter(O.items.get(p, [])), Counter(N.items.get(p, [])), \
            Counter(M.items.get(p, []))
        for val in sorted(nc - oc):
            cls = "merged" if mc[val] else ("commented_out" if val in comment_items else "new_line")
            results.append(LineResult(cls, p, "@item", None, None, val, None, val))
        for val in sorted(oc - nc):
            if mc[val]:
                results.append(LineResult("kept_removed", p, "@item", None, val, None, val, val))
    return results


def classify_scalar(mod_value, old_value, new_value):
    """Classify a single-value REPLACE (`REPLACE:name = value`). Returns a
    LineResult, or None when vanilla's value did not change."""
    wrap = lambda v: None if v is None else ("=", norm_value(str(v).strip()))
    o, n, m = wrap(old_value), wrap(new_value), wrap(mod_value)
    if o == n:
        return None
    return _single((), "", o, n, m, set())
