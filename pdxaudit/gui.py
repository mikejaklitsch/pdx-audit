"""GUI override audit: shadowing, fork points, and saved fork points."""

import re
import sys
import difflib
import tarfile
import io
import json
import hashlib
from bisect import bisect_right
from collections import Counter, defaultdict, namedtuple
from pathlib import Path

from pdx_utilities.script_parser import parse, tokenize

from . import session
from .adoption import detect_fork, first_change_after
from .normalize import _norm_clean, _explode_braces, _explode_norms, statement_entries, statement_norms  # noqa: F401
from .report import diff_lines, diff_summary, Finding
from .tracker import MODULE_ROOTS, _git_archive, full_hash, get_commits
from .worddiff import word_spans
from .config import should_skip

def _containment(mod_text, old_text, new_text):
    """Compare a mod block against vanilla's old→new change by normalized-line
    containment. Returns (missing, kept):
      missing: vanilla-added code lines absent from the mod block
      kept: code lines vanilla removed outright that the mod still carries
                (lines still present anywhere in new vanilla text are moves,
                not removals, and are not flagged; bare-brace lines skipped)
    Brace formatting is normalized on both sides (short `{ }` blocks exploded
    onto their own lines) so a formatter collapsing blocks onto a single line
    cannot read as drift. Comment-only lines are ignored on both sides."""
    mod_norms = set(statement_norms(mod_text))
    a = statement_norms(old_text)
    b = statement_norms(new_text)
    new_norms = set(b)
    d = list(difflib.unified_diff(a, b, n=0))
    added = [l[1:] for l in d if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:] for l in d if l.startswith("-") and not l.startswith("---")]
    missing, kept = [], []
    for n in added:
        if n and n.strip("{} ") and n not in mod_norms and n not in missing:
            missing.append(n)
    for n in removed:
        if (n and n.strip("{} ") and n not in new_norms
                and n in mod_norms and n not in kept):
            kept.append(n)
    return missing, kept

GUI_DEF_HEAD = re.compile(r"^\s*(template|local_template|types)\s+([A-Za-z_][\w.]*)")

GUI_TYPE_HEAD = re.compile(r"^\s*type\s+([A-Za-z_][\w.]*)\s*=")

def _gui_code(line):
    """Code portion of a .gui line: comment stripped, string contents blanked
    (quotes kept), so braces inside comments and strings never count."""
    out = []
    in_str = False
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
                out.append('"')
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append('"')
        elif ch == "#":
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out)

def parse_gui_defs(text):
    """Extract template/local_template/types/type definitions from .gui content.
    Line-granular: headers sharing a line with a second definition are missed,
    and a definition's text may include a trailing brace that closed its parent
    on the same line; both are stable across commits, so comparisons hold.
    Returns (defs, clean). clean=False flags brace anomalies vanilla actually
    ships: a stray extra closer (engine-tolerated; accounting resyncs at 0) or
    a block left open at EOF (its definition is dropped). Emitted defs closed
    properly and are trustworthy either way."""
    lines = text.split("\n")
    defs = []
    depth = 0
    clean = True
    open_defs = []  # {kind, name, start, base, opened}
    for i, raw in enumerate(lines):
        code = _gui_code(raw)
        if depth == 0:
            m = GUI_DEF_HEAD.match(code)
            if m:
                # a still-braceless def at depth 0 was malformed; drop it
                open_defs = [d for d in open_defs if d["opened"]]
                open_defs.append({"kind": m.group(1), "name": m.group(2),
                                  "start": i, "base": 0, "opened": False})
        elif depth == 1 and open_defs and open_defs[0]["kind"] == "types":
            m = GUI_TYPE_HEAD.match(code)
            if m:
                open_defs.append({"kind": "type", "name": m.group(1),
                                  "start": i, "base": 1, "opened": False})
        o, c = code.count("{"), code.count("}")
        if o:
            for d in open_defs:
                d["opened"] = True
        depth += o - c
        while open_defs and open_defs[-1]["opened"] and depth <= open_defs[-1]["base"]:
            d = open_defs.pop()
            defs.append({"kind": d["kind"], "name": d["name"], "line": d["start"] + 1,
                         "text": "\n".join(lines[d["start"]:i + 1])})
        if depth < 0:
            depth = 0
            clean = False
    if depth != 0 or any(d["opened"] for d in open_defs):
        clean = False
    return defs, clean

def mod_gui_files(mod_root):
    """[(rel_path, text), ...] for the mod's .gui files under <module>/gui/."""
    out = []
    for fp in session.mod_paths(mod_root, ".gui"):
        rel = fp.relative_to(mod_root)
        if (len(rel.parts) < 3 or rel.parts[0] not in MODULE_ROOTS
                or rel.parts[1] != "gui" or should_skip(rel)):
            continue
        try:
            out.append((rel.as_posix(), session.read_text(fp)))
        except Exception:
            continue
    return out

def build_gui_vanilla(vanilla_repo, commit, modules, label=""):
    """Parse all vanilla .gui under the given modules' gui/ dirs at `commit`.
    Returns (def_idx, file_idx, bad):
      def_idx: (module, kind, name) -> (vfile, text) for template/type;
                 on duplicate names the first file in path-sorted order wins,
                 approximating first-loaded-wins
      file_idx: vfile -> full text (for same-path replacement checks)
      bad: vfiles with brace anomalies (defs still indexed best-effort)"""
    dirs = [f"{m}/gui" for m in sorted(modules)]
    if label:
        print(f"  {label}: extracting {len(dirs)} gui directories...",
              end="", file=sys.stderr, flush=True)
    def_idx, file_idx, bad = {}, {}, []
    raw = _git_archive(vanilla_repo, commit, dirs)
    if not raw:
        if label:
            print(" failed!", file=sys.stderr)
        return def_idx, file_idx, bad
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), ignore_zeros=True) as tf:
            members = [m for m in tf.getmembers()
                       if m.isfile() and m.name.endswith(".gui")]
            for member in sorted(members, key=lambda m: m.name.lower()):
                f = tf.extractfile(member)
                if f is None:
                    continue
                content = f.read().decode("utf-8-sig", errors="replace")
                vfile = member.name
                file_idx[vfile] = content
                defs, clean = parse_gui_defs(content)
                if not clean:
                    bad.append(vfile)
                module = vfile.split("/", 1)[0]
                for d in defs:
                    if d["kind"] not in ("template", "type"):
                        continue
                    key = (module, d["kind"], d["name"])
                    if key not in def_idx:
                        def_idx[key] = (vfile, d["text"])
    except tarfile.TarError:
        if label:
            print(" tar error!", file=sys.stderr)
        return def_idx, file_idx, bad
    if label:
        print(f" {len(def_idx)} defs in {len(file_idx)} files.", file=sys.stderr)
    return def_idx, file_idx, bad

