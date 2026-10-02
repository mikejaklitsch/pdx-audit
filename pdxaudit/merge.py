"""Node-level three-way merge of vanilla patch changes into the mod's copies.

For each copy the merge takes three texts:

  base     vanilla at the copy's baseline, not later than --old
  ours     the mod's copy
  theirs   vanilla at --new

It parses all three with the shared parser (diff3.nodes) and aligns base with ours
and base with theirs (diff3.align), level by level. Each base node is decided on its
own, so two adjacent edits never fuse into one conflict:

  ours        theirs      result
  same        same        ours
  changed     same        ours
  same        changed     theirs, unless a rule or an entry says keep_mod
  changed     changed     ours when both agree; blocks with one head recurse;
                          otherwise the disposition, or an open decision

A node that only theirs has goes after the ours counterpart of its nearest earlier
sibling; a node that both added is kept once. The merge splices into the ours text at
node offsets, so the mod's layout, order and comments stay. A one-line block that a
merged node gives a line break gets one child per line, so the text stays readable.

A rule or an entry without a disposition (a grouping), a banned rule and a stale
entry never apply: each node goes to the user as an open decision.

The removed-line check compares the merged text with ours line by line. Every line
that the merge removes must lie inside an operation that a vanilla change explains.
Any other removal fails the file, and `--apply` does not write it."""
import difflib
import re
from dataclasses import dataclass, field

from . import diff3, intent

TAKE, KEEP, OPEN = "take", "keep", "open"


@dataclass
class Op:
    start: int
    end: int
    text: str
    why: str                     # what explains it: "vanilla changed", "rule:x", ...
    removes: bool = True         # True when the op removes ours text


@dataclass
class Decision:
    path: list
    kind: str                    # vanilla_changed, vanilla_added, vanilla_removed, both_changed, ...
    action: str                  # take, keep, open
    by: str = None               # rule or entry that decided it
    reason: str = ""
    base: str = None
    ours: str = None
    theirs: str = None
    line: int = None
    commit: str = None

    def to_json(self):
        return {k: v for k, v in self.__dict__.items() if v not in (None, "")}


@dataclass
class Result:
    text: str
    ops: list = field(default_factory=list)
    decisions: list = field(default_factory=list)
    unexplained: list = field(default_factory=list)
    stale: list = field(default_factory=list)

    @property
    def open(self):
        return [d for d in self.decisions if d.action == OPEN]

    @property
    def check_passed(self):
        return not self.unexplained


def _line_start(text, pos):
    return text.rfind("\n", 0, pos) + 1


def _line_end(text, pos):
    e = text.find("\n", pos)
    return len(text) if e < 0 else e


def _indent(text, pos):
    s = _line_start(text, pos)
    return text[s:len(text[s:]) - len(text[s:].lstrip(" \t")) + s]


def _code_after(text, pos):
    """The code between `pos` and the end of its line, without a comment."""
    rest = text[pos:_line_end(text, pos)]
    in_str = False
    for i, c in enumerate(rest):
        if c == '"':
            in_str = not in_str
        elif c == "#" and not in_str:
            return rest[:i]
    return rest


def _lead(text, node):
    """The start of the comment lines directly above `node`, with no blank line
    between: they describe the node and move with it. The node's own start when there
    are none, or when the node shares its line with other code."""
    s = _line_start(text, node.start)
    if text[s:node.start].strip():
        return node.start
    start = s
    while start > 0:
        prev = _line_start(text, start - 1)
        line = text[prev:start - 1].strip()
        if not line.startswith("#"):
            break
        start = prev
    return start if start < s else node.start


def _trail(text, node):
    """(offset, comment) for the comment after `node` on its last line, when nothing
    else of code follows it there; else None."""
    rest = text[node.end:_line_end(text, node.end)]
    if _code_after(text, node.end).strip():
        return None
    cut = rest.find("#")
    return None if cut < 0 else (node.end + cut, rest[cut:].rstrip())


def _lead_lines(text, node):
    """The comment lines directly above `node` (see _lead), stripped."""
    first = _lead(text, node)
    if first >= node.start:
        return []
    return [ln.strip() for ln in text[first:_line_start(text, node.start)].rstrip("\n").split("\n")]


def _own_line(text, node):
    """True when no other code shares the node's first line before it."""
    return not text[_line_start(text, node.start):node.start].strip()


def _reindent(src, node, indent, comments=False):
    """The text of `node` in `src`, its later lines moved from the node's own indent
    to `indent`, with no space at the end of a line. With `comments`, the comment
    lines above the node and the comment after it on its last line come with it."""
    head = ""
    if comments and _lead(src, node) < node.start:
        lines = _lead_lines(src, node)
        head = "\n".join(lines[:1] + [indent + ln for ln in lines[1:]]) + "\n" + indent
    body = src[node.start:node.end]
    old = _indent(src, node.start)
    lines = body.split("\n")
    out = [lines[0].rstrip()]
    for line in lines[1:]:
        out.append((indent + line[len(old):] if line.startswith(old) else line).rstrip())
    tail = _trail(src, node) if comments else None
    return head + "\n".join(out) + (" " + tail[1] if tail else "")


def _notes(text, node):
    """The comments inside `node`, stripped, in order."""
    out = []
    for line in text[node.start:node.end].split("\n"):
        c = intent._comment_of(line)
        if c:
            out.append(c)
    return out


def _comment_lines(text, a, z, skip=()):
    """The comment lines of text[a:z], stripped, outside the (start, end) spans in
    `skip`."""
    out, pos = [], a
    for line in text[a:z].split("\n"):
        if line.strip().startswith("#") and not any(s <= pos < e for s, e in skip):
            out.append(line.strip())
        pos += len(line) + 1
    return out


