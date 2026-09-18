"""Where a mod's copy of script or GUI text differs from vanilla, and who made each
difference.

The copy is lined up against vanilla's current text block by block. Siblings pair
in order: the same statement first, then the same name (a block that sets
`name = "..."`), then in script the same selector (the `limit`, `trigger` or `id`
that says what a block applies to), then the same key, then the same distinctive
quoted value under another key (vanilla moving `onpressed = "[OnPause]"` to
`on_action = "[OnPause]"`). What is still unpaired pairs by those same identities
out of order, so a moved block still pairs. Layout, comments and the spelling of
numbers never count as a difference. A statement vanilla has that the copy holds
only as a one-line comment in the same block, word for word (`# cost = 5`), is the
copy's own deletion.

The copy's baseline is the tracked vanilla version it differs from least; a newer
version must fit strictly better to be chosen.

Each difference is attributed with vanilla's tracked history, read at the same place
in each older version. That place is derived, never named: vanilla's versions are
aligned to each other the same way the copy is aligned to the current one, and a
node's counterpart in the version before is whatever that alignment pairs it with,
followed back one version at a time. So two blocks repeated under one key are told
apart by what they hold, and a section vanilla inserted moves nothing: it simply has
no counterpart in the version before it. Where a version has no such block the trail
stops there, since a block vanilla deleted and later put back is two blocks.

  vanilla_changed   your value is one vanilla had at this place; vanilla has changed it
  vanilla_added     vanilla added the statement after the block existed; you lack it
  vanilla_removed   you carry a statement vanilla had here and has deleted
  both_changed      vanilla changed a statement you changed too, or deleted one you
                    changed, after your baseline
  removed_changed   vanilla changed a statement you deleted, after your baseline; the
                    copy has no such statement either way, so this is the copy's own
                    edit and is not reported
  mod_changed, mod_added, mod_removed
                    your own edits: vanilla never touched the statement, or touched it
                    at or before your baseline, so your copy was made seeing it

A copy still holding an old vanilla value, or a statement vanilla deleted, is flagged
whatever its baseline, since that is vanilla's text and not an edit; so is vanilla's
deleted text inside a block only the copy has.

A block of your own wrapped around vanilla's statements moves them to a different
place, so they are not linked to vanilla's history there.

Priority: both_changed is mid, since the copy's statement applies before and after
vanilla's change, so the game behaves as it did. removed_changed is info: a statement
the copy deleted is deleted whatever vanilla later does to it, so vanilla editing it
changes nothing about the copy, exactly as for a statement vanilla never touched. The
other vanilla changes are high when the block holding them also holds an edit of
yours, conflicts and deletions included, since they compete with it, and mid
otherwise. Your own edits are info.

History that starts after your copy was made cannot tell your edits from vanilla's
earlier ones: a difference older than the oldest tracked version reads as yours."""
import difflib
import re
from collections import Counter, defaultdict, namedtuple
from decimal import Decimal, InvalidOperation

from pdx_utilities.script_parser import parse, tokenize

from . import session

Change = namedtuple("Change", "kind priority path mod new since parent after old")
Change.__new__.__defaults__ = (None,)
Change.__doc__ = """One difference between a copy and vanilla's current text.

kind, priority: see the module docstring. path: the keys of the blocks holding it.
mod, new: the Node in the copy and in vanilla's current text (either may be None),
with offsets into its own text. since: index into the history of the version where
vanilla made the change, or None. parent, after: where a statement only vanilla has
belongs in the copy: the copy's enclosing block Node (None at the top) and the
copy's statement it follows (None when it comes first). old: for
both_changed and removed_changed, vanilla's statement at the same place in the
version before `since` that its current text no longer has, a Node with offsets into
that version's text, or None."""

# Script and GUI reach their conclusions differently, so each says so. A script
# block's children are an unordered set: a block repeated under one key is told apart
# by the child that selects what it applies to, never by where it sits. GUI children
# render in sequence, so a nameless one is told apart by its position. Both dialects
# know a block that sets `name = "..."` by that name (Node.label).
SCRIPT, GUI = "script", "gui"
SELECTORS = {SCRIPT: ("limit", "trigger", "id"), GUI: ()}

HIGH, MID, INFO = "high", "mid", "info"
VANILLA_KINDS = ("vanilla_changed", "vanilla_added", "vanilla_removed", "vanilla_renamed",
                 "vanilla_moved")
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


class Nodes(list):
    """The Nodes of one level, with `commented`: the statements its one-line comments
    hold, such as `# cost = 5`."""
    __slots__ = ("commented",)

    def __init__(self, *args):
        super().__init__(*args)
        self.commented = []