GUI_CACHE_VERSION = 1

def _gui_cache_path(vanilla_repo, commit, modules):
    """Cache file for a commit's parsed GUI index, keyed by the full commit hash
    and the module set. Commit content is immutable, so entries never go stale;
    the version bumps when the parser or index shape changes."""
    full = full_hash(vanilla_repo, commit)
    if not full:
        return None
    mod_key = hashlib.sha1(",".join(sorted(modules)).encode()).hexdigest()[:12]
    return Path(vanilla_repo).parent / "cache" / \
        f"gui-v{GUI_CACHE_VERSION}-{full}-{mod_key}.json"

def build_gui_vanilla_cached(vanilla_repo, commit, modules, label=""):
    """build_gui_vanilla with a per-commit disk cache. Fork detection reads the
    GUI index at every tracked commit; without caching that whole-history scan
    would fall on every audit, so the parsed index is memoized under
    <vanilla-tracker>/cache/ keyed by the immutable commit hash."""
    cache = _gui_cache_path(vanilla_repo, commit, modules)
    if cache and cache.is_file():
        try:
            data = json.loads(cache.read_text())
            def_idx = {tuple(k): (v[0], v[1]) for k, v in data["defs"]}
            if label:
                print(f"  {label}: gui index from cache "
                      f"({len(def_idx)} defs).", file=sys.stderr)
            return def_idx, data["files"], data["bad"]
        except (OSError, ValueError, KeyError, IndexError):
            pass
    def_idx, file_idx, bad = build_gui_vanilla(vanilla_repo, commit, modules, label)
    if cache:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "defs": [[list(k), [v[0], v[1]]] for k, v in def_idx.items()],
                "files": file_idx,
                "bad": bad,
            }
            cache.write_text(json.dumps(payload))
        except OSError:
            pass
    return def_idx, file_idx, bad

def _def_level_changes(old_text, new_text):
    """Definition names that changed/appeared/disappeared between two versions
    of one .gui file."""
    od, _ = parse_gui_defs(old_text)
    nd, _ = parse_gui_defs(new_text)
    om = {(d["kind"], d["name"]): d["text"] for d in od}
    nm = {(d["kind"], d["name"]): d["text"] for d in nd}
    changed = sorted(k for k in om if k in nm and om[k].strip() != nm[k].strip())
    added = sorted(k for k in nm if k not in om)
    removed = sorted(k for k in om if k not in nm)
    return changed, added, removed

def _print_containment(missing, kept, subject):
    if not missing and not kept:
        print(f"  **Mod status:** ✓ {subject} already contains vanilla's change")
        return
    if missing:
        print(f"  **Mod status:** ✗ {subject} is MISSING vanilla lines:")
        for m in missing[:10]:
            print(f"      `{m}`")
        if len(missing) > 10:
            print(f"      ... and {len(missing) - 10} more")
    if kept:
        print(f"  **Mod status:** ✗ {subject} still carries lines vanilla removed:")
        for m in kept[:10]:
            print(f"      `{m}`")
        if len(kept) > 10:
            print(f"      ... and {len(kept) - 10} more")

def _same(a, b):
    """True when two texts are equal after normalization (or both absent)."""
    if a is None or b is None:
        return a is None and b is None
    return statement_norms(a) == statement_norms(b)