def _delete_span(text, node):
    """(start, end) to delete `node`: its whole line when nothing else of code is on
    it, else the node with the space before it."""
    s, e = _line_start(text, node.start), _line_end(text, node.end)
    before = text[s:node.start]
    if not before.strip() and not _code_after(text, node.end).strip():
        start, end = _line_start(text, _lead(text, node)), min(e + 1, len(text))
        # An empty line above and an empty line or the end of the text below: delete
        # one more empty line, so that no two empty lines stay. A blanked line of
        # another definition holds spaces, so it never counts as empty.
        if start > 0 and text[_line_start(text, start - 1):start - 1] == "":
            if end < len(text) and text[end] == "\n":
                end += 1
            elif end == len(text):
                start = _line_start(text, start - 1)
        return start, end
    start = node.start
    while start > s and text[start - 1] in " \t":
        start -= 1
    return start, node.end


# A moved block must hold at least this many statements, so that a short block such
# as `size = { 90 28 }` never counts as moved vanilla text.
MOVE_MIN_SIZE = 3


def _walk(nodes):
    for n in nodes:
        yield n
        if n.children:
            yield from _walk(n.children)


def _leaves(node, out=None):
    """The signatures of the statements under a node."""
    out = set() if out is None else out
    for c in node.children or ():
        if c.children is None:
            out.add(c.sig)
        else:
            _leaves(c, out)
    return out


def _similarity(x, y):
    a, b = _leaves(x), _leaves(y)
    return len(a & b) / len(a | b) if a or b else 0.0


def _score(m, n, dialect, hint=None):
    """How well two siblings pair, or 0 when they cannot: a named block or a block
    with a selector pairs only with its own name or selector; other blocks of one key
    pair better the more statements they share."""
    if m.kind != n.kind or m.key != n.key:
        return 0
    if m.label != m.key or n.label != n.key:
        return 4 if m.label == n.label else 0
    if m.children is None:
        return 1.5 if m.value == n.value else 1
    sm, sn = diff3.selector(m, dialect), diff3.selector(n, dialect)
    if sm is not None and sm == sn:
        return 3
    # `hint`: vanilla's new version of m. Ours can hold vanilla's new text already, so
    # the closeness to it counts as much as the closeness to the base.
    extra = _similarity(hint, n) if hint is not None and hint.children is not None else 0
    return 1 + _similarity(m, n) + extra


def _gap(a, b, ia, ib, dialect, hints=None):
    """Order-preserving pairs between two runs of unmatched siblings, with the most
    total score (a weighted longest common subsequence)."""
    ia, ib = list(ia), list(ib)
    if not ia or not ib:
        return []
    if len(ia) * len(ib) > 250000:                 # a huge run: diff3's own pairing
        pairs = diff3.align([a[i] for i in ia], [b[j] for j in ib], dialect)
        return [(ia[x], ib[y]) for x, y in pairs]
    n, m = len(ia), len(ib)
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    for x in range(n - 1, -1, -1):
        for y in range(m - 1, -1, -1):
            s = _score(a[ia[x]], b[ib[y]], dialect, (hints or {}).get(ia[x]))
            take = best[x + 1][y + 1] + s if s else 0.0
            best[x][y] = max(best[x + 1][y], best[x][y + 1], take)
    pairs, x, y = [], 0, 0
    while x < n and y < m:
        s = _score(a[ia[x]], b[ib[y]], dialect, (hints or {}).get(ia[x]))
        if s and best[x][y] == best[x + 1][y + 1] + s:
            pairs.append((ia[x], ib[y]))
            x, y = x + 1, y + 1
        elif best[x][y] == best[x + 1][y]:
            x += 1
        else:
            y += 1
    return pairs


def merge_align(a, b, dialect, hints=None):
    """Pairs between sibling nodes for the merge. Identical nodes pair first, in
    order. Between them, the runs pair by _score in order, so a block that vanilla
    inserted before a changed sibling of the same key does not take its partner.
    Last, a named block or a block with a selector that moved pairs out of order.
    `hints`: {index in a: vanilla's new version of that node}, for the base-to-ours
    pairing."""
    import difflib as _dl
    pairs, pa, pb = [], 0, 0
    sm = _dl.SequenceMatcher(None, [n.sig for n in a], [n.sig for n in b], autojunk=False)
    for x, y, size in sm.get_matching_blocks():
        pairs += _gap(a, b, range(pa, x), range(pb, y), dialect, hints)
        pairs += [(x + k, y + k) for k in range(size)]
        pa, pb = x + size, y + size
    taken_a, taken_b = {i for i, _j in pairs}, {j for _i, j in pairs}
    for ident in (lambda n: n.sig,                     # the same node, moved
                  lambda n: n.label if n.label != n.key else None,
                  lambda n: diff3.selector(n, dialect)):
        free_b = {}
        for j, n in enumerate(b):
            if j not in taken_b and ident(n) is not None:
                free_b.setdefault(ident(n), []).append(j)
        for i, n in enumerate(a):
            if i not in taken_a and ident(n) is not None and free_b.get(ident(n)):
                j = free_b[ident(n)].pop(0)
                pairs.append((i, j))
                taken_a.add(i)
                taken_b.add(j)
    # A key that only one unpaired node holds on each side: the same node, moved and
    # changed.
    free_a = [i for i in range(len(a)) if i not in taken_a]
    free_b = [j for j in range(len(b)) if j not in taken_b]
    for i in free_a:
        same_a = [k for k in free_a if a[k].key == a[i].key]
        same_b = [j for j in free_b if b[j].key == a[i].key and j not in taken_b]
        if len(same_a) == 1 and len(same_b) == 1 and _score(a[i], b[same_b[0]], dialect):
            pairs.append((i, same_b[0]))
            taken_b.add(same_b[0])
    return sorted(pairs)


