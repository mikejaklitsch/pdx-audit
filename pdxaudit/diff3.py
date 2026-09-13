"""Where a mod's copy of script or GUI text differs from vanilla, and who made each
difference.

The copy is lined up against vanilla's current text block by block. Siblings pair
in order: the same statement first, then the same key (a block that sets
`name = "..."` is known by that name first, then by its key alone), then the same
distinctive quoted value under another key (vanilla moving `onpressed = "[OnPause]"`
to `on_action = "[OnPause]"`). What is still unpaired pairs by key out of order, so
a moved block still pairs. Layout, comments and the spelling of numbers never count
as a difference.

The copy's baseline is the tracked vanilla version it differs from least; a newer
version must fit strictly better to be chosen. Each difference is attributed with
vanilla's tracked history, looked up at the same place: the keys of the enclosing
blocks plus the statement's own key.

  vanilla_changed   your value is one vanilla had at this place; vanilla has changed it
  vanilla_added     vanilla added the statement after the block existed; you lack it
  vanilla_removed   you carry a statement vanilla had here and has deleted
  both_changed      vanilla changed a statement you changed too, or deleted one you
                    changed, after your baseline
  removed_changed   vanilla changed a statement you deleted, after your baseline
  mod_changed, mod_added, mod_removed
                    your own edits: vanilla never touched the statement, or touched it
                    at or before your baseline, so your copy was made seeing it

A copy still holding an old vanilla value, or a statement vanilla deleted, is flagged
whatever its baseline, since that is vanilla's text and not an edit; so is one whose
key vanilla no longer uses at that place, and vanilla's deleted text inside a block
only the copy has.

A block of your own wrapped around vanilla's statements moves them to a different
place, so they are not linked to vanilla's history there.

Priority: both_changed and removed_changed are high. The other vanilla changes are
high when the block holding them also holds an edit of yours, since they compete
with it, and mid otherwise. Your own edits are info.

History that starts after your copy was made cannot tell your edits from vanilla's
earlier ones: a difference older than the oldest tracked version reads as yours."""
import difflib
import re
from collections import Counter, defaultdict, namedtuple
from decimal import Decimal, InvalidOperation

from pdx_utilities.script_parser import parse, tokenize

from . import session

Change = namedtuple("Change", "kind priority path mod new since parent after")
Change.__doc__ = """One difference between a copy and vanilla's current text.

kind, priority: see the module docstring. path: the keys of the blocks holding it.
mod, new: the Node in the copy and in vanilla's current text (either may be None),
with offsets into its own text. since: index into the history of the version where
vanilla made the change, or None. parent, after: where a statement only vanilla has
belongs in the copy: the copy's enclosing block Node (None at the top) and the
copy's statement it follows (None when it comes first)."""

HIGH, MID, INFO = "high", "mid", "info"
VANILLA_KINDS = ("vanilla_changed", "vanilla_added", "vanilla_removed")
CONFLICT_KINDS = ("both_changed", "removed_changed")
MOD_KINDS = ("mod_changed", "mod_added", "mod_removed")

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


class Node:
    """A statement of parsed text.

    kind: 'stmt' (`key op value`), 'block' (`key [op value] { children }`), 'items'
    (a run of bare values, such as `0.0 0.0 0.0 1.0`) or 'raw' (a block kept as
    text). key: what the statement sets. label: the key, plus `name=...` for a
    block that names itself. value: (op, value) for a stmt, the head (op, value)
    for a block, a tuple for items, the text for raw. start, end: offsets into the
    parsed text; open_end is the end of a block's '{'. sig is equal for nodes that
    read the same; size counts statements."""
    __slots__ = ("kind", "key", "label", "value", "children", "start", "end", "open_end",
                 "sig", "size")

    def __init__(self, kind, key, value, children, start, end, open_end=None, label=None):
        self.kind, self.key, self.label = kind, key, label or key
        self.value, self.children = value, children
        self.start, self.end, self.open_end = start, end, open_end
        if children is None:
            self.sig = hash((kind, self.label, value))
            self.size = 1
        else:
            self.sig = hash((kind, self.label, value, tuple(c.sig for c in children)))
            self.size = 1 + sum(c.size for c in children)

    def __repr__(self):
        return f"Node({self.kind} {self.label!r} {self.value!r})"


def nodes(text):
    """The top-level Nodes of `text`. A run parses each text once."""
    text = text or ""
    return session.memo(("diff3.nodes", text), lambda: _build(
        parse(tokenize(text), text, strict=False, positions=True, keyless=True)))