def _commented(comment):
    """[Node] for the one statement a comment holds, or [] when the comment is anything
    else, such as prose."""
    text = comment.lstrip("#")
    found = _build(parse(tokenize(text), text, strict=False, positions=True, keyless=True))
    return list(found) if len(found) == 1 and found[0].kind in ("stmt", "block") else []


def nodes(text):
    """The top-level Nodes of `text`. A run parses each text once."""
    text = text or ""
    return session.memo(("diff3.nodes", text), lambda: _build(
        parse(tokenize(text), text, strict=False, positions=True, keyless=True)))


def _build(parsed):
    out, run = Nodes(), []

    def flush():
        if run:
            out.append(Node("items", "@items", tuple(norm_value(n["key"]) for n in run), None,
                            run[0]["_start"], run[-1]["_end"]))
            run.clear()

    for n in parsed:
        kind = n.get("type")
        if kind == "comment":
            out.commented.extend(_commented(n["val"]))
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


def selector(node, dialect):
    """(key, the selecting child's signature) for a block that carries one, or None.
    In script this is what tells two blocks under one key apart: two `trigger_if`
    blocks are the same one across versions when their `limit` is the same."""
    if node.children is None or not SELECTORS[dialect]:
        return None
    for c in node.children:
        if c.key in SELECTORS[dialect]:
            return (node.key, c.sig)
    return None


def _level_keys(dialect):
    """How siblings pair, strongest first: the same statement, the same name, then in
    script the same selector, then the same key, then the same distinctive value. A
    node with nothing at one level gets a value unique to its side, so it cannot pair
    there."""
    levels = [lambda n, i, tag: n.sig,
              lambda n, i, tag: n.label if n.label != n.key else (tag, i)]
    if SELECTORS[dialect]:
        levels.append(lambda n, i, tag: selector(n, dialect) or (tag, i))
    levels.append(lambda n, i, tag: n.key)
    levels.append(lambda n, i, tag: _distinctive(n) or (tag, i))
    return levels


def align(a, b, dialect=SCRIPT):
    """Sorted index pairs between sibling Nodes a and b. In the order of both sides,
    each step pairs only between the pairs the steps before it found, through the
    dialect's levels (_level_keys). Last, what is still unpaired pairs out of order
    by those same identities, so a block that moved still pairs with its own."""
    pairs = []
    levels = _level_keys(dialect)

    def keys(side, idx, level, tag):
        return [levels[level](side[i], i, tag) for i in idx]

    def step(ia, ib, level):
        if not ia or not ib or level >= len(levels):
            return
        matcher = difflib.SequenceMatcher(None, keys(a, ia, level, "a"),
                                          keys(b, ib, level, "b"), autojunk=False)
        pa = pb = 0
        for x, y, size in matcher.get_matching_blocks():
            step(ia[pa:x], ib[pb:y], level + 1)
            pairs.extend(zip(ia[x:x + size], ib[y:y + size]))
            pa, pb = x + size, y + size

    step(list(range(len(a))), list(range(len(b))), 0)
    loose = [lambda n: n.label if n.label != n.key else None, lambda n: n.key]
    if SELECTORS[dialect]:
        loose.insert(1, lambda n: selector(n, dialect))
    for ident in loose:
        taken_a, taken_b = {i for i, _j in pairs}, {j for _i, j in pairs}
        loose_b = defaultdict(list)
        for j, n in enumerate(b):
            if j not in taken_b and ident(n) is not None:
                loose_b[ident(n)].append(j)
        for i, n in enumerate(a):
            if i not in taken_a and ident(n) is not None and loose_b.get(ident(n)):
                pairs.append((i, loose_b[ident(n)].pop(0)))
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


def distance(mine, theirs, dialect=SCRIPT):
    """How many statements differ between two lists of sibling Nodes: each one only
    one side has counts its statements, and each lined-up pair that reads differently
    counts one (a pair of blocks, the statements differing inside)."""
    pairs = align(mine, theirs, dialect)
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
            cost += (m.value != v.value) + distance(m.children, v.children, dialect)
        else:
            cost += 1
    return cost


def baseline(mod_text, versions, unwrap=False, dialect=SCRIPT):
    """Index of the tracked version the copy differs from least, or None when
    vanilla has no version of the text. Among equals the oldest wins: a newer
    version is the baseline only when the copy fits it strictly better. A run
    computes each copy's baseline once."""
    return session.memo(("diff3.baseline", mod_text, tuple(versions), unwrap, dialect),
                        lambda: _baseline(mod_text, versions, unwrap, dialect))