class _Merge:
    def __init__(self, base, ours, theirs, dialect, unwrap, decide, history=()):
        self.b_text, self.o_text, self.t_text = base, ours, theirs
        self.dialect, self.unwrap, self.decide = dialect, unwrap, decide
        self.ops, self.decisions, self.overlaps = [], [], []
        self.history = [h for h in history if h is not None]
        self.ours_only = []              # nodes only ours has, for moved()
        self.deferred = []               # decisions that wait for moved()
        self.ours_sigs = {n.sig for n in _walk(self.view(ours))}

    def view(self, text):
        top = diff3.nodes(text)
        return diff3.body(top) if self.unwrap else top

    def run(self):
        b, o, t = self.view(self.b_text), self.view(self.o_text), self.view(self.t_text)
        first = lambda text: next((n for n in diff3.nodes(text) if n.kind == "block"), None)   # noqa: E731
        outer = first(self.o_text) if self.unwrap else None
        parents = (first(self.b_text), first(self.t_text)) if self.unwrap else (None, None)
        self.level(b, o, t, [], outer, parents)
        self.moved()
        return self.apply()

    # --- one level --------------------------------------------------------------
    def level(self, B, O, T, path, o_parent, parents=(None, None)):
        bt = dict(merge_align(B, T, self.dialect))
        bo = dict(merge_align(B, O, self.dialect, {i: T[j] for i, j in bt.items()}))
        tb = {j: i for i, j in bt.items()}
        ob = {j: i for i, j in bo.items()}
        for i, b in enumerate(B):
            o = O[bo[i]] if i in bo else None
            t = T[bt[i]] if i in bt else None
            seg_node, seg_level = (t, T) if t is not None else ((o, O) if o is not None else (b, B))
            here = path + [intent.segment(seg_node, seg_level, self.dialect)]
            o_same = o is not None and o.sig == b.sig
            t_same = t is not None and t.sig == b.sig
            if t_same and o is not None:
                # Vanilla changed no statement of the node; it may have changed a
                # comment on it or inside it.
                self.carry_comments(b, o, t)
                if o_same and b.children and o.children is not None and t.children is not None \
                        and _notes(self.b_text, b) != _notes(self.t_text, t):
                    self.level(b.children, o.children, t.children, here, o, (b, t))
                continue
            if t_same or (o is None and t is None):
                continue
            if o is not None and t is not None and o.sig == t.sig:
                continue
            if o_same and t is not None and o.children is not None and t.children is not None \
                    and o.key == t.key:
                self.recurse(here, b, o, t)          # take vanilla's change inside, keep ours' layout
            elif o_same:
                kind = "vanilla_changed" if t is not None else "vanilla_removed"
                gap = self.gap_comments(i, B, T, bt, parents) if t is None else None
                self.vanilla_change(kind, here, b, o, t, gap)
            elif o is None:
                place = (self.anchor(T, bt[i], tb, bo, O), o_parent)
                if self.movable(b):
                    self.deferred.append(("removed_changed", here, b, t, place))
                else:
                    self.decide_conflict("removed_changed", here, b, None, t, place)
            elif t is None:
                self.decide_conflict("both_changed", here, b, o, None, None)
            elif (o.children is not None and t.children is not None and o.key == t.key == b.key):
                self.recurse(here, b, o, t)
            else:
                self.decide_conflict("both_changed", here, b, o, t, None)
        # A node that only ours has, but that a vanilla version after the base held as
        # it is, is vanilla's text: the mod took it from that version. Vanilla's later
        # change to it is a vanilla change, not the mod's.
        used_t = self.later_vanilla(path, B, O, T, ob, tb)
        # Nodes that only theirs has. A node that ours holds as it is stays once. A
        # node that ours added in another form is a both_added decision, so the
        # merge never writes one key two times.
        twins = self.twins(O, T, [j for j in range(len(O)) if j not in ob and j not in self._taken_o],
                           [j for j in range(len(T)) if j not in tb and j not in used_t])
        for j, t in enumerate(T):
            if j in tb or j in used_t:
                continue
            if any(x.sig == t.sig for x in O):
                continue
            here = path + [intent.segment(t, T, self.dialect)]
            if j in twins:
                self.decide_conflict("both_added", here, None, O[twins[j]], t, None)
                continue
            anchor = self.anchor(T, j, tb, bo, O, twins)
            if self.movable(t) or any(self.movable(x) for x in _walk(t.children or [])):
                self.deferred.append(("vanilla_added", here, None, t, (anchor, o_parent)))
            else:
                self.vanilla_added(here, t, (anchor, o_parent))
        twin_o = set(twins.values())
        t_sigs = {n.sig for n in T}
        self.ours_only += [O[j] for j in range(len(O)) if j not in ob and j not in self._taken_o
                           and j not in twin_o and O[j].sig not in t_sigs]

    def movable(self, node):
        """True when a block that the base or theirs holds at this place may sit as it
        is in another place of ours: then moved() decides it after the whole pass."""
        return node.children is not None and node.size >= MOVE_MIN_SIZE and node.sig in self.ours_sigs

    def vanilla_added(self, here, t, place):
        action, by = self.decide(here, "vanilla_added", None, t, self.t_text, None)
        self.decisions.append(Decision(here, "vanilla_added", action, by, theirs=intent.canon(t)))
        if action == TAKE:
            self.insert(place[0], t, place[1], by or "vanilla added")

    def moved(self):
        """Decide each vanilla block that the mod moved into a block of its own. A block
        of the mod's own text (one that ours added) can hold a copy of a vanilla block
        from the base or a later version, moved there as it is. When vanilla no longer
        holds that text anywhere, vanilla changed or removed the original: the copy
        follows (vanilla_changed takes vanilla's new block, vanilla_removed deletes
        the copy), unless a rule or an entry decides otherwise. Vanilla's new block is
        its block of the same key at the original's place that shares at least half of
        the copy's statements; with none, vanilla removed it."""
        inside = {x.sig for root in self.ours_only for x in _walk([root])}
        for kind, here, b, t, place in self.deferred:
            node = b if b is not None else t
            if node.sig in inside:
                continue                 # the mod moved it: decided below
            if kind == "removed_changed":
                self.decide_conflict(kind, here, b, None, t, place)
            elif any(x.sig in inside for x in _walk(t.children or []) if x.children is not None
                     and x.size >= MOVE_MIN_SIZE):
                # Vanilla's new block holds a block that the mod moved into a block of
                # its own: taking it would write that block two times.
                action, by = self.decide(here, "vanilla_added", None, t, self.t_text, "conflict")
                self.decisions.append(Decision(
                    here, "vanilla_added", OPEN if action == TAKE and by is None else action, by,
                    theirs=intent.canon(t),
                    reason="vanilla's new block holds a block that the mod moved into a block of its own"))
                if action == TAKE and by is not None:
                    self.insert(place[0], t, place[1], by)
            else:
                self.vanilla_added(here, t, place)
        if not self.ours_only:
            return
        origins = {}
        for text in [self.b_text] + self.history:
            top = self.view(text)
            for n in _walk(top):
                if n.children is not None and n.size >= MOVE_MIN_SIZE:
                    origins.setdefault(n.sig, (n, top))
        if not origins:
            return
        theirs_top = self.view(self.t_text)
        theirs_sigs = {n.sig for n in _walk(theirs_top)}
        ours_top = self.view(self.o_text)
        ours_sigs = {n.sig for n in _walk(ours_top)}
        stack = list(self.ours_only)
        while stack:
            n = stack.pop()
            if n.children is None:
                continue
            if n.size < MOVE_MIN_SIZE or n.sig not in origins or n.sig in theirs_sigs:
                stack.extend(n.children)
                continue
            origin, otop = origins[n.sig]
            parent_path = (intent.path_of(otop, origin, self.dialect) or [])[:-1]
            level, _loose = intent.resolve(theirs_top, parent_path, self.dialect)
            mates = [c for c in (level.children or []) if c.key == n.key and c.children is not None
                     and c.sig not in ours_sigs] if level is not None else []
            best = max(mates, key=lambda c: _similarity(n, c), default=None)
            t = best if best is not None and _similarity(n, best) >= 0.5 else None
            here = intent.path_of(ours_top, n, self.dialect) or [{"key": n.key}]
            kind = "vanilla_changed" if t is not None else "vanilla_removed"
            action, by = self.decide(here, kind, n, t, self.t_text, None)
            self.decisions.append(Decision(here, kind, action, by, base=intent.canon(origin), ours=intent.canon(n),
                                           theirs=intent.canon(t),
                                           reason="the mod moved this vanilla block into a block of its own",
                                           line=self.o_text.count("\n", 0, n.start) + 1))
            if action != TAKE:
                continue
            if t is None:
                self.remove(n, by or "vanilla removed the block the mod moved")
            else:
                self.replace(None, n, t, by or "vanilla changed the block the mod moved")

    def history_level(self, path):
        """The nodes at `path` in each vanilla version between the base and theirs."""
        out = []
        for text in self.history:
            node, _loose = intent.resolve(self.view(text), path, self.dialect)
            if node is not None and node.children is not None:
                out.extend(node.children)
        return out

    def later_vanilla(self, path, B, O, T, ob, tb):
        """Decide each node that only ours has and that a vanilla version after the base
        holds as it is. Vanilla's node of the same name, selector or single key in
        theirs is vanilla's change to it (vanilla_changed); with none, vanilla removed
        it (vanilla_removed). Returns the theirs indexes it used."""
        self._taken_o, used = set(), set()
        if not self.history:
            return used
        free_o = [j for j in range(len(O)) if j not in ob]
        t_sigs = {n.sig for n in T}
        if not free_o:
            return used
        earlier = {}
        for n in self.history_level(path):
            earlier.setdefault(n.sig, n)
        b_sigs = {n.sig for n in B}
        cand = [j for j in free_o if O[j].sig in earlier and O[j].sig not in t_sigs and O[j].sig not in b_sigs]
        if not cand:
            return used
        free_t = [j for j in range(len(T)) if j not in tb and T[j].sig not in {n.sig for n in O}]
        pairs = self.twins(O, T, cand, free_t)
        mate = {o: t for t, o in pairs.items()}
        for j in cand:
            o = O[j]
            t = T[mate[j]] if j in mate else None
            here = path + [intent.segment(t if t is not None else o, T if t is not None else O, self.dialect)]
            kind = "vanilla_changed" if t is not None else "vanilla_removed"
            action, by = self.decide(here, kind, o, t, self.t_text, None)
            self.decisions.append(Decision(here, kind, action, by, base=intent.canon(earlier[o.sig]),
                                           ours=intent.canon(o), theirs=intent.canon(t),
                                           reason="the mod took this node from a later vanilla version",
                                           line=self.o_text.count("\n", 0, o.start) + 1))
            self._taken_o.add(j)
            if t is not None:
                used.add(mate[j])
            if action != TAKE:
                continue
            if t is None:
                self.remove(o, by or "vanilla removed")
            else:
                self.replace(None, o, t, by or "vanilla changed")
        return used

    def twins(self, O, T, free_o, free_t):
        """{theirs index: ours index} for a node that both sides added in different
        forms: the same name, the same selector, or a key that one added node holds
        on each side. Nodes that read the same on both sides are not twins."""
        o_sigs, t_sigs = {n.sig for n in O}, {n.sig for n in T}
        cand_o = [k for k in free_o if O[k].sig not in t_sigs]
        cand_t = [j for j in free_t if T[j].sig not in o_sigs]
        plain = lambda n: n.label == n.key and diff3.selector(n, self.dialect) is None   # noqa: E731
        out, used = {}, set()
        for j in cand_t:
            t = T[j]
            same = [k for k in cand_o if k not in used and O[k].kind == t.kind and O[k].key == t.key]
            if t.label != t.key:
                hit = [k for k in same if O[k].label == t.label]
            elif diff3.selector(t, self.dialect) is not None:
                hit = [k for k in same if diff3.selector(O[k], self.dialect) == diff3.selector(t, self.dialect)]
            else:
                rivals = [i for i in cand_t if T[i].kind == t.kind and T[i].key == t.key]
                hit = same if len(same) == 1 and len(rivals) == 1 and plain(O[same[0]]) else []
            if hit:
                out[j] = hit[0]
                used.add(hit[0])
        return out

    def carry_comments(self, b, o, t):
        """Take vanilla's change to the comment lines above a node and to the comment
        after it on its line, when the mod left that comment as the base had it."""
        if not (_own_line(self.o_text, o) and _own_line(self.t_text, t) and _own_line(self.b_text, b)):
            return
        ind = _indent(self.o_text, o.start)
        above = _lead_lines(self.b_text, b)
        if _lead_lines(self.o_text, o) == above and _lead_lines(self.t_text, t) != above:
            start = _line_start(self.o_text, _lead(self.o_text, o))
            self.ops.append(Op(start, _line_start(self.o_text, o.start),
                               "".join(ind + ln + "\n" for ln in _lead_lines(self.t_text, t)),
                               "vanilla changed the comment above the node"))
        ot, bt, tt = _trail(self.o_text, o), _trail(self.b_text, b), _trail(self.t_text, t)
        if _code_after(self.o_text, o.end).strip():
            return
        if (ot and ot[1]) == (bt and bt[1]) and (tt and tt[1]) != (bt and bt[1]):
            self.ops.append(Op(o.end, _line_end(self.o_text, o.end), " " + tt[1] if tt else "",
                               "vanilla changed the comment after the node"))

    def recurse(self, here, b, o, t):
        self.carry_comments(b, o, t)
        if o.value != t.value:
            if o.value == b.value:
                d = self.decide(here, "vanilla_changed", o, t, self.t_text, "head")
                if d[0] == TAKE:
                    self.ops.append(Op(o.start, o.open_end, self.t_text[t.start:t.open_end],
                                       d[1] or "vanilla changed the head"))
                self.decisions.append(Decision(here, "vanilla_changed", d[0], d[1],
                                               ours=self.o_text[o.start:o.open_end],
                                               theirs=self.t_text[t.start:t.open_end]))
            elif t.value != b.value:
                self.decisions.append(Decision(here, "both_changed", OPEN, None,
                                               ours=self.o_text[o.start:o.open_end],
                                               theirs=self.t_text[t.start:t.open_end]))
        self.level(b.children, o.children, t.children, here, o, (b, t))

    def anchor(self, T, j, tb, bo, O, twins=None):
        """('after', ours node) for the nearest earlier theirs sibling with an ours
        counterpart, else ('before', ours node) for the nearest later one, else
        ('end', None). The counterpart goes through the base, or, for a node the base
        lacks, is its twin (see `twins`) or the ours node that reads the same."""
        def mate(k):
            if k in tb:
                return O[bo[tb[k]]] if tb[k] in bo else None
            if twins and k in twins:
                return O[twins[k]]
            return next((o for o in O if o.sig == T[k].sig), None)
        for k in range(j - 1, -1, -1):
            if mate(k) is not None:
                return ("after", mate(k))
        for k in range(j + 1, len(T)):
            if mate(k) is not None:
                return ("before", mate(k))
        return ("end", None)

    # --- decisions --------------------------------------------------------------
    def gap_comments(self, i, B, T, bt, parents):
        """(base, theirs) comment lines in the place of base node B[i]: between the
        nodes that hold the base neighbours of B[i] on each side, or the edge of the
        block. Theirs leaves out the comments of the nodes vanilla put there and the
        comments directly above the next node, since those move with their nodes."""
        prev = next((k for k in range(i - 1, -1, -1) if k in bt), None)
        nxt = next((k for k in range(i + 1, len(B)) if k in bt), None)

        def bounds(text, a_node, z_node, parent):
            if a_node is not None:
                a = _line_end(text, a_node.end) + 1
            else:
                a = _line_end(text, parent.open_end) + 1 if parent is not None else 0
            if z_node is not None:
                z = _line_start(text, z_node.start)
            else:
                z = _line_start(text, parent.end - 1) if parent is not None else len(text)
            return a, max(a, z)
        b_parent, t_parent = parents
        ba, bz = bounds(self.b_text, B[prev] if prev is not None else None,
                        B[nxt] if nxt is not None else None, b_parent)
        ta, tz = bounds(self.t_text, T[bt[prev]] if prev is not None else None,
                        T[bt[nxt]] if nxt is not None else None, t_parent)
        b_skip = [(n.start, n.end) for n in B if ba <= n.start < bz]
        t_skip = [(_line_start(self.t_text, _lead(self.t_text, n)), n.end) for n in T if ta <= n.start < tz]
        if nxt is not None:              # the next node's own comments go with it (carry_comments)
            n = T[bt[nxt]]
            t_skip.append((_line_start(self.t_text, _lead(self.t_text, n)), n.start))
        return _comment_lines(self.b_text, ba, bz, b_skip), _comment_lines(self.t_text, ta, tz, t_skip)

    def near_comments(self, o):
        """The comment lines of ours around `o`, up to the code line above it and the
        code line below it."""
        text, out = self.o_text, []
        pos = _line_start(text, o.start)
        while pos > 0:
            prev = _line_start(text, pos - 1)
            line = text[prev:pos - 1].strip()
            if line and not line.startswith("#"):
                break
            if line:
                out.append(line)
            pos = prev
        pos = _line_end(text, o.end) + 1
        while pos < len(text):
            end = _line_end(text, pos)
            line = text[pos:end].strip()
            if line and not line.startswith("#"):
                break
            if line:
                out.append(line)
            pos = end + 1
        return out

    def vanilla_change(self, kind, here, b, o, t, gap=None):
        action, by = self.decide(here, kind, o, t, self.t_text, None)
        self.decisions.append(Decision(here, kind, action, by, base=intent.canon(b), ours=intent.canon(o),
                                       theirs=intent.canon(t),
                                       line=self.o_text.count("\n", 0, o.start) + 1))
        if action != TAKE:
            return
        if t is None:
            self.remove(o, by or "vanilla removed", gap)
        else:
            self.replace(b, o, t, by or "vanilla changed")

    def remove(self, o, why, gap=None):
        """Delete ours node `o`. A comment that vanilla wrote in the node's place takes
        its place, and so does a comment above it that vanilla kept. A comment
        directly above the next node goes with that node instead (carry_comments):
        1.4 messagetypes.txt turns `sound=yes` into `#sound=yes - TODO in ud010`
        above `message_category`."""
        s, e = _delete_span(self.o_text, o)
        text = ""
        if gap is not None and _own_line(self.o_text, o) and not _code_after(self.o_text, o.end).strip():
            base_c, theirs_c = gap
            lead = _lead_lines(self.o_text, o)
            near = self.near_comments(o)
            stay = list(near)
            for c in lead:
                if c in stay:
                    stay.remove(c)
            keep = [c for c in theirs_c if (c not in base_c or c in lead) and c not in stay]
            if keep:
                ind = _indent(self.o_text, o.start)
                s = _line_start(self.o_text, _lead(self.o_text, o))
                e = min(_line_end(self.o_text, o.end) + 1, len(self.o_text))
                text = "".join(ind + c + "\n" for c in keep)
        self.ops.append(Op(s, e, text, why))

    def replace(self, b, o, t, why):
        """Put vanilla's node `t` in the place of ours node `o`. A comment above the
        node or after it on its line goes with vanilla's change, unless the mod
        changed that comment."""
        ind = _indent(self.o_text, o.start)
        start, end = o.start, o.end
        text = _reindent(self.t_text, t, ind)
        if b is None:                    # no base node: the mod's comments stay
            self.ops.append(Op(start, end, text, why))
            return
        if _own_line(self.o_text, o) and _own_line(self.t_text, t):
            above = _lead_lines(self.b_text, b)
            if _lead_lines(self.o_text, o) == above and _lead_lines(self.t_text, t) != above:
                start = _line_start(self.o_text, _lead(self.o_text, o))
                text = ind + "".join(ln + "\n" + ind for ln in _lead_lines(self.t_text, t)) + text
        ot, bt, tt = _trail(self.o_text, o), _trail(self.b_text, b), _trail(self.t_text, t)
        if ot is not None or not _code_after(self.o_text, o.end).strip():
            same = (ot and ot[1]) == (bt and bt[1])
            if same and (tt and tt[1]) != (bt and bt[1]):
                end = _line_end(self.o_text, o.end) if ot is not None else o.end
                text += " " + tt[1] if tt else ""
        self.ops.append(Op(start, end, text, why))

    def decide_conflict(self, kind, here, b, o, t, place):
        action, by = self.decide(here, kind, o, t, self.t_text, "conflict")
        line = self.o_text.count("\n", 0, o.start) + 1 if o is not None else None
        self.decisions.append(Decision(here, kind, action, by, base=intent.canon(b), ours=intent.canon(o),
                                       theirs=intent.canon(t), line=line))
        if action == TAKE and o is not None:
            if t is None:
                s, e = _delete_span(self.o_text, o)
                self.ops.append(Op(s, e, "", by))
            else:
                self.ops.append(Op(o.start, o.end, _reindent(self.t_text, t, _indent(self.o_text, o.start)), by))
        elif action == TAKE and o is None and t is not None:
            if place is None:
                self.decisions[-1].action = OPEN
            else:                                # put vanilla's node back where ours dropped it
                self.insert(place[0], t, place[1], by)

    def insert(self, anchor, t, o_parent, why):
        where, node = anchor
        notes = _lead(self.t_text, t) < t.start or _trail(self.t_text, t) is not None
        if where == "after":
            if _code_after(self.o_text, node.end).strip():
                # Inside a one-line block. Vanilla's comments come on lines of their own;
                # the line break makes expand_one_line_blocks lay the block out.
                ind = _indent(self.o_text, node.start)
                pos, text = node.end, ("\n" + ind + _reindent(self.t_text, t, ind, comments=True) + "\n" + ind if notes
                                       else " " + _reindent(self.t_text, t, ind))
            else:
                pos = _line_end(self.o_text, node.end)
                ind = _indent(self.o_text, node.start)
                text = "\n" + ind + _reindent(self.t_text, t, ind, comments=True)
        elif where == "before":
            if self.o_text[_line_start(self.o_text, node.start):node.start].strip():
                ind = _indent(self.o_text, node.start)
                pos, text = node.start, ("\n" + ind + _reindent(self.t_text, t, ind, comments=True) + "\n" + ind if notes
                                         else _reindent(self.t_text, t, ind) + " ")
            else:
                pos = _line_start(self.o_text, _lead(self.o_text, node))   # above its comments
                ind = _indent(self.o_text, node.start)
                text = ind + _reindent(self.t_text, t, ind, comments=True) + "\n"
        else:
            if o_parent is not None and o_parent.children is not None:
                close = o_parent.end - 1                 # the closing brace
                ls = _line_start(self.o_text, close)
                if self.o_text[ls:close].strip():
                    pos, text = close, ("\n" + _reindent(self.t_text, t, "", comments=True) + "\n" if notes
                                        else _reindent(self.t_text, t, "") + " ")
                else:
                    ind = _indent(self.o_text, o_parent.start) + "\t"
                    pos, text = ls, ind + _reindent(self.t_text, t, ind, comments=True) + "\n"
            else:
                pos = len(self.o_text)
                sep = "" if self.o_text.endswith("\n") or not self.o_text else "\n"
                text = sep + _reindent(self.t_text, t, "", comments=True) + "\n"
        self.ops.append(Op(pos, pos, text, why, removes=False))

    # --- output -----------------------------------------------------------------
    def apply(self):
        ops = _splice_ops(self.ops, self.overlaps)
        ops = sorted(self.expand_one_line_blocks(ops), key=lambda op: (op.start, op.end))
        out, pos = [], 0
        for op in ops:
            out.append(self.o_text[pos:op.start])
            out.append(op.text)
            pos = op.end
        out.append(self.o_text[pos:])
        return "".join(out), ops

    def expand_one_line_blocks(self, ops):
        """`ops`, with each one-line block of ours that an op gives a line break made
        into one op that lays the block out on more lines. The block is the outermost
        one-line block around the op, so no one-line ancestor holds a line break."""
        units, stack = [], list(diff3.nodes(self.o_text))
        while stack:
            n = stack.pop()
            if n.kind != "block":
                continue
            if "\n" in self.o_text[n.start:n.end]:
                stack.extend(n.children)
            else:
                units.append(n)
        out = list(ops)
        for u in units:
            if not any("\n" in op.text and op.start >= u.open_end and op.end <= u.end - 1 for op in ops):
                continue
            inside = [op for op in out if u.start <= op.start and op.end <= u.end
                      and not (op.start == op.end and op.start in (u.start, u.end))]
            ids = {id(op) for op in inside}
            if any(op.start < u.end and op.end > u.start and id(op) not in ids for op in out):
                continue                             # an op crosses the edge of the block
            text = _spliced(self.o_text[u.start:u.end],
                            [Op(op.start - u.start, op.end - u.start, op.text, op.why) for op in inside])
            new = _expand(text, _indent(self.o_text, u.start))
            if new is None:
                continue
            why = next(op.why for op in inside if "\n" in op.text)
            out = [op for op in out if id(op) not in ids] + [Op(u.start, u.end, new, why)]
        return out