def _build(parsed):
    out, run = [], []

    def flush():
        if run:
            out.append(Node("items", "@items", tuple(norm_value(n["key"]) for n in run), None,
                            run[0]["_start"], run[-1]["_end"]))
            run.clear()

    for n in parsed:
        kind = n.get("type")
        if kind == "comment":
            continue
        if kind == "node" and n.get("val") is None:
            run.append(n)
            continue
        flush()
        if kind == "raw_block":
            text = " ".join(n["val"].split())
            out.append(Node("raw", text.split(" ", 1)[0], text, None, n["_start"], n["_end"]))
            continue
        op, val, val_key = n.get("op"), n["val"], n.get("val_key")
        key = " ".join(filter(None, (n["key"], n.get("mid_key"),
                                     val_key if op is None else None))) or "{}"
        if isinstance(val, list):
            children = _build(val)
            name = next((c.value[1] for c in children
                         if c.kind == "stmt" and c.key == "name" and c.value[0] == "="), None)
            # `key {` and `key = {` are the same block; `key ?= {` and `key = value {` are not
            head = (None if op in (None, "=") and val_key is None else op, val_key)
            out.append(Node("block", key, head, children, n["_start"], n["_end"],
                            n.get("_open_end", n["_start"]),
                            f"{key} name={name}" if name is not None else None))
        elif val == "PENDING_BLOCK":              # a block pattern its '{' never followed
            out.append(Node("stmt", key, (op, None), None, n["_start"],
                            n["_start"] + len(n["key"])))
        else:
            out.append(Node("stmt", key, (op, norm_value(val)), None, n["_start"], n["_end"]))
    flush()
    return out


def body(top):
    """The children of the first block among top-level Nodes, or the Nodes
    themselves when there is none: a REPLACE block and vanilla's block compare
    by their contents, since their keys differ."""
    return next((n.children for n in top if n.kind == "block"), top)


def _distinctive(node):
    """A stmt's quoted value with at least three letters or digits, or None."""
    if node.kind != "stmt" or not isinstance(node.value[1], str):
        return None
    v = node.value[1]
    return v if v.startswith('"') and len(re.findall(r"\w", v)) >= 3 else None


def align(a, b):
    """Sorted index pairs between sibling Nodes a and b. In the order of both sides,
    each step pairs only between the pairs the steps before it found: the same
    statement, the same label, the same key, then the same distinctive value. Last,
    what is still unpaired pairs by label, then by key, out of order."""
    pairs = []

    def keys(side, idx, level, tag):
        if level == 0:
            return [side[i].sig for i in idx]
        if level == 1:
            return [side[i].label for i in idx]
        if level == 2:
            return [side[i].key for i in idx]
        return [_distinctive(side[i]) or (tag, i) for i in idx]

    def step(ia, ib, level):
        if not ia or not ib or level > 3:
            return
        matcher = difflib.SequenceMatcher(None, keys(a, ia, level, "a"),
                                          keys(b, ib, level, "b"), autojunk=False)
        pa = pb = 0
        for x, y, size in matcher.get_matching_blocks():
            step(ia[pa:x], ib[pb:y], level + 1)
            pairs.extend(zip(ia[x:x + size], ib[y:y + size]))
            pa, pb = x + size, y + size

    step(list(range(len(a))), list(range(len(b))), 0)
    for attr in ("label", "key"):
        taken_a, taken_b = {i for i, _j in pairs}, {j for _i, j in pairs}
        loose_b = defaultdict(list)
        for j, n in enumerate(b):
            if j not in taken_b:
                loose_b[getattr(n, attr)].append(j)
        for i, n in enumerate(a):
            if i not in taken_a and loose_b.get(getattr(n, attr)):
                pairs.append((i, loose_b[getattr(n, attr)].pop(0)))
    return sorted(pairs)


def _settle(mine, theirs, pairs):
    """(indexes in mine, indexes in theirs) of statements, not blocks, that the
    pairing left without an identical partner but that match an identical one on the
    other side left the same way. Statements compare as a multiset within their
    block, so repeated keys and list members never differ by order alone."""
    same = {(i, j) for i, j in pairs if mine[i].sig == theirs[j].sig}
    same_mine, same_theirs = {i for i, _j in same}, {j for _i, j in same}
    loose_mine, loose_theirs = defaultdict(list), defaultdict(list)
    for i, n in enumerate(mine):
        if n.children is None and i not in same_mine:
            loose_mine[n.sig].append(i)
    for j, n in enumerate(theirs):
        if n.children is None and j not in same_theirs:
            loose_theirs[n.sig].append(j)
    settled_mine, settled_theirs = set(), set()
    for sig, idx in loose_mine.items():
        other = loose_theirs.get(sig, [])
        n = min(len(idx), len(other))
        settled_mine.update(idx[:n])
        settled_theirs.update(other[:n])
    return settled_mine, settled_theirs