def _gap_hash(missing, kept):
    payload = json.dumps([sorted(missing), sorted(kept)], ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()

def _code_end(line):
    """Where a .gui line's comment starts (its length when it has none)."""
    in_str, i = False, 0
    while i < len(line):
        if in_str and line[i] == "\\":
            i += 2
            continue
        if line[i] == '"':
            in_str = not in_str
        elif line[i] == "#" and not in_str:
            return i
        i += 1
    return len(line)


def _line_statements(lines):
    """([(depth before, visual depth, statements)] per line, depth after the last
    line, each line's start offset), read with the shared script parser so the game's
    own tolerance for a stray brace applies.

    A statement is (depth, key, start, end, full end, value), its offsets into the
    whole text: what it sets, keyed `visible`, `blockoverride "text"`, or `@items`
    for a run of bare values such as `0.0 0.0 0.0 1.0` (one statement however the
    values are spread over lines), and for `key = value` the value. A block's range
    stops at its '{', so the statements inside pair on their own, and its full end
    reaches the closing brace. A statement belongs to the line it starts on. The
    visual depth leaves out braces the line closes first."""
    text = "\n".join(lines)
    offsets, pos = [], 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    line_of = lambda at: bisect_right(offsets, at) - 1
    tokens = tokenize(text)
    stmts = [[] for _ in lines]

    def walk(nodes, depth):
        run = None                        # (start, end) of consecutive bare values
        for node in nodes + [None]:
            if node is not None and node.get("type") == "comment":
                continue
            if node is not None and node.get("type") == "node" and node.get("val") is None:
                run = (run[0] if run else node["_start"], node["_end"])
                continue
            if run:
                stmts[line_of(run[0])].append((depth, "@items", run[0], run[1], run[1], None))
                run = None
            if node is None or node.get("type") != "node":
                continue
            key = " ".join(filter(None, (node["key"], node.get("mid_key"),
                                         node.get("val_key") if node.get("op") is None else None)))
            val, start = node["val"], node["_start"]
            if isinstance(val, list):
                stmts[line_of(start)].append((depth, key, start, node.get("_open_end", start),
                                              node["_end"], None))
                walk(val, depth + 1)
            elif val == "PENDING_BLOCK":      # a block pattern its '{' never followed
                end = start + len(node["key"])
                stmts[line_of(start)].append((depth, key, start, end, end, None))
            else:
                stmts[line_of(start)].append((depth, key, start, node["_end"], node["_end"], val))

    walk(parse(tokens, text, strict=False, positions=True), 0)
    info, depth, t = [], 0, 0
    for n, line in enumerate(lines):
        before, closers, leading = depth, 0, True
        while t < len(tokens) and tokens[t]["start"] <= offsets[n] + len(line):
            tok = tokens[t]
            if tok["type"] == "op" and tok["val"] == "}":
                closers += leading
                depth = max(depth - 1, 0)     # a stray '}' closes nothing, as the parser reads it
            elif tok["type"] != "comment":
                leading = False
                depth += tok["type"] == "op" and tok["val"] == "{"
            t += 1
        info.append((before, max(before - closers, 0), stmts[n]))
    return info, depth, offsets


def _distinctive(value):
    """A value distinctive enough to pair two different keys by: a quoted string with
    at least three letters or digits, such as "[OnPause]" (never yes, a number, or
    "#W")."""
    return bool(value) and value.startswith('"') and len(re.findall(r"\w", value)) >= 3


def _pair_statements(a, b):
    """Index pairs between two versions of one stretch's statements, each given as
    (depth, key, value), keeping the order of both sides. Each step pairs only
    between the pairs found by the steps before it: the same key at the same depth;
    the same key at any depth (a wrapper block of your own nests a line deeper than
    vanilla's); the same distinctive value under a different key (vanilla moving
    `onpressed = "[OnPause]"` to `on_action = "[OnPause]"`); and a key standing one
    for one where another stood (the same depths in the same order). Last, a
    distinctive value found exactly once on each side pairs even where vanilla put
    it in a different order, as long as neither statement paired already."""
    pairs = []

    def keys(side, idx, step):
        if step == 0:
            return [(side[i][0], side[i][1]) for i in idx]
        if step == 1:
            return [side[i][1] for i in idx]
        return [side[i][2] if _distinctive(side[i][2]) else (id(side), i) for i in idx]

    def match(ia, ib, step):
        if not ia or not ib:
            return
        if step == 3:
            if [(a[i][0], a[i][1] == "@items") for i in ia] == [(b[i][0], b[i][1] == "@items") for i in ib]:
                pairs.extend(zip(ia, ib))
            return
        pa = pb = 0
        matcher = difflib.SequenceMatcher(None, keys(a, ia, step), keys(b, ib, step), autojunk=False)
        for x, y, size in matcher.get_matching_blocks():
            match(ia[pa:x], ib[pb:y], step + 1)
            pairs.extend(zip(ia[x:x + size], ib[y:y + size]))
            pa, pb = x + size, y + size

    match(list(range(len(a))), list(range(len(b))), 0)
    taken_a, taken_b = {x for x, _y in pairs}, {y for _x, y in pairs}
    once_a = Counter(s[2] for s in a if _distinctive(s[2]))
    once_b = Counter(s[2] for s in b if _distinctive(s[2]))
    where_b = {s[2]: j for j, s in enumerate(b) if once_b[s[2]] == 1}
    for i, s in enumerate(a):
        j = where_b.get(s[2])
        if j is not None and once_a[s[2]] == 1 and i not in taken_a and j not in taken_b:
            pairs.append((i, j))
    return sorted(pairs)


def _indent_unit(lines, info):
    """The copy's indentation step: a tab, or the spaces one level adds."""
    if sum(line[:1] == "\t" for line in lines) >= sum(line[:1] == " " for line in lines):
        return "\t"
    steps, prev = Counter(), None
    for line, (_before, visual, _stmts) in zip(lines, info):
        if not line.strip():
            continue
        width = len(line) - len(line.lstrip())
        if prev and visual == prev[0] + 1 and width > prev[1]:
            steps[width - prev[1]] += 1
        prev = (visual, width)
    return " " * (steps.most_common(1)[0][0] if steps else 4)


def _gui_patch(mod_text, new_text, missing, kept):
    """A copy of a GUI definition lined up against vanilla's current one, for the
    --display app: rows of {"t": text, "c": class}, with "e" holding the character
    ranges of the words that differ where a row pairs with one on the other side.

    Where the copy and vanilla differ, their statements pair by the same key in the
    same order (at the same depth first), or by a key standing one for one where
    another stood; a one-line block pairs with the same block spread over lines. A line of
    yours is 'changed' (or 'removed' when vanilla has nothing in its place) when
    vanilla dropped it, or when it pairs with a line vanilla changed. Vanilla's
    lines the copy lacks, and those lining up with a line of yours marked so, follow
    as 'new' (or 'added'), indented as the copy indents that depth."""
    mod_lines, new_lines = mod_text.split("\n"), (new_text or "").split("\n")
    missing, kept = set(missing), set(kept)
    mod_norms = [tuple(_explode_norms(line)) for line in mod_lines]
    new_norms = [tuple(_explode_norms(line)) for line in new_lines]
    matcher = difflib.SequenceMatcher(None, mod_norms, new_norms, autojunk=False)
    mod_all, new_all = "\n".join(mod_lines), "\n".join(new_lines)
    (mod_info, mod_end, mod_off) = _line_statements(mod_lines)
    (new_info, new_end, new_off) = _line_statements(new_lines)
    # The statements each line starts, matched against the containment check's
    # missing and kept statements.
    stmts_mod, stmts_new = defaultdict(set), defaultdict(set)
    for text, offsets, into in ((mod_all, mod_off, stmts_mod), (new_all, new_off, stmts_new)):
        for at, norm in statement_entries(text):
            into[bisect_right(offsets, at) - 1].add(norm)
    depth_at = lambda info, end, n: info[n][0] if n < len(info) else end
    unit = _indent_unit(mod_lines, mod_info)
    indent = lambda line: line[:len(line) - len(line.lstrip())]
    rows = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            rows += [{"t": line, "c": "context"} for line in mod_lines[i1:i2]]
            continue
        base_mod, base_new = depth_at(mod_info, mod_end, i1), depth_at(new_info, new_end, j1)
        mine = [(n, s) for n in range(i1, i2) for s in mod_info[n][2]]
        theirs = [(n, s) for n in range(j1, j2) for s in new_info[n][2]]
        pairs = _pair_statements([(s[0] - base_mod, s[1], s[5]) for _n, s in mine],
                                 [(s[0] - base_new, s[1], s[5]) for _n, s in theirs])
        # A line differs only in layout when every statement on it pairs with one that
        # reads the same (a block also having nothing unpaired inside): the copy
        # already matches vanilla there, so neither side's line is marked or shown.
        paired = dict(pairs)
        inner_mine = _unpaired_contents(mine, set(paired), mod_all)
        inner_vanilla = _unpaired_contents(theirs, set(paired.values()), new_all)
        same = {(x, y) for x, y in pairs
                if not inner_mine.get(x) and not inner_vanilla.get(y)
                and word_spans(mod_all[mine[x][1][2]:mine[x][1][3]],
                               new_all[theirs[y][1][2]:theirs[y][1][3]]) == ([], [])}
        layout_mine = _layout_only(mine, {x for x, _y in same}, mod_off)
        layout_new = _layout_only(theirs, {y for _x, y in same}, new_off)
        changed_new = {n for n in range(j1, j2) if stmts_new[n] & missing} - layout_new
        flagged = {n for n in range(i1, i2) if stmts_mod[n] & kept}
        flagged |= {mine[x][0] for x, y in pairs if theirs[y][0] in changed_new}
        flagged -= layout_mine

        # Vanilla's lines to show: those the copy lacks and the ones a marked line of
        # yours lines up with, each with the blocks around it that open in this
        # stretch, and the braces that close the blocks shown.
        shown, linked = set(changed_new), defaultdict(list)
        for x, y in pairs:
            if mine[x][0] in flagged:
                linked[mine[x][0]].append(theirs[y][0])
        for lines in linked.values():
            shown.update(range(min(lines), max(lines) + 1))
        stack, parents, closer = [], {}, {}
        for n in range(j1, j2):
            parents[n] = list(stack)
            for ch in _gui_code(new_lines[n]):
                if ch == "{":
                    stack.append(n)
                elif ch == "}" and stack:
                    closer[stack.pop()] = n
        for n in list(shown):
            shown.update(parents[n])
        shown.update(closer[n] for n in list(shown) if n in closer)
        wanted = sorted(shown)

        # Vanilla's lines take the copy's indentation for their depth, measured from
        # your first line here that has text, or the last one before.
        ref = next((n for n in range(i1, i2) if mod_lines[n].strip()), None)
        if ref is None:
            ref = next((n for n in range(i1 - 1, -1, -1) if mod_lines[n].strip()), None)
        texts = {}
        for n in wanted:
            level = base_mod + new_info[n][1] - base_new
            if ref is None:
                prefix = unit * max(level, 0)
            else:
                prefix, shift = indent(mod_lines[ref]), level - mod_info[ref][1]
                prefix = (prefix + unit * shift if shift >= 0
                          else prefix[:max(len(prefix) - len(unit) * -shift, 0)])
            texts[n] = prefix + new_lines[n].lstrip()

        # Highlights are found on offsets into the whole text, so a statement spread
        # over lines compares as one, then split back into each line's ranges.
        emph_mine, emph_vanilla = [], []
        for x, y in pairs:
            (n, (_d, _k, s0, s1, full0, _v)), (m, (_dv, _kv, t0, t1, full1, _vv)) = mine[x], theirs[y]
            if n not in flagged or m not in texts:
                continue
            if (mod_all[s1 - 1:s1] == "{") != (new_all[t1 - 1:t1] == "{"):
                s1, t1 = full0, full1     # a block against a plain value: compare both whole
            spans_mine, spans_vanilla = word_spans(mod_all[s0:s1], new_all[t0:t1])
            emph_mine += [(a + s0, b + s0) for a, b in spans_mine]
            emph_vanilla += [(a + t0, b + t0) for a, b in spans_vanilla]
            # When two blocks line up, what changed inside them: the unpaired statements
            # directly inside each, unless one fills the lines it is on (its sign says so).
            for i in inner_mine.get(x, []):
                _row, (_d, _k, start, _e, full, _v) = mine[i]
                if not _fills_lines(start, full, mod_lines, mod_off):
                    emph_mine.append((start, full))
            for i in inner_vanilla.get(y, []):
                _row, (_d, _k, start, _e, full, _v) = theirs[i]
                if not _fills_lines(start, full, new_lines, new_off):
                    emph_vanilla.append((start, full))
        by_line_mine = _spans_by_line(emph_mine, mod_lines, mod_off)
        by_line_vanilla = _spans_by_line(emph_vanilla, new_lines, new_off)
        for n in range(i1, i2):
            tag = ("changed" if op == "replace" else "removed") if n in flagged else "context"
            spans = _merge_spans(by_line_mine.get(n, [])) if n in flagged else []
            rows.append(dict({"t": mod_lines[n], "c": tag}, **({"e": spans} if spans else {})))
        for n in wanted:
            moved = len(texts[n]) - len(new_lines[n])
            spans = _merge_spans([(a + moved, b + moved) for a, b in by_line_vanilla.get(n, [])])
            rows.append(dict({"t": texts[n], "c": "new" if op == "replace" else "added"},
                             **({"e": spans} if spans else {})))
    return rows


def _layout_only(side, same, offsets):
    """Line indexes on one side of a stretch where every statement touching the line
    (one spread over several lines touches each) is in `same`: it pairs with a
    statement that reads the same, so the line differs only in layout."""
    touching = defaultdict(list)
    for i, (n, stmt) in enumerate(side):
        last = bisect_right(offsets, max(stmt[3] - 1, stmt[2])) - 1
        for line in range(n, last + 1):
            touching[line].append(i in same)
    return {line for line, flags in touching.items() if all(flags)}


def _spans_by_line(spans, lines, offsets):
    """{line index: [(start, end)]}: ranges given as offsets into the whole text,
    split at line breaks and trimmed of whitespace at their edges."""
    out = defaultdict(list)
    for start, end in spans:
        n = bisect_right(offsets, start) - 1
        while n < len(lines) and offsets[n] < end:
            a = max(start, offsets[n]) - offsets[n]
            b = min(end, offsets[n] + len(lines[n])) - offsets[n]
            line = lines[n]
            while a < b and line[a].isspace():
                a += 1
            while b > a and line[b - 1].isspace():
                b -= 1
            if a < b:
                out[n].append((a, b))
            n += 1
    return out


def _fills_lines(start, end, lines, offsets):
    """True when the range covers all the code on every line it touches."""
    n = bisect_right(offsets, start) - 1
    while n < len(lines) and offsets[n] < end:
        line = lines[n]
        code = line[:_code_end(line)].strip()
        part = line[max(start, offsets[n]) - offsets[n]:min(end, offsets[n] + len(line)) - offsets[n]].strip()
        if code and part != code:
            return False
        n += 1
    return True


def _unpaired_contents(side, paired, text):
    """{index of a block statement: indexes of the unpaired statements directly
    inside it}, for one side of a stretch given as [(line index, statement)]."""
    inner, stack = defaultdict(list), []
    for i, (_n, stmt) in enumerate(side):
        while stack and side[stack[-1]][1][0] >= stmt[0]:
            stack.pop()
        if stack and i not in paired:
            inner[stack[-1]].append(i)
        if text[stmt[3] - 1:stmt[3]] == "{":
            stack.append(i)
    return inner


def _merge_spans(spans):
    """Highlighted ranges in order, with overlapping ones joined."""
    out = []
    for start, end in sorted(spans):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out

def _tag(msg):
    parts = (msg or "").split()
    return parts[0] if parts else ""

def gui_def_target(key):
    """Record target name for a shadowed GUI definition (module, kind, name)."""
    module, kind, name = key
    return f"gui:{module}/{kind}/{name}"

def gui_file_target(rel):
    """Record target name for a same-path .gui file replacement."""
    return f"guifile:{rel}"

# How a copy's baseline was chosen, for the detail output.
Baseline = namedtuple("Baseline", "text vfile hash msg index how history partials")

def commits_up_to(commits, new_hash):
    """The newest-first commit list trimmed so `new_hash` is its first entry."""
    for i, (h, _msg) in enumerate(commits):
        if new_hash.startswith(h) or h.startswith(new_hash):
            return commits[i:]
    return commits

def detect_baselines(vanilla_repo, commits, modules, mdefs, mod_file_texts,
                     bases=None, fallback_hash=None):
    """Baseline vanilla version for each shadowed definition and each same-path
    file replacement. `commits` is newest first and ends the history at the
    audit's new version.

    Precedence: a version saved in the findings record for the target
    (`bases`: target -> version tag); otherwise the newest vanilla patch the
    copy adopted (adoption.detect_fork); otherwise `fallback_hash`, the window's
    old version. Returns (def_base, file_base) mapping (module, kind, name) and
    rel paths to Baseline."""
    bases = bases or {}
    old_first = list(reversed(commits))
    tags = [_tag(msg) for _h, msg in old_first]
    all_idx = []
    for i, (h, _msg) in enumerate(old_first):
        di, fi, _ = build_gui_vanilla_cached(
            vanilla_repo, h, modules, f"fork scan {i + 1}/{len(old_first)} ({h[:7]})")
        all_idx.append((di, fi))
    fallback_index = None
    if fallback_hash:
        for i, (h, _msg) in enumerate(old_first):
            if fallback_hash.startswith(h) or h.startswith(fallback_hash):
                fallback_index = i

    def resolve(target, mod_text, texts, vfiles):
        # Adoption always runs: partial adoption is a fact about the copy, so it
        # must not vanish once a finding's own recorded base feeds the next run.
        fork, partials = detect_fork(mod_text, texts, norms=statement_norms)
        tag = bases.get(target)
        if tag in tags:
            i = len(tags) - 1 - tags[::-1].index(tag)
            if texts[i] is not None:
                h, msg = old_first[i]
                return Baseline(texts[i], vfiles[i], h, msg, i, "recorded", texts,
                                [(tags[p], got, total) for p, got, total in partials if p <= i])
        how = "detected"
        if fork is None:
            if fallback_index is None or texts[fallback_index] is None:
                return None
            fork, partials, how = fallback_index, [], "window"
        h, msg = old_first[fork]
        return Baseline(texts[fork], vfiles[fork], h, msg, fork, how, texts,
                        [(tags[i], got, total) for i, got, total in partials])

    def_base = {}
    for d in mdefs:
        key = (d["module"], d["kind"], d["name"])
        if key in def_base:
            continue
        entries = [idx[0].get(key) for idx in all_idx]
        texts = [e[1] if e else None for e in entries]
        vfiles = [e[0] if e else None for e in entries]
        b = resolve(gui_def_target(key), d["text"], texts, vfiles)
        if b:
            def_base[key] = b

    file_base = {}
    for rel, mtext in mod_file_texts.items():
        texts = [idx[1].get(rel) for idx in all_idx]
        b = resolve(gui_file_target(rel), mtext, texts, [rel] * len(texts))
        if b:
            file_base[rel] = b
    return def_base, file_base

def build_fork_baselines(vanilla_repo, commits, modules, mdefs, mod_file_texts, bases=None):
    """Detected fork points for --stamp-fork-points (no window fallback).
    Returns (def_base, file_base):
      def_base: (module, kind, name) -> (vfile, text, fork_hash, fork_msg, recorded)
      file_base: rel -> (text, fork_hash, fork_msg, recorded)"""
    defs, files = detect_baselines(vanilla_repo, commits, modules, mdefs,
                                   mod_file_texts, bases=bases)
    return ({k: (b.vfile, b.text, b.hash, b.msg, b.how == "recorded") for k, b in defs.items()},
            {r: (b.text, b.hash, b.msg, b.how == "recorded") for r, b in files.items()})

def _mod_defs(files):
    mdefs, skipped = [], 0
    for rel, text in files:
        defs, clean = parse_gui_defs(text)
        if not clean:
            print(f"Warning: unbalanced braces in mod file {rel}; "
                  f"definitions parsed best-effort", file=sys.stderr)
        module = rel.split("/", 1)[0]
        for d in defs:
            if d["kind"] in ("template", "type"):
                d["file"] = rel
                d["module"] = module
                mdefs.append(d)
            else:
                skipped += 1
    return mdefs, skipped

def run_gui_audit(mod_root, vanilla_repo, old_hash, old_msg, new_hash, new_msg, args, ctx=None):
    files = mod_gui_files(mod_root)
    if not files:
        print("No mod .gui files found.", file=sys.stderr)
        if ctx is not None:
            ctx.scanned["gui"] = 0
        return []

    mdefs, skipped = _mod_defs(files)
    if args.block:
        mdefs = [d for d in mdefs if d["name"] == args.block]
    if ctx is not None:
        ctx.scanned["gui"] = len(mdefs) if args.block else len(mdefs) + len(files)

    modules = sorted({rel.split("/", 1)[0] for rel, _ in files})
    print(f"Scanning {len(mdefs)} GUI definitions in {len(files)} mod .gui files...",
          file=sys.stderr)
    old_idx, old_files, bad_old = build_gui_vanilla_cached(
        vanilla_repo, old_hash, modules, f"old ({old_hash[:7]})")
    new_idx, new_files, bad_new = build_gui_vanilla_cached(
        vanilla_repo, new_hash, modules, f"new ({new_hash[:7]})")
    if not new_idx and not new_files:
        print("Could not read vanilla .gui files (archive failed).", file=sys.stderr)
        sys.exit(1)
    if bad_old or bad_new:
        bad_files = sorted(set(bad_old) | set(bad_new))
        print(f"Warning: {len(bad_files)} vanilla .gui file(s) have unbalanced "
              f"braces (vanilla's own); parsed best-effort:", file=sys.stderr)
        for vf in bad_files:
            print(f"  {vf}", file=sys.stderr)

    new_tag = ctx.new_tag if ctx is not None else _tag(new_msg)
    old_tag = _tag(old_msg)

    # Baselines are per copy by default: a saved review version, else the newest
    # patch the copy adopted, else the window's old version. An explicit fixed
    # window (--full / --old / --new) measures everything from old_hash instead.
    if ctx is not None:
        fixed_window = ctx.fixed_window
    else:
        fixed_window = bool(args.full or args.old or args.new)
    fork_defs, fork_files = {}, {}
    history_len = 0
    if not fixed_window:
        commits = commits_up_to(ctx.commits if ctx is not None else get_commits(vanilla_repo),
                                new_hash)
        history_len = len(commits)
        mod_file_texts = {rel: text for rel, text in files}
        fork_defs, fork_files = detect_baselines(
            vanilla_repo, commits, modules, mdefs, mod_file_texts,
            bases=ctx.bases if ctx is not None else None, fallback_hash=old_hash)

    old_first_msgs = []
    if not fixed_window:
        old_first_msgs = [m for _h, m in reversed(commits)]

    def since_and_base(b):
        if fixed_window or b is None:
            return new_tag, old_tag
        i = first_change_after(b.history, b.index, history_len - 1, norms=statement_norms)
        since = _tag(old_first_msgs[i]) if i is not None else new_tag
        return since, _tag(b.msg)

    # Files that replace a vanilla file at the same path are audited at file
    # level below; their definitions are replacements, not shadows.
    same_path = {rel for rel, _ in files if rel in old_files or rel in new_files}

    changed, new_coll, van_removed, unchanged = [], [], [], []
    mod_only = 0
    partial = []
    for d in mdefs:
        if d["file"] in same_path:
            continue
        key = (d["module"], d["kind"], d["name"])
        b = None
        if not fixed_window:
            b = fork_defs.get(key)
            o = (b.vfile, b.text) if b else None
            if b and b.partials:
                partial.append((d, b))
        else:
            o = old_idx.get(key)
        n = new_idx.get(key)
        if not o and not n:
            mod_only += 1
        elif o and not n:
            van_removed.append((d, o))
        elif not o and n:
            new_coll.append((d, n))
        elif _same(o[1], n[1]):
            unchanged.append((d, n))
        else:
            changed.append((d, o, n, b))

    # Split changed shadows by whether the mod copy already carries vanilla's
    # change. A definition vanilla edited whose mod shadow already reflects the
    # edit is reconciled, not drift, and must not be reported as action-needed.
    stale_changed, reconciled_changed = [], []
    for d, o, n, b in changed:
        missing, kept = _containment(d["text"], o[1], n[1])
        entry = (d, o, n, b, missing, kept)
        (stale_changed if (missing or kept) else reconciled_changed).append(entry)

    replaced = []
    for rel, mtext in files:
        b = None
        if not fixed_window:
            b = fork_files.get(rel)
            ot = b.text if b else None
            if b and b.partials:
                partial.append(({"name": rel, "file": rel, "line": None, "module": rel.split("/")[0],
                                 "kind": "file"}, b))
        else:
            ot = old_files.get(rel)
        nt = new_files.get(rel)
        if ot is None and nt is None:
            continue
        if ot is not None and nt is not None and _same(ot, nt):
            continue
        # A whole replaced file is rebuilt by design, so line containment would
        # say "missing" for nearly everything; the baseline alone decides.
        replaced.append((rel, ot, nt, b))

    if not fixed_window:
        header = [
            f"# GUI Override Audit: per-copy baseline → {new_hash[:7]}",
            f"*each copy compared against the vanilla version it was last synced "
            f"to → {new_msg}*",
        ]
    else:
        header = [f"# GUI Override Audit: {old_hash[:7]} → {new_hash[:7]} (fixed window)"]
        if old_msg or new_msg:
            header.append(f"*{old_msg} → {new_msg}*")
    header.append("")
    print("\n".join(header))

    if fork_files:
        print("## Baselines for Replaced Files")
        print()
        print("The vanilla version each replaced file was measured against: `recorded` "
              "is a version saved with --stamp-fork-points, `detected` is the newest "
              "vanilla patch the copy adopted, `window` is the audit's old version.")
        print()
        for rel in sorted(fork_files):
            b = fork_files[rel]
            print(f"- `{rel}`: {_tag(b.msg) or '?'} *({b.how})*")
        print()
    shadow_defs = [d for d in mdefs if d["file"] not in same_path]

    n_tmpl = sum(1 for d in shadow_defs if d["kind"] == "template")
    n_type = len(shadow_defs) - n_tmpl
    extras = []
    if len(mdefs) > len(shadow_defs):
        extras.append(f"{len(mdefs) - len(shadow_defs)} in same-path files "
                      f"audited at file level")
    if skipped:
        extras.append(f"{skipped} file-scoped skipped")
    counts = [
        f"**{len(shadow_defs)}** shadow-capable definitions ({n_tmpl} template, "
        f"{n_type} type) in **{len(files)}** mod .gui files"
        + ("; " + "; ".join(extras) if extras else ""),
        f"- **{len(stale_changed)}** shadowed vanilla definitions changed and not "
        f"reconciled: action needed",
    ]
    if reconciled_changed:
        counts.append(f"- **{len(reconciled_changed)}** shadowed definitions vanilla "
                      f"changed but the mod copy already reconciled")
    counts += [
        f"- **{len(replaced)}** same-path file replacements where vanilla's file "
        f"changed: action needed",
        f"- **{len(new_coll)}** new name collisions (vanilla added a same-name definition)",
        f"- **{len(van_removed)}** shadowed definitions removed from vanilla",
        f"- **{len(unchanged)}** unchanged shadows, **{mod_only}** mod-only definitions",
        "",
    ]
    if partial:
        counts.insert(-1, f"- **{len(partial)}** copies where an earlier vanilla patch "
                          f"was only partly adopted: review")
    print("\n".join(counts))

    def _print_changed_def(d, o, n, b, missing, kept):
        print(f"### {d['name']}")
        print(f"- **Kind:** {d['kind']}")
        print(f"- **Mod:** `{d['file']}:{d['line']}`")
        print(f"- **Vanilla:** `{n[0]}`")
        if b is not None:
            print(f"- **Measured from:** {_tag(b.msg)} *({b.how})*")
        if args.diff:
            dl = diff_lines(o[1], n[1], d["name"])
            if dl:
                print("```diff")
                print("".join(dl).rstrip("\n"))
                print("```")
        else:
            n_add, n_rem, key_lines = diff_summary(o[1], n[1])
            for line in key_lines[:10]:
                print(line)
            if len(key_lines) > 10:
                print(f"  ... and {len(key_lines) - 10} more lines")
            print(f"  *({n_add} added, {n_rem} removed)*")
        _print_containment(missing, kept, "mod copy")
        print()

    if stale_changed:
        print(f"## Changed Shadowed Definitions: mod GUI override is suppressing "
              f"vanilla changes ({len(stale_changed)})")
        print()
        for entry in stale_changed:
            _print_changed_def(*entry)

    if reconciled_changed:
        print(f"## Changed Shadowed Definitions: mod copy already reconciled "
              f"({len(reconciled_changed)})")
        print()
        print("Vanilla changed these definitions and the mod's shadow copy already "
              "carries the change: informational, no action needed.")
        print()
        for entry in reconciled_changed:
            _print_changed_def(*entry)

    if partial:
        print(f"## Partly Adopted Vanilla Patches ({len(partial)})")
        print()
        print("The copy adopted a later vanilla patch, but only part of these "
              "earlier ones; the lines it lacks from them are not measured again.")
        print()
        for d, b in partial:
            shown = ", ".join(f"{t}: {g}/{tot}" for t, g, tot in b.partials)
            where = f"`{d['file']}:{d['line']}`" if d.get("line") else f"`{d['file']}`"
            print(f"- **{d['name']}** {where}: {shown} of vanilla's changed lines")
        print()

    if replaced:
        print(f"## Same-Path File Replacements: vanilla file changed underneath "
              f"({len(replaced)})")
        print()
        print("The mod file fully replaces the vanilla file at the same path, so "
              "vanilla's changes to its version are suppressed. Definitions "
              "inside these files are audited here, not as shadows.")
        print()
        for rel, ot, nt, b in replaced:
            print(f"### {rel}")
            if b is not None:
                print(f"- **Measured from:** {_tag(b.msg)} *({b.how})*")
            if ot is None:
                print("  *(vanilla added this file; the mod file now overrides "
                      "a file that did not exist before)*")
            elif nt is None:
                print("  *(vanilla removed this file; the mod copy is now the only one)*")
            else:
                ch, ad, rm = _def_level_changes(ot, nt)
                def _fmt(ks):
                    s = ", ".join(f"{k}:{name}" for k, name in ks[:8])
                    return s + (" …" if len(ks) > 8 else "")
                if ch:
                    print(f"  - changed defs: {_fmt(ch)}")
                if ad:
                    print(f"  - added defs: {_fmt(ad)}")
                if rm:
                    print(f"  - removed defs: {_fmt(rm)}")
                n_add, n_rem, _ = diff_summary(ot, nt)
                print(f"  *({n_add} lines added, {n_rem} removed in vanilla's version)*")
            print()

    if new_coll:
        print(f"## New Name Collisions: vanilla now defines a name the mod also "
              f"defines ({len(new_coll)})")
        print()
        for d, n in new_coll:
            print(f"- **{d['kind']}:{d['name']}**, mod `{d['file']}:{d['line']}` "
                  f"vs vanilla `{n[0]}`")
        print()

    if van_removed:
        print(f"## Shadowed Definitions Removed from Vanilla ({len(van_removed)})")
        print()
        for d, o in van_removed:
            print(f"- **{d['kind']}:{d['name']}**, mod `{d['file']}:{d['line']}` "
                  f"(was in `{o[0]}`); the mod copy is now the only definition")
        print()

    if not stale_changed and not replaced and not new_coll:
        if reconciled_changed:
            print("**All GUI overrides are current with vanilla** "
                  f"({len(reconciled_changed)} changed but already reconciled).")
        else:
            print("**All GUI overrides are current with vanilla.**")
    else:
        print("---")
        print(f"**Action needed:** "
              f"{len(stale_changed)} shadowed definitions drifted, "
              f"{len(replaced)} replaced files changed, "
              f"{len(new_coll)} new collisions.")
        if not args.diff and stale_changed:
            print("Run with `--diff` for full unified diffs.")
    print()
    print("_Implicit GUI overrides: a mod template/type definition shadows the "
          "vanilla definition of the same name. The mod loads after vanilla, so "
          "the override applies; this reports where vanilla then changed underneath "
          "it._")

    # Findings for the cross-audit triage. The class (report.KIND) supplies the
    # shared wording and remedy; each finding carries its item, its identity key
    # and the patch it comes from.
    want = getattr(args, "results_file", None)   # rich payload only for the --display app
    old_first_tags = [_tag(m) for m in old_first_msgs]

    def partial_patch(d, b, tag):
        """The copy lined up against vanilla's text at a partly adopted patch, with
        the lines of that patch the copy lacks or still carries marked."""
        if tag not in old_first_tags:
            return None
        i = old_first_tags.index(tag)
        before, after = (b.history[i - 1] if i > 0 else None), b.history[i]
        if before is None or after is None:
            return None
        mod_text = dict(files)[d["file"]] if d.get("kind") == "file" else d["text"]
        missing, kept = _containment(mod_text, before, after)
        return {"patch": _gui_patch(mod_text, after, missing, kept)}

    findings = []
    for d, o, n, b, missing, kept in stale_changed:
        key = (d["module"], d["kind"], d["name"])
        since, base = since_and_base(b)
        this_patch = fixed_window or not _same((old_idx.get(key) or (None, None))[1], n[1])
        kind = "gui_shadow_stale" if this_patch else "gui_shadow_behind"
        findings.append(Finding(kind, d["name"], f"{d['file']}:{d['line']}",
                                f"{len(missing)} missing, {len(kept)} removed lines kept",
                                {"patch": _gui_patch(d["text"], n[1], missing, kept),
                                 "vanilla_file": n[0]} if want else None,
                                {"target": gui_def_target(key), "gap": _gap_hash(missing, kept)},
                                new_tag if this_patch else since, base))
    for d, b in partial:
        target = (gui_file_target(d["file"]) if d.get("kind") == "file"
                  else gui_def_target((d["module"], d["kind"], d["name"])))
        where = f"{d['file']}:{d['line']}" if d.get("line") else d["file"]
        for t, got, total in b.partials:
            findings.append(Finding("gui_partial_adoption", d["name"], where,
                                    f"{t}: {got}/{total} of vanilla's changed lines adopted",
                                    partial_patch(d, b, t) if want else None,
                                    {"target": target, "patch": t,
                                           "got": got, "total": total},
                                    t, _tag(b.msg)))
    for rel, ot, nt, b in replaced:
        target = {"target": gui_file_target(rel)}
        if ot is None or nt is None:
            findings.append(Finding("gui_file_review", rel, rel,
                                    "vanilla added" if ot is None else "vanilla removed",
                                    None, dict(target, change="added" if ot is None else "removed"),
                                    new_tag, old_tag))
            continue
        since, base = since_and_base(b)
        this_patch = fixed_window or not _same(old_files.get(rel), nt)
        missing, kept = _containment(dict(files)[rel], ot, nt)
        data = None
        if want:
            ch, ad, rm = _def_level_changes(ot, nt)
            data = {"diff": "".join(diff_lines(ot, nt, rel)).rstrip("\n"),
                    "defs": {"changed": [f"{k}:{nm}" for k, nm in ch],
                             "added": [f"{k}:{nm}" for k, nm in ad],
                             "removed": [f"{k}:{nm}" for k, nm in rm]}}
        findings.append(Finding("gui_file_replaced" if this_patch else "gui_file_behind",
                                rel, rel, "", data,
                                dict(target, gap=_gap_hash(missing, kept)),
                                new_tag if this_patch else since, base))
    for d, o in van_removed:
        findings.append(Finding("gui_van_removed", d["name"], f"{d['file']}:{d['line']}",
                                "", {"vanilla_file": o[0]} if want else None,
                                {"target": gui_def_target((d["module"], d["kind"], d["name"]))},
                                new_tag))
    for d, n in new_coll:
        findings.append(Finding("gui_new_collision", d["name"], f"{d['file']}:{d['line']}",
                                "", {"vanilla_file": n[0]} if want else None,
                                {"target": gui_def_target((d["module"], d["kind"], d["name"]))},
                                new_tag))
    for d, _o, _n, _b, _m, _k in reconciled_changed:
        findings.append(Finding("gui_reconciled", d["name"], f"{d['file']}:{d['line']}",
                                "", None,
                                {"target": gui_def_target((d["module"], d["kind"], d["name"]))}))
    return findings

def run_stamp_fork_points(mod_root, vanilla_repo, commits, store, refresh=False):
    """Detect each GUI copy's fork point and save it in the findings record
    (reviewed_against). Never touches the mod's files. Saved fork points are
    kept unless refresh=True."""
    files = mod_gui_files(mod_root)
    if not files:
        print("No mod .gui files found.")
        return
    modules = sorted({rel.split("/", 1)[0] for rel, _ in files})
    mdefs, _skipped = _mod_defs(files)
    mod_file_texts = {rel: text for rel, text in files}
    print("Detecting fork points...", file=sys.stderr)
    def_base, file_base = build_fork_baselines(
        vanilla_repo, commits, modules, mdefs, mod_file_texts)

    plan = [(gui_def_target(k), _tag(v[3])) for k, v in def_base.items()]
    plan += [(gui_file_target(rel), _tag(v[2])) for rel, v in file_base.items()]
    saved = store.state["reviewed_against"]
    written, differing = [], []
    for target, tag in sorted(plan):
        if not tag:
            continue
        if target in saved and not refresh:
            if saved[target] != tag:
                differing.append((target, saved[target], tag))
            continue
        if saved.get(target) != tag:
            saved[target] = tag
            written.append((target, tag))

    for target, tag in written:
        print(f"  {target}: {tag}")
    if differing:
        print(f"Kept {len(differing)} saved fork point(s) that differ from detection "
              f"(re-run with --refresh to update):")
        for target, old, new in differing:
            print(f"  - {target}: saved {old}, detected {new}")
    if store.save():
        print(f"Saved {len(written)} fork point(s) to {store.record_path}.")
    else:
        print("No new fork points to save.")