def _splice_ops(ops, dropped=None):
    """`ops` in order, less each op that overlaps an earlier one; `dropped` receives
    those. A dropped op fails the removed-line check (merge_texts), so a file whose
    edits collide is never written."""
    clean, pos = [], 0
    for op in sorted(ops, key=lambda op: (op.start, op.end)):
        if op.start < pos:
            if dropped is not None:
                dropped.append(op)
            continue
        clean.append(op)
        pos = op.end
    return clean


def _spliced(text, ops):
    """`text` with `ops` applied."""
    out, pos = [], 0
    for op in _splice_ops(ops):
        out.append(text[pos:op.start])
        out.append(op.text)
        pos = op.end
    out.append(text[pos:])
    return "".join(out)


def _shift(text, indent):
    """`text` with its later lines moved from the indent of its last line to `indent`."""
    lines = text.split("\n")
    old = lines[-1][:len(lines[-1]) - len(lines[-1].lstrip(" \t"))]
    return "\n".join(lines[:1] + [indent + ln[len(old):] if ln.startswith(old) else ln for ln in lines[1:]])


def _expand(text, indent):
    """The block that `text` holds, with each child on its own line at `indent` and a
    tab, and the closing brace on its own line at `indent`. A child block that opens
    on its first child's line and holds a line break is laid out the same way. A
    comment between the children stays: after its child on the child's line, or on
    its own line. None when anything else lies between the children, or when the
    layout changes more than white space."""
    top = diff3.nodes(text)
    if len(top) != 1 or top[0].kind != "block" or top[0].start != 0 or text[top[0].end:].strip():
        return None

    def notes(gap):
        """(comment after the previous child on its line, comment lines of the gap),
        or None when the gap holds anything but white space and comments."""
        lines = gap.split("\n")
        if any(ln.strip() and not ln.strip().startswith("#") for ln in lines):
            return None
        first = lines[0].strip() if len(lines) > 1 else ""
        rest = [ln.strip() for ln in (lines[1:] if len(lines) > 1 else lines) if ln.strip()]
        return first, rest

    def lay(node, ind):
        inner = ind + "\t"
        kids = node.children
        edges = [node.open_end] + [x for c in kids for x in (c.start, c.end)] + [node.end - 1]
        gaps = [notes(text[edges[k]:edges[k + 1]]) for k in range(0, len(edges), 2)]
        if any(g is None for g in gaps):
            return None
        parts = [text[node.start:node.open_end]]
        for k, c in enumerate(kids):
            after, lines = gaps[k]
            if after:
                parts.append(" " + after)
            parts += ["\n" + inner + ln for ln in lines]
            body = text[c.start:c.end]
            if "\n" in body and c.kind == "block" and "\n" not in text[c.open_end:(c.children[0].start
                                                                         if c.children else c.end)]:
                body = lay(c, inner)
                if body is None:
                    return None
            elif "\n" in body:
                body = _shift(body, inner)
            parts.append("\n" + inner + body)
        after, lines = gaps[-1]
        if after:
            parts.append(" " + after)
        parts += ["\n" + inner + ln for ln in lines]
        parts.append("\n" + ind + "}")
        return "".join(parts)

    new = lay(top[0], indent)
    return new if new is not None and "".join(new.split()) == "".join(text.split()) else None