def distance(mine, theirs):
    """How many statements differ between two lists of sibling Nodes: each one only
    one side has counts its statements, and each lined-up pair that reads differently
    counts one (a pair of blocks, the statements differing inside)."""
    pairs = align(mine, theirs)
    settled_mine, settled_theirs = _settle(mine, theirs, pairs)
    taken_mine = {i for i, _j in pairs} | settled_mine
    taken_theirs = {j for _i, j in pairs} | settled_theirs
    cost = (sum(n.size for i, n in enumerate(mine) if i not in taken_mine)
            + sum(n.size for j, n in enumerate(theirs) if j not in taken_theirs))
    for i, j in pairs:
        m, v = mine[i], theirs[j]
        if i in settled_mine or j in settled_theirs:
            # one matched an identical statement elsewhere; its partner stands alone
            cost += (0 if i in settled_mine else m.size) + (0 if j in settled_theirs else v.size)
        elif m.sig == v.sig:
            continue
        elif m.kind == v.kind == "block" and m.key == v.key:
            cost += (m.value != v.value) + distance(m.children, v.children)
        else:
            cost += 1
    return cost


def baseline(mod_text, versions, unwrap=False):
    """Index of the tracked version the copy differs from least, or None when
    vanilla has no version of the text. Among equals the oldest wins: a newer
    version is the baseline only when the copy fits it strictly better."""
    view = (lambda t: body(nodes(t))) if unwrap else nodes
    mine, best, costs = view(mod_text), None, {}
    for k, text in enumerate(versions):
        if text is None:
            continue
        if text not in costs:
            costs[text] = distance(mine, view(text))
        if best is None or costs[text] < best[0]:
            best = (costs[text], k)
    return best and best[1]


def _index(top):
    """{place: Counter of sigs} for one version, where a place is (enclosing keys,
    key) for a node and (enclosing keys, key, 'head') for a block's head."""
    idx = defaultdict(Counter)

    def walk(ns, path):
        for n in ns:
            idx[(path, n.key)][n.sig] += 1
            if n.children is not None:
                idx[(path, n.key, "head")][hash(n.value)] += 1
                walk(n.children, path + (n.key,))

    walk(top, ())
    return idx


class _History:
    """Vanilla's versions of one text as place indexes, oldest first; the last is
    current. `floor` is where the text's latest unbroken run of versions starts;
    `base` is the copy's baseline."""

    def __init__(self, indexes, base):
        self.idx, self.base = indexes, base
        self.cur = len(indexes) - 1
        self.floor = max((k + 1 for k, x in enumerate(indexes) if x is None), default=0)

    def count(self, k, place, sig=None):
        x = self.idx[k]
        c = x.get(place) if x is not None else None
        if not c:
            return 0
        return sum(c.values()) if sig is None else c.get(sig, 0)

    def _parent_exists(self, k, path):
        return self.idx[k] is not None if not path else self.count(k, (path[:-1], path[-1])) > 0

    def introduced(self, path, place, sig):
        """The version where vanilla put its current number of `sig` at `place`
        inside a block that already existed, or None when they have been there
        since the block appeared."""
        need = self.count(self.cur, place, sig)
        k = self.cur
        while k >= self.floor and self.count(k, place, sig) >= need:
            k -= 1
        k += 1
        if k > self.cur or k <= self.floor or not self._parent_exists(k - 1, path):
            return None
        return k

    def stale(self, place, sig):
        """The version after the newest one that had more of `sig` at `place` than
        current vanilla has, or None when vanilla never had more."""
        now = self.count(self.cur, place, sig)
        for k in range(self.cur - 1, -1, -1):
            if self.count(k, place, sig) > now:
                return k + 1
        return None

    def gone(self, place):
        """The version after the newest one with more statements at `place` than
        current vanilla has, or None when vanilla never had more."""
        now = self.count(self.cur, place)
        for k in range(self.cur - 1, -1, -1):
            if self.count(k, place) > now:
                return k + 1
        return None

    def seen_before_copy(self, since):
        """True when vanilla's change came at or before the copy's baseline."""
        return since is not None and self.base is not None and since <= self.base