def _baseline(mod_text, versions, unwrap, dialect):
    view = (lambda t: body(nodes(t))) if unwrap else nodes
    mine, best, costs = view(mod_text), None, {}
    for k, text in enumerate(versions):
        if text is None:
            continue
        if text not in costs:
            costs[text] = distance(mine, view(text), dialect)
        if best is None or costs[text] < best[0]:
            best = (costs[text], k)
    return best and best[1]


class _History:
    """Vanilla's versions of one text, oldest first, the last current, each aligned to
    the one before it.

    A place is never named. The sibling list at the same place in an older version is
    whatever the alignments pair the current one with, followed back a version at a
    time (`inside`), so blocks repeated under one key are told apart by what they hold
    rather than by a path that cannot tell them apart at all. Where a version has no
    such block the level is None and the trail stops: a block vanilla deleted and
    later put back is two blocks, not one with a gap in the middle.

    `floor` is where the text's latest unbroken run of versions starts; `base` is the
    copy's baseline."""

    def __init__(self, tops, base, dialect=SCRIPT):
        self.tops, self.base, self.dialect = list(tops), base, dialect
        self.cur = len(self.tops) - 1
        self.floor = max((k + 1 for k, t in enumerate(self.tops) if t is None), default=0)
        self._pairs = {}

    def top(self):
        """The level at the top of the text: each version's own Nodes."""
        return tuple(self.tops)

    def _pair(self, a, b):
        """{index in a: index in b} between two adjacent versions' siblings, once each."""
        key = (id(a), id(b))
        got = self._pairs.get(key)
        if got is None:
            got = self._pairs[key] = dict(align(a, b, self.dialect))
        return got

    def inside(self, level, j):
        """The level inside the current version's block at index j of `level`: the same
        block in each older version, reached by following each version's alignment with
        the one before it."""
        out = [None] * len(level)
        idx = j
        for k in range(self.cur, self.floor - 1, -1):
            ns = level[k]
            if ns is None or idx is None or ns[idx].children is None:
                idx = None
                continue
            out[k] = ns[idx].children
            prev = level[k - 1] if k > self.floor else None
            idx = self._pair(ns, prev).get(idx) if prev is not None else None
        return out

    def matches(self, level, k, same):
        """How many nodes at this place in version k `same` accepts."""
        ns = level[k] if 0 <= k < len(level) else None
        return sum(1 for n in (ns or ()) if same(n))

    def introduced(self, level, j, same):
        """The version where vanilla put the node at index j of `level` here, inside a
        block that already existed, or None when it has been here since the block
        appeared. The node is followed back by the alignments; the first version whose
        counterpart `same` rejects, or that has none, is where vanilla put it."""
        idx, k = j, self.cur
        while k > self.floor:
            ns, prev = level[k], level[k - 1]
            if ns is None or prev is None:
                return None
            nxt = self._pair(ns, prev).get(idx)
            if nxt is None or not same(prev[nxt]):
                return k
            idx, k = nxt, k - 1
        return None

    def removed(self, level, same):
        """The version after the newest one that held more nodes `same` accepts here
        than current vanilla holds, or None when vanilla never held more."""
        now = self.matches(level, self.cur, same)
        for k in range(self.cur - 1, self.floor - 1, -1):
            if level[k] is None:
                return None
            if self.matches(level, k, same) > now:
                return k + 1
        return None

    def moved_out(self, level, sig):
        """The version after the newest one that held `sig` at this level while current
        vanilla holds it here no longer, or None. A block of your own wrapped around
        vanilla's statements moves them one level in, so they are looked for where they
        used to sit: beside the wrapper, not anywhere beneath it. Deeper than that the
        copy's block has no counterpart of vanilla's at all, and nothing in it can be
        attributed to vanilla's history."""
        if self.matches(level, self.cur, lambda n: n.sig == sig):
            return None
        for k in range(self.cur - 1, self.floor - 1, -1):
            if level[k] is None:
                return None
            if self.matches(level, k, lambda n: n.sig == sig):
                return k + 1
        return None

    def before(self, k, level, key):
        """Vanilla's statement of `key` in the version before `k` that its current text
        no longer has, or None."""
        if k is None or k < 1 or level[k - 1] is None:
            return None
        now = {n.sig for n in (level[self.cur] or ()) if n.key == key}
        return next((n for n in level[k - 1] if n.key == key and n.sig not in now), None)

    def replaced(self, k, level, key):
        """True when vanilla held as many statements of `key` here before the change at
        `k` as it holds now, so what it put here took one's place rather than joining
        them."""
        if k is None or k < 1 or level[k - 1] is None:
            return False
        return (self.matches(level, k - 1, lambda n: n.key == key)
                >= self.matches(level, self.cur, lambda n: n.key == key))

    def held_at_baseline(self, level, same, since):
        """True when the copy's baseline already held as many nodes `same` accepts here as
        current vanilla holds, and vanilla's change came later. Vanilla can move a
        statement out of a block and back again, which makes it look newer than the copy;
        whoever wrote the copy saw it at the baseline, so the copy lacking it is their own
        deletion and not a change to take. Vanilla holding more of them than the baseline
        did is still an addition, and a change at the baseline itself is not later."""
        if since is None or self.base is None or since <= self.base:
            return False
        if not 0 <= self.base < len(level):
            return False
        return self.matches(level, self.base, same) >= self.matches(level, self.cur, same)

    def seen_before_copy(self, since):
        """True when vanilla's change came at or before the copy's baseline."""
        return since is not None and self.base is not None and since <= self.base