def splice(text, edits):
    """(new text, spans) for `text` with each (start, end, new text) edit applied.
    Edits do not overlap; two insertions at one offset keep their order. spans: the
    (start, end) of each edit's text in the new text. Then each run of two or more
    lines that hold only white space at an edit is made one empty line, so a removal
    never leaves two empty lines."""
    order = sorted(range(len(edits)), key=lambda k: (edits[k][0], edits[k][1], k))
    out, spans, pos, size = [], [], 0, 0
    for k in order:
        s, e, new = edits[k]
        out.append(text[pos:s])
        size += s - pos
        out.append(new)
        spans.append((size, size + len(new)))
        size += len(new)
        pos = e
    out.append(text[pos:])
    return _squeeze_blank_runs("".join(out), spans), spans


def _squeeze_blank_runs(text, spans):
    lines = text.split("\n")
    starts, at = [], 0
    for line in lines:
        starts.append(at)
        at += len(line) + 1
    touched = set()
    for a, z in spans:
        touched.update(range(text.count("\n", 0, a) - 1, text.count("\n", 0, z) + 2))
    keep, k = [], 0
    while k < len(lines):
        if lines[k].strip():
            keep.append(lines[k])
            k += 1
            continue
        j = k
        while j < len(lines) and not lines[j].strip():
            j += 1
        run = lines[k:j]
        if j - k < 2 or not touched & set(range(k, j)) or k == 0:
            keep.extend(run)                     # untouched, or the file's first lines
        elif j == len(lines):
            keep.append("")                      # the text ends with one line end
        else:
            keep.append("")
        k = j
    return "\n".join(keep)