def compare(mod_text, versions, unwrap=False):
    """Changes between a copy and vanilla's current text, which is versions[-1].
    versions holds vanilla's text of the same block or file at each tracked version,
    oldest first, None where vanilla had none. unwrap compares the contents of each
    text's first block (a REPLACE against vanilla's block). Returns [Change] in the
    order they occur in vanilla's text, a block's own changes before its contents'."""
    if not versions or versions[-1] is None:
        raise ValueError("vanilla's current text is required")
    view = (lambda t: body(nodes(t))) if unwrap else nodes
    indexes = [None if t is None else session.memo(("diff3.index", t, unwrap),
                                                   lambda t=t: _index(view(t)))
               for t in versions]
    hist = _History(indexes, baseline(mod_text, versions, unwrap))
    out = []
    _walk(view(mod_text), view(versions[-1]), (), None, hist, out)
    return out


def _walk(mine, theirs, path, parent, hist, out):
    pairs = align(mine, theirs)
    of_mine = {i: j for i, j in pairs}
    of_theirs = {j: i for i, j in pairs}
    settled_mine, settled_theirs = _settle(mine, theirs, pairs)
    local, inner = [], []

    def add(kind, m, v, since, after=None, demote=True):
        if demote and kind in CONFLICT_KINDS and hist.seen_before_copy(since):
            kind, since = ("mod_removed" if m is None else
                           "mod_changed" if v is not None else "mod_added"), None
        local.append(Change(kind, None, path, m, v, since, parent, after))

    after = None
    for j, v in enumerate(theirs):
        i = of_theirs.get(j)
        here = after
        if i is not None:
            after = mine[i]
        if j in settled_theirs or (i is not None and mine[i].sig == v.sig):
            continue
        if i is None or i in settled_mine:
            place = (path, v.key)
            k = hist.introduced(path, place, v.sig)
            if k is None:
                add("mod_removed", None, v, None, here)
            elif hist.count(k - 1, place) >= hist.count(hist.cur, place):
                add("removed_changed", None, v, k, here)
            else:
                add("vanilla_added", None, v, k, here)
            continue
        m = mine[i]
        if m.kind == v.kind == "block" and m.key == v.key:
            if m.value != v.value:
                head = (path, v.key, "head")
                kind, since = _classify_pair(hist, path, head, hash(m.value), head, hash(v.value))
                add(kind, m, v, since)
            inner.append((m, v))
        else:
            kind, since = _classify_pair(hist, path, (path, m.key), m.sig, (path, v.key), v.sig)
            add(kind, m, v, since)

    for i, m in enumerate(mine):
        j = of_mine.get(i)
        if i in settled_mine or (j is not None and j not in settled_theirs):
            continue
        place = (path, m.key)
        k = hist.stale(place, m.sig)
        if k is not None:
            add("vanilla_removed", m, None, k)
        elif (k := hist.gone(place)) is not None:
            # a key vanilla no longer uses here at all is flagged whatever the baseline
            add("both_changed", m, None, k, demote=hist.count(hist.cur, place) > 0)
        else:
            add("mod_added", m, None, None)
            if m.children is not None:
                _stale_inside(m, path + (m.key,), hist, out)

    yours = any(c.kind in MOD_KINDS for c in local)
    for c in local:
        if c.kind in MOD_KINDS:
            priority = INFO
        elif c.kind in CONFLICT_KINDS or yours:
            priority = HIGH
        else:
            priority = MID
        out.append(c._replace(priority=priority))
    for m, v in inner:
        _walk(m.children, v.children, path + (v.key,), m, hist, out)


def _stale_inside(block, path, hist, out):
    """Flag what a block only your copy has still carries of vanilla's deleted text:
    statements vanilla had at the same place and has since removed. They compete
    with the block around them, so they are high."""
    for n in block.children:
        k = hist.stale((path, n.key), n.sig)
        if k is not None:
            out.append(Change("vanilla_removed", HIGH, path, n, None, k, block, None))
        elif n.children is not None:
            _stale_inside(n, path + (n.key,), hist, out)


def _classify_pair(hist, path, m_place, m_sig, v_place, v_sig):
    """(kind, since) for a statement of yours lined up with a different one of
    vanilla's."""
    k = hist.stale(m_place, m_sig)
    if k is not None:
        return "vanilla_changed", k
    k = hist.introduced(path, v_place, v_sig)
    if k is not None:
        return "both_changed", k
    return "mod_changed", None