def compare(mod_text, versions, unwrap=False, dialect=SCRIPT):
    """Changes between a copy and vanilla's current text, which is versions[-1].
    versions holds vanilla's text of the same block or file at each tracked version,
    oldest first, None where vanilla had none. unwrap compares the contents of each
    text's first block (a REPLACE against vanilla's block). Returns [Change] in the
    order they occur in vanilla's text, a block's own changes before its contents'."""
    if not versions or versions[-1] is None:
        raise ValueError("vanilla's current text is required")
    view = (lambda t: body(nodes(t))) if unwrap else nodes
    tops = [None if t is None else view(t) for t in versions]
    hist = _History(tops, baseline(mod_text, versions, unwrap, dialect), dialect)
    out = []
    _walk(view(mod_text), tops[-1], (), hist.top(), None, hist, out, dialect)
    return out


def _walk(mine, theirs, path, level, parent, hist, out, dialect):
    pairs = align(mine, theirs, dialect)
    of_mine = {i: j for i, j in pairs}
    of_theirs = {j: i for i, j in pairs}
    settled_mine, settled_theirs = _settle(mine, theirs, pairs)
    local, inner = [], []
    commented = Counter(n.sig for n in getattr(mine, "commented", ()))

    def add(kind, m, v, since, after=None, key=None):
        if kind in CONFLICT_KINDS and hist.seen_before_copy(since):
            kind, since = ("mod_removed" if m is None else
                           "mod_changed" if v is not None else "mod_added"), None
        old = hist.before(since, level, key) if kind in CONFLICT_KINDS and key else None
        local.append(Change(kind, None, path, m, v, since, parent, after, old))

    after = None
    for j, v in enumerate(theirs):
        i = of_theirs.get(j)
        here = after
        if i is not None:
            after = mine[i]
        if j in settled_theirs or (i is not None and mine[i].sig == v.sig):
            continue
        if i is None or i in settled_mine:
            if commented[v.sig]:                   # you commented vanilla's statement out
                commented[v.sig] -= 1
                add("mod_removed", None, v, None, here)
                continue
            same = lambda n, v=v: n.sig == v.sig                       # noqa: E731
            k = hist.introduced(level, j, same)
            # Vanilla put the statement here at or before the version the copy matches,
            # so whoever wrote the copy saw it and left it out. That is their deletion.
            if k is None or hist.seen_before_copy(k) or hist.held_at_baseline(level, same, k):
                add("mod_removed", None, v, None, here)
            elif hist.replaced(k, level, v.key):
                add("removed_changed", None, v, k, here, v.key)
            else:
                add("vanilla_added", None, v, k, here)
            continue
        m = mine[i]
        if m.kind == v.kind == "block" and m.key == v.key:
            if m.value != v.value:
                kind, since = _classify_pair(hist, level, j, m, v, head=True)
                add(kind, m, v, since)
            inner.append((m, v, j))
        else:
            kind, since = _classify_pair(hist, level, j, m, v)
            add(kind, m, v, since, key=v.key)

    for i, m in enumerate(mine):
        j = of_mine.get(i)
        if i in settled_mine or (j is not None and j not in settled_theirs):
            continue
        k = hist.removed(level, lambda n, m=m: n.sig == m.sig)
        if k is not None:
            add("vanilla_removed", m, None, k)
        elif (k := hist.removed(level, lambda n, m=m: n.key == m.key)) is not None:
            add("both_changed", m, None, k, key=m.key)
        else:
            add("mod_added", m, None, None)
            if m.children is not None:
                _stale_inside(m, path + (m.key,), level, hist, out)

    # Whether the block holds an edit of yours is read before the merge, since a rename
    # can take a conflict of yours with it and the changes beside it still compete.
    yours = any(c.kind in MOD_KINDS + CONFLICT_KINDS for c in local)
    _merge_renames(local, level, hist)
    _merge_moves(local)
    for c in local:
        if c.kind in MOD_KINDS or c.kind == "removed_changed":
            priority = INFO
        elif c.kind in CONFLICT_KINDS or not yours:
            priority = MID
        else:
            priority = HIGH
        out.append(c._replace(priority=priority))
    for m, v, j in inner:
        _walk(m.children, v.children, path + (v.key,), hist.inside(level, j), m, hist, out,
              dialect)