def removed_lines(ours, merged, ops):
    """Lines of ours that the merged text lacks and that no removing op covers."""
    covered = set()
    for op in ops:
        if not op.removes:
            if op.start != _line_start(ours, op.start):   # an insertion inside a line
                covered.add(ours.count("\n", 0, op.start))
            continue
        first = ours.count("\n", 0, op.start)
        last = ours.count("\n", 0, max(op.end - 1, op.start))
        covered.update(range(first, last + 1))
    from collections import Counter
    a, b = ours.split("\n"), merged.split("\n")
    norm = lambda s: " ".join(s.split())                    # noqa: E731
    # A line counts as removed only when the merged text holds fewer copies of it than
    # ours holds outside the ops, not counting the lines the ops wrote. So a line that
    # the diff shows as moved is not a removal.
    kept = Counter(norm(a[i]) for i in range(len(a)) if i not in covered and norm(a[i]))
    # Rebuild the merged text from the ops and mark each character an op wrote.
    chars, marks, pos = [], [], 0
    for op in sorted(ops, key=lambda o: (o.start, o.end)):
        chars.append(ours[pos:op.start])
        marks.append("0" * (op.start - pos))
        chars.append(op.text)
        marks.append("1" * len(op.text))
        pos = op.end
    chars.append(ours[pos:])
    marks.append("0" * (len(ours) - pos))
    text, mask = "".join(chars), "".join(marks)
    written, start = Counter(), 0
    for line in text.split("\n"):
        if "1" in mask[start:start + len(line)] and norm(line):
            written[norm(line)] += 1
        start += len(line) + 1
    # The merged text as it is, less the lines an op wrote or touched: so a bug that
    # drops a line of ours from the output still shows.
    have = Counter(norm(x) for x in b if norm(x))
    have.subtract(written)
    short = {t: n - have[t] for t, n in kept.items() if n > have[t]}
    out = []
    sm = difflib.SequenceMatcher(None, [norm(x) for x in a], [norm(x) for x in b], autojunk=False)
    for tag, i1, i2, _j1, _j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            for i in range(i1, i2):
                t = norm(a[i])
                if i not in covered and t and short.get(t, 0) > 0:
                    short[t] -= 1
                    out.append((i + 1, a[i]))
    return out


def merge_texts(base, ours, theirs, dialect=diff3.SCRIPT, unwrap=False, decide=None, history=()):
    """Result of a three-way merge of one copy. `decide(path, kind, ours node, theirs
    node, theirs text, what)` returns (action, by); by default a vanilla change where
    ours equals base is taken and every conflict is open. `history`: vanilla's texts
    between the base and theirs, oldest first; a node the mod took from one of them
    counts as vanilla's (see _Merge.later_vanilla)."""
    decide = decide or (lambda path, kind, o, t, tt, what: (OPEN, None) if what == "conflict" else (TAKE, None))
    m = _Merge(base, ours, theirs, dialect, unwrap, decide, history)
    text, ops = m.run()
    res = Result(text, ops, m.decisions)
    res.unexplained = removed_lines(ours, text, ops) + [
        (ours.count("\n", 0, op.start) + 1, f"an edit ({op.why}) overlaps another edit and was not made")
        for op in m.overlaps]
    return res


def merge_inject(base, ours, theirs, decide=None):
    """Result for an INJECT. The INJECT sets children of vanilla's block; vanilla's
    other changes reach the game without a merge. A child that the INJECT sets and
    that vanilla changed between base and theirs is a decision: keep_mod keeps the
    child, take_vanilla deletes it from the INJECT so vanilla's child applies, and
    anything else stays open."""
    decide = decide or (lambda path, kind, o, t, tt, what: (OPEN, None))
    mine = diff3.body(diff3.nodes(ours))
    b_kids = diff3.body(diff3.nodes(base)) if base else []
    t_kids = diff3.body(diff3.nodes(theirs)) if theirs else []
    ops, decisions = [], []
    for m in mine:
        was = sorted(intent.canon(n) for n in b_kids if n.key == m.key)
        now = sorted(intent.canon(n) for n in t_kids if n.key == m.key)
        if was == now:
            continue
        t = next((n for n in t_kids if n.key == m.key), None)
        path = [intent.segment(t, t_kids, diff3.SCRIPT)] if t is not None else [{"key": m.key}]
        action, by = decide(path, "both_changed", m, t, theirs, "conflict")
        decisions.append(Decision(path, "inject_overlap", action, by, base=" | ".join(was) or None,
                                  ours=intent.canon(m), theirs=" | ".join(now) or None,
                                  line=ours.count("\n", 0, m.start) + 1))
        if action == TAKE:
            s, e = _delete_span(ours, m)
            ops.append(Op(s, e, "", by or "vanilla changed the injected key"))
    text, pos = [], 0
    for op in sorted(ops, key=lambda o: o.start):
        text.append(ours[pos:op.start])
        pos = op.end
    text.append(ours[pos:])
    merged = "".join(text)
    return Result(merged, ops, decisions, removed_lines(ours, merged, ops))


def unified(path, before, after):
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        f"a/{path}", f"b/{path}"))


_WORD = re.compile(r"\w+")