def _says(node):
    """What a statement says, without the key it says it under, so vanilla's text under
    two names compares equal."""
    return (node.kind, node.value, None if node.children is None else tuple(c.sig for c in node.children))


def _merge_renames(local, level, hist):
    """Vanilla moving a statement to another key reads as one addition and one removal
    of the same text, which is two findings for one rename. Vanilla's own history tells
    them apart: the statement it held under the old key just before the change reads the
    same as the one it holds under the new key now. Replace the pair with one change.

    Only a block counts. Two bare statements that carry the same short value, such as
    `old = yes` and `new = yes`, read alike too often for one to be the other under a
    new name, and a merge that is wrong hides a change."""
    added = [c for c in local if c.kind == "vanilla_added"]
    # Vanilla deleting a statement of yours reads as vanilla_removed when your copy of
    # it is vanilla's, and as both_changed when you had edited it. Either way the key
    # is gone from vanilla, so either can be the old half of a rename.
    dropped = [c for c in local if c.new is None and c.mod is not None
               and c.kind in ("vanilla_removed",) + CONFLICT_KINDS]
    for gone in dropped:
        was = gone.old or hist.before(gone.since, level, gone.mod.key)
        if was is None or was.children is None:
            continue
        hit = next((c for c in added if c.since == gone.since and _says(c.new) == _says(was)), None)
        if hit is None:
            continue
        added.remove(hit)
        local[local.index(gone)] = gone._replace(kind="vanilla_renamed", new=hit.new, old=None)
        local.remove(hit)


def _holds(node, value):
    """True when a statement at or under `node` carries `value` as its distinctive one."""
    stack = [node]
    while stack:
        n = stack.pop()
        if _distinctive(n) == value:
            return True
        if n.children:
            stack.extend(n.children)
    return False


def _merge_moves(local):
    """Vanilla putting a statement inside a block it now holds beside it reads as a
    deletion, because the statement is gone from this level. Your copy still sets it
    where it was, which is worth saying, but calling it deleted is wrong: vanilla still
    carries it. Report the pair as one move, naming the block it went into.

    Only a distinctive quoted value counts (see `_distinctive`), so a plain `a = 1`
    turning up in an unrelated new block is never read as the same statement."""
    for gone in [c for c in local if c.kind == "vanilla_removed" and c.mod is not None]:
        value = _distinctive(gone.mod)
        if value is None:
            continue
        host = next((c for c in local if c.mod is None and c.new is not None
                     and c.new.children is not None and _holds(c.new, value)), None)
        if host is None:
            continue
        local[local.index(gone)] = gone._replace(kind="vanilla_moved", new=host.new)
        if host.kind == "vanilla_added":
            local.remove(host)          # the move already names the block it went into


def _stale_inside(block, path, level, hist, out):
    """Flag what a block only your copy has still carries of vanilla's deleted text:
    the statements vanilla held beside the block and has since removed, which your copy
    wrapped. They compete with the block around them, so they are high."""
    for n in block.children:
        k = hist.moved_out(level, n.sig)
        if k is not None:
            out.append(Change("vanilla_removed", HIGH, path, n, None, k, block, None))


def _classify_pair(hist, level, j, m, v, head=False):
    """(kind, since) for a statement of yours lined up with a different one of
    vanilla's. `head` compares the two blocks' heads rather than their whole text."""
    if head:
        same_m = lambda n: n.key == m.key and n.value == m.value        # noqa: E731
        same_v = lambda n: n.key == v.key and n.value == v.value        # noqa: E731
    else:
        same_m = lambda n: n.sig == m.sig                               # noqa: E731
        same_v = lambda n: n.sig == v.sig                               # noqa: E731
    k = hist.removed(level, same_m)
    if k is not None:
        return "vanilla_changed", k
    k = hist.introduced(level, j, same_v)
    if k is not None:
        return "both_changed", k
    return "mod_changed", None
