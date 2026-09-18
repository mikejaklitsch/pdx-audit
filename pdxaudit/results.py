"""What the --display app shows: a run's findings as records, and the record
actions the app takes without a new run (dismiss, restore, orphaned records).

A run started from the app calls the command line with --results-file, which
writes build_payload's dictionary as JSON. Records keep each finding's kind and
key, so a dismissal made from a record has the same id as one made with
--dismiss."""
import datetime
import difflib
import hashlib
import re
from pathlib import Path
from stat import S_ISREG

from pdx_utilities.constants import SCAN_TOPDIRS
from pdx_utilities.script_parser import tokenize

from . import ledger
from .config import should_skip
from .diff3 import CONFLICT_KINDS, norm_value
from .overrides import _brace_extract, find_overrides
from .report import (KIND, SEV_INFO, Finding, _is_value_finding, change_kind, line_label,
                     value_pair)
from .store import open_store
from .worddiff import word_spans


def _split_location(loc):
    loc = loc or ""
    if ":" in loc and loc.rsplit(":", 1)[1].isdigit():
        file, line = loc.rsplit(":", 1)
        return file, line
    return loc or "?", ""


def build_payload(findings, *, mod_name="", old_msg="", new_msg="", new_tag="",
                  selected=(), dismissed=0, triage="", details=(), warnings=(), window=""):
    """The app's view of one run. `findings` are the visible (not dismissed)
    findings; informational ones are counted, not listed. A target's block view (a
    GUI definition, GUI file, REPLACE or INJECT block) is stored once under
    `blocks`, and every finding of that target points at it. A change whose finding
    is not listed, because it was dismissed, keeps no mark."""
    blocks, block_of = {}, {}
    listed = {ledger.finding_id(f) for f in findings if KIND[f.kind][0] != SEV_INFO}
    for f in findings:
        if f.data and "changes" in f.data and KIND[f.kind][0] != SEV_INFO:
            target = (f.key or {}).get("target") or f"{f.location}\n{f.name}"
            bid = hashlib.sha1(target.encode("utf-8")).hexdigest()[:12]
            where = (f.data.get("file"), f.data.get("line"))
            if bid in blocks and (blocks[bid].get("file"), blocks[bid].get("line")) != where:
                # Another copy of the same target, such as one REPLACE in two files.
                bid = hashlib.sha1(f"{target}\n{where[0]}:{where[1]}".encode("utf-8")).hexdigest()[:12]
            if bid not in blocks:
                blocks[bid] = dict(f.data, changes=[
                    c if c["fid"] is None or c["fid"] in listed else dict(c, mark=None, fid=None)
                    for c in f.data["changes"]])
            block_of[id(f)] = bid

    records, info = [], 0
    for f in findings:
        sev, audit, label, fix = KIND[f.kind]
        if sev == SEV_INFO:
            info += 1
            continue
        fid = ledger.finding_id(f)
        file, line = _split_location(f.location)
        rec = {"fid": fid, "id": ledger.short_id(fid), "kind": f.kind, "name": f.name,
               "audit": audit, "sev": sev, "label": label, "fix": fix,
               "location": f.location or "", "file": file, "line": line,
               "detail": f.detail or "", "key": f.key, "since": f.since or "",
               "base": f.base or "", "dismissible": ledger.is_dismissible(f),
               "earlier": bool(new_tag and f.since and f.since != new_tag)}
        if id(f) in block_of:
            rec["block"] = block_of[id(f)]
        elif f.data:
            rec["data"] = f.data
        if _is_value_finding(f):
            rec["path"] = line_label(f.key)
            rec["yours"], rec["vanilla"] = value_pair(
                change_kind(f.kind), f.key["yours"], f.key["vanilla"], f.since, f.key.get("was"))
        records.append(rec)

    return {"mod": mod_name, "old": old_msg or "", "new": new_msg or "",
            "new_tag": new_tag or "", "selected": list(selected), "records": records,
            "blocks": blocks, "info": info, "dismissed": dismissed, "triage": triage,
            "details": [list(d) for d in details], "warnings": list(warnings),
            "window": window or (f"{old_msg} → {new_msg}" if old_msg or new_msg else "")}


def mod_fingerprint(mod_root):
    """{relative path: [modified time in ns, size]} for the mod's script, GUI and
    localization files, recorded with a run so the app can tell when the mod
    changed after it."""
    root, files = Path(mod_root), {}
    for top in SCAN_TOPDIRS:
        for fp in (root / top).rglob("*"):
            if fp.suffix not in (".txt", ".gui", ".yml"):
                continue
            rel = fp.relative_to(root)
            if should_skip(rel):
                continue
            try:
                stat = fp.stat()      # one stat per file: on a mounted drive each costs
            except OSError:
                continue
            if S_ISREG(stat.st_mode):
                files[rel.as_posix()] = [stat.st_mtime_ns, stat.st_size]
    return files


def changed_files(payload, mod_root):
    """Files edited, added or removed since the run that wrote payload."""
    before = payload.get("files")
    if before is None:
        return []
    now = mod_fingerprint(mod_root)
    return sorted(path for path in set(before) | set(now) if before.get(path) != now.get(path))


def _finding(rec):
    """A record back into the Finding it came from; its id is unchanged."""
    return Finding(rec["kind"], rec["name"], rec.get("location", ""), rec.get("detail", ""),
                   None, rec.get("key"), rec.get("since") or None, rec.get("base") or None)


def dismiss(mod_root, records, ids, reason, today=None):
    """Dismiss findings from a run's records by id, as --dismiss does. Returns
    (done, errors)."""
    store, err = open_store(mod_root)
    if store is None:
        return [], [err]
    done, errors = ledger.dismiss(store.state, [_finding(r) for r in records], ids,
                                  reason or None, today or datetime.date.today().isoformat())
    if done:
        store.save()
    return done, errors


def undismiss(mod_root, ids):
    """Bring back dismissed findings by id, as --undismiss does. Returns
    (removed, errors)."""
    store, err = open_store(mod_root)
    if store is None:
        return [], [err]
    removed, errors = ledger.undismiss(store.state, ids)
    if removed:
        store.save()
    return removed, errors


def dismissed_entries(mod_root):
    """This mod's dismissals, in --show-dismissed order."""
    store, _err = open_store(mod_root)
    return _dismissed_entries(store) if store else []


def store_views(mod_root):
    """Returns (dismissals, orphaned record files) for the app.

    The Dismissed page and the Tracker page of the app show this data. The function
    reads the findings record again at each call."""
    store, _err = open_store(mod_root)
    if store is None:
        return [], []
    return _dismissed_entries(store), [store.commits_dir / f"{h}.json" for h in store.orphans()]


def _dismissed_entries(store):
    entries = []
    for fid, e in store.state["dismissed"].items():
        kind = e.get("finding", "")
        entries.append({"fid": fid, "id": ledger.short_id(fid), "kind": kind,
                        "label": KIND[kind][2] if kind in KIND else kind,
                        "name": e.get("name", ""), "detail": e.get("detail", ""),
                        "on": e.get("on", ""), "reason": e.get("reason", ""), "gone": bool(e.get("gone"))})
    return sorted(entries, key=lambda e: (e["kind"], e["name"]))


def run_argv(options):
    """The command-line flags for a run the app starts. Every run covers all five
    audits, except that a category applies only to the override and duplicate
    audits, so a run with one covers just those two."""
    argv = ["--overrides", "--dupes"] if options.get("category") else []
    if options.get("full"):
        argv.append("--full")
    for flag in ("old", "new", "block", "category"):
        if options.get(flag):
            argv += [f"--{flag}", options[flag]]
    return argv


def override_targets(mod_root):
    """{category: sorted block names} for every INJECT and REPLACE in the mod. The
    category is the folder below the module root (common/building_types), which
    --category matches in both the override and duplicate audits."""
    targets = {}
    for ov in find_overrides(Path(mod_root)):
        category = ov["category"].split("/", 1)[1] if "/" in ov["category"] else ov["category"]
        targets.setdefault(category, set()).add(ov["block"])
    return {c: sorted(blocks) for c, blocks in sorted(targets.items())}


def gui_targets(mod_root):
    """Sorted names of the GUI templates and types the mod defines, which --block
    matches in the GUI and duplicate audits."""
    from .gui import mod_gui_files, parse_gui_defs
    return sorted({d["name"] for _rel, text in mod_gui_files(Path(mod_root)) for d in parse_gui_defs(text)[0]
                   if d["kind"] in ("template", "type")})


# --- the block view -----------------------------------------------------------

def _row(n, text, ghost):
    return {"n": n, "text": text, "mark": None, "sign": None, "ghost": ghost, "note": "",
            "fid": None, "id": None}


def _indent(line):
    return line[:len(line) - len(line.lstrip())]


def _indent_unit(lines):
    """The copy's indentation step: a tab, or the smallest step its spaces take."""
    tabs = sum(line[:1] == "\t" for line in lines)
    spaced = sorted({len(_indent(line)) for line in lines if line[:1] == " " and line.strip()})
    if tabs >= len([line for line in lines if line[:1] == " "]) or not spaced:
        return "\t"
    steps = [b - a for a, b in zip([0] + spaced, spaced) if b > a]
    return " " * min(steps)


def _depths(text):
    """Each line's depth inside `text`: the blocks open before it, less the ones the
    line closes before anything else."""
    tokens, out, depth, t, pos = tokenize(text), [], 0, 0, 0
    for line in text.split("\n"):
        end, before, closers, leading = pos + len(line), depth, 0, True
        while t < len(tokens) and tokens[t]["start"] <= end:
            tok = tokens[t]
            if tok["type"] == "op" and tok["val"] == "}":
                closers += leading
                depth = max(depth - 1, 0)
            elif tok["type"] != "comment":
                leading = False
                depth += tok["type"] == "op" and tok["val"] == "{"
            t += 1
        out.append(max(before - closers, 0))
        pos = end + 1
    return out


def _cause(kind):
    """What a marked row's change is, where the legend words it apart from its mark:
    'conflict' (vanilla changed a statement you also changed), 'inject' (vanilla
    changed a key you inject), or None."""
    return "conflict" if kind in CONFLICT_KINDS else "inject" if kind == "inject_overlap" else None


def block_rows(block):
    """Rows for the app's view of one target: the copy's lines with their numbers,
    and vanilla's side of each flagged change.

    Each row is {n, text, mark, sign, ghost, note, fid, id}, and a marked row also
    holds its change's `cause` (see _cause). The lines of a flagged
    change's copy statement get its mark ('stale' or 'review') and sign '-'.
    Vanilla's text for it follows as ghost rows (sign '+', n None, the same mark):
    right under your statement's last line when both sides have one, or where it
    belongs when only vanilla has it. Ghost rows are indented like the line they
    follow, one level deeper inside a block, in the copy's own indentation. When
    both sides are one line, the ghost row is your line with vanilla's statement in
    place of yours, so a value inside a one-line block keeps its key, and `emph`
    holds the words that differ. A change inside a statement whose vanilla text is
    already shown is marked but not shown again."""
    lines, first = block["lines"], int(block["line"])
    unit = _indent_unit(lines)
    rows = [_row(first + i, text, False) for i, text in enumerate(lines)]
    under, shown = {}, []

    def ghosts(anchor, base, vanilla, change):
        out = []
        depths = _depths(vanilla)
        for k, text in enumerate(vanilla.split("\n")):
            row = _row(None, base + unit * depths[k] + text.lstrip(), True)
            row.update(mark=change["mark"], sign="+", fid=change["fid"], id=ledger.short_id(change["fid"] or ""),
                       cause=_cause(change.get("kind")))
            out.append(row)
        under.setdefault(anchor, []).extend(out)
        return out

    for c in block["changes"]:
        if not c["mark"]:
            continue
        if c["vanilla"] is not None:
            c = dict(c, vanilla=c["vanilla"].strip())
        at = c["first"] if c["first"] is not None else c["anchor"] + 1
        inside = any(lo <= at <= hi for lo, hi in shown)
        if c["first"] is not None:
            for n in range(c["first"], c["last"] + 1):
                row = rows[n - first]
                row["mark"] = row["mark"] or c["mark"]
                row.setdefault("cause", _cause(c.get("kind")))
                row["sign"] = "-"
                row["fid"] = row["fid"] or c["fid"]
                row["id"] = ledger.short_id(row["fid"] or "")
            if c["vanilla"] is None or inside:
                continue
            shown.append((c["first"], c["last"]))
            if c["first"] == c["last"] and "\n" not in c["vanilla"]:
                text, (start, end) = lines[c["first"] - first], c["cols"]
                ghost = _row(None, text[:start] + c["vanilla"] + text[end:], True)
                ghost.update(mark=c["mark"], sign="+", fid=c["fid"], id=ledger.short_id(c["fid"] or ""),
                             cause=_cause(c.get("kind")))
                under.setdefault(c["last"], []).append(ghost)
                mine, theirs = word_spans(text[start:end], c["vanilla"])
                rows[c["first"] - first]["emph"] = [(a + start, b + start) for a, b in mine]
                ghost["emph"] = [(a + start, b + start) for a, b in theirs]
            else:
                ghosts(c["last"], _indent(lines[c["first"] - first]), c["vanilla"], c)
        elif c["vanilla"] is not None and not inside:
            anchor = c["anchor"]
            follow = lines[anchor - first] if anchor >= first else lines[0] if lines else ""
            ghosts(anchor, _indent(follow) + (unit if c["inside"] else ""), c["vanilla"], c)

    out = [r for r in under.get(first - 1, [])]
    for row in rows:
        out.append(row)
        out.extend(under.get(row["n"], []))
    return out


_DIRECTIVE = re.compile(r"^[A-Z_]+:")


def _line_key(line):
    """A line as the side-by-side view compares it: its tokens without comments,
    layout, number spelling or an override directive such as `REPLACE:`. A blank or
    comment-only line reads as ''."""
    tokens = [t["val"] for t in tokenize(line) if t["type"] != "comment"]
    if tokens:
        tokens[0] = _DIRECTIVE.sub("", tokens[0])
    return " ".join(norm_value(t) for t in tokens if t)


def _line_keys(lines):
    """_line_key of each line, where a line that only closes blocks also holds its depth,
    so a diff never trades the brace that closes one block for another block's."""
    keys = []
    for line, depth in zip(lines, _depths("\n".join(lines))):
        key = _line_key(line)
        keys.append(f"{key} @{depth}" if key and not key.replace("}", "").strip() else key)
    return keys


def _closes(key):
    return bool(key) and not key.split(" @")[0].replace("}", "").strip()


def _slide(ops, base, side):
    """Slide each run of only deleted or only added lines down while it starts with a
    line that only closes blocks and the line after the run is that same line, so a
    removed block ends with its own closing brace rather than the one before it."""
    k = 0
    while k < len(ops):
        kind = ops[k][0]
        end = k
        while end < len(ops) and ops[end][0] == kind:
            end += 1
        if kind in ("del", "add") and end < len(ops) and ops[end][0] == "same":
            _tag, i_next, j_next = ops[end]
            keys, first, following = ((base, ops[k][1], i_next) if kind == "del" else (side, ops[k][2], j_next))
            if _closes(keys[first]) and keys[first] == keys[following]:
                shifted = [(kind, i, None) for i in range(first + 1, following + 1)] if kind == "del" else \
                          [(kind, None, j) for j in range(first + 1, following + 1)]
                ops[k:end + 1] = ([("same", first, j_next)] if kind == "del" else [("same", i_next, first)]) + shifted
                continue
        k = max(end, k + 1)


def _partners(pending, dels, base, side):
    """Which base line each added line replaces, for one run of deletions and additions.

    Blank and comment-only lines are left out, because two copies of one block lay it
    out differently, and a blank line replaces nothing. What is left pairs in order when
    the two counts agree. Otherwise each added line pairs with a deleted line that sets
    the same key, so a run that adds one line and changes another still pairs the one it
    changed."""
    adds = [j for j in pending if side[j]]
    drops = [i for i in dels if base[i]]
    if len(adds) == len(drops):
        return dict(zip(adds, drops))
    out, taken = {}, set()
    for j in adds:
        key = side[j].split(" ", 1)[0]
        hit = next((i for i in drops if i not in taken and base[i].split(" ", 1)[0] == key), None)
        if hit is not None:
            out[j] = hit
            taken.add(hit)
    return out


def _diff(base, side):
    """(same, deleted, inserted, partner) from compared lines `base` to `side`: same maps
    a base index to the side index that keeps it; deleted holds base indexes the side
    dropped; inserted maps a base index to the side indexes added just before it (the
    end for len(base)); partner maps a side index to the one base index it replaces.
    Runs of deleted or added lines are slid onto block boundaries (_slide)."""
    ops = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, base, side, autojunk=False).get_opcodes():
        if tag == "equal":
            ops += [("same", i, j) for i, j in zip(range(i1, i2), range(j1, j2))]
        else:
            ops += [("del", i, None) for i in range(i1, i2)] + [("add", None, j) for j in range(j1, j2)]
    _slide(ops, base, side)
    same, deleted, inserted, partner = {}, set(), {}, {}
    pending, dels = [], []
    for kind, i, j in ops:
        if kind == "add":
            pending.append(j)
            continue
        if pending:
            partner.update(_partners(pending, dels, base, side))
            inserted.setdefault(i, []).extend(pending)
            pending, dels = [], []
        if kind == "same":
            same[i] = j
            dels = []
        else:
            deleted.add(i)
            dels.append(i)
    if pending:
        partner.update(_partners(pending, dels, base, side))
        inserted.setdefault(len(base), []).extend(pending)
    return same, deleted, inserted, partner


def inject_side_rows(block):
    """Rows for the side-by-side view of an INJECT: vanilla's current statements for the
    keys you inject on the left, yours on the right, one key after another.

    An INJECT adds statements to vanilla's block, so neither side deleted anything and
    no line is an addition: every line is current text, and only the keys you inject are
    shown, since the rest of vanilla's block is not an injection point. A key written
    more than once on either side brings all of its statements. Rows carry the same
    shape as side_rows, so the block view draws them the same way; `state` is always
    'same' and the finding's mark carries the severity."""
    rows = []
    for pair in block.get("pairs") or ():
        left, right = pair["vanilla"], pair["yours"]
        one_each = len(left) == 1 and len(right) == 1
        for i in range(max(len(left), len(right))):
            cells = []
            for side in (left, right):
                if i >= len(side):
                    cells.append(None)
                    continue
                n, text = side[i]
                cells.append({"n": n, "text": text, "state": "same", "quiet": not text.split("#")[0].strip(),
                              "emph": None})
            if one_each and cells[0] and cells[1]:
                spans = word_spans(cells[1]["text"], cells[0]["text"])
                cells[0]["emph"], cells[1]["emph"] = spans[1], spans[0]
            rows.append({"left": cells[0], "right": cells[1], "mark": pair["mark"],
                         "fid": pair["fid"], "id": ledger.short_id(pair["fid"]),
                         "cause": "inject", "lead": i == 0})
    return rows


def side_rows(block):
    """Rows for the side-by-side view of one target: vanilla's current text on the left
    and the copy on the right, each compared with vanilla's text at the version the copy
    matches (block["base_lines"]), the way a three-way merge shows two edits of one base.
    A line both sides keep from that version shares a row.

    Each row is {left, right, mark, fid, id, cause, lead}. A side is None where only the other
    side has a line there, or {n, text, state, quiet, emph}: `state` is 'same' for a
    line kept from the base, 'add' for a line the side added, and 'del' for a base line
    the side deleted, shown with the base's text and n None; n counts vanilla's lines
    from its block's first line and the copy's from its first line in the file; `quiet`
    marks a blank or comment-only line, which is never coloured; `emph` holds the words
    that differ between an added line and the one base line it replaces. Lines compare
    by _line_key. The row's `fid` and `cause` come from a flagged change on a line either
    side shows, and its `mark` is that finding's strongest mark across all its changes.
    Blank and comment-only rows between two rows of one finding belong to it, and `lead`
    is true on the first row of each run of rows one finding covers."""
    first = int(block["line"])
    texts = {"left": block["vanilla_lines"], "right": block["lines"]}
    base = block.get("base_lines") or texts["left"]
    base_keys = _line_keys(base)
    keys = {side: _line_keys(lines) for side, lines in texts.items()}
    diffs = {side: _diff(base_keys, keys[side]) for side in texts}
    replaced = {side: {i: j for j, i in diffs[side][3].items()} for side in texts}
    number = {"left": lambda j: j + 1, "right": lambda j: first + j}
    flagged = {"left": {}, "right": {}}
    for c in block["changes"]:
        for side, lo, hi in (("left", c.get("vfirst"), c.get("vlast")), ("right", c["first"], c["last"])):
            if c["mark"] and lo is not None:
                for n in range(lo, hi + 1):
                    flagged[side].setdefault(n, c)

    def kept_or_added(side, j, state):
        text, i = texts[side][j], diffs[side][3].get(j)
        emph = word_spans(text, base[i])[0] if state == "add" and i is not None else None
        return {"n": number[side](j), "text": text, "state": state, "quiet": not keys[side][j], "emph": emph}

    def deleted(side, i):
        j = replaced[side].get(i)
        emph = word_spans(texts[side][j], base[i])[1] if j is not None else None
        return {"n": None, "text": base[i], "state": "del", "quiet": not base_keys[i], "emph": emph}

    rows = []

    def row(left, right):
        hit = next((flagged[side][c["n"]] for side, c in (("left", left), ("right", right))
                    if c is not None and c["n"] in flagged[side]), None)
        rows.append({"left": left, "right": right, "mark": hit["mark"] if hit else None,
                     "fid": hit["fid"] if hit else None, "id": ledger.short_id(hit["fid"] or "") if hit else None,
                     "cause": _cause(hit["kind"]) if hit else None})

    # An added line belongs beside the base line it replaces, so the two sides put their
    # replacements of one base line on one row. Each side is compared with the base on
    # its own, and one side can keep a blank line the other drops, which puts the same
    # replacement at a different place in the base. Its partner is the same on both
    # sides, so the partner and not that place decides where the line goes.
    anchored = {}
    for side in texts:
        at = {}
        for i, js in sorted(diffs[side][2].items()):
            for j in js:
                p = diffs[side][3].get(j)
                at.setdefault(i if p is None else p + 1, []).append(j)
        anchored[side] = at

    def added_rows(i):
        """The rows for the lines the sides add at base line `i`, each side in the order
        of its own file. Two lines that replace the same base line share a row. A line
        that replaces nothing, which is blank or comment only, waits for no other line."""
        here = {side: anchored[side].get(i, []) for side in texts}
        at = dict.fromkeys(texts, 0)
        while any(at[side] < len(here[side]) for side in texts):
            head = {s: here[s][at[s]] if at[s] < len(here[s]) else None for s in texts}
            p = {s: diffs[s][3].get(head[s]) if head[s] is not None else None for s in texts}
            take = {s: head[s] is not None for s in texts}
            for side, other in (("left", "right"), ("right", "left")):
                if head[side] is None or p[side] is None or head[other] is None:
                    continue
                if p[other] is None:
                    take[side] = False                      # the other line replaces nothing
                elif p[side] != p[other]:
                    take[side] = p[side] < p[other]         # the earlier base line first
            row(*(kept_or_added(side, head[side], "add") if take[side] else None
                  for side in ("left", "right")))
            for side in texts:
                at[side] += take[side]

    for i in range(len(base) + 1):
        added_rows(i)
        if i < len(base):
            row(*(kept_or_added(side, diffs[side][0][i], "same") if i in diffs[side][0] else deleted(side, i)
                  for side in ("left", "right")))
    strongest = {}
    for c in block["changes"]:
        if c["fid"] and c["mark"]:
            strongest[c["fid"]] = "stale" if "stale" in (c["mark"], strongest.get(c["fid"])) else c["mark"]
    # A run of rows that no finding claims, between two rows of one finding, belongs to
    # it: a blank line, or a line only your copy changed, inside the block the finding
    # covers. The view draws one outline around each run of a finding's rows, so leaving
    # these out breaks one block into several boxes. It joins them when the view shows
    # all of them anyway (hidden_span), so the outline costs no lines and follows what
    # you can see. A run the view folds still divides the finding, and so does a run of
    # blank lines, which needs no outline of its own.
    quiet = lambda r: all(c is None or c["quiet"] for c in (r["left"], r["right"]))
    k = 0
    while k < len(rows):
        end = k
        while end < len(rows) and rows[end]["fid"] is None:
            end += 1
        joins = hidden_span(end - k, True, True) is None or all(quiet(r) for r in rows[k:end])
        if (end > k and k > 0 and end < len(rows) and joins
                and rows[k - 1]["fid"] and rows[k - 1]["fid"] == rows[end]["fid"]):
            for r in rows[k:end]:
                r.update(fid=rows[end]["fid"], id=rows[end]["id"], cause=rows[end]["cause"])
        k = max(end, k + 1)
    for k, r in enumerate(rows):
        if r["fid"]:
            r["mark"] = strongest.get(r["fid"], r["mark"])
        r["lead"] = bool(r["fid"]) and (k == 0 or rows[k - 1]["fid"] != r["fid"])
    return rows


_CLOSER = re.compile(r"^}(\s*})*\s*$")


def flatten_rows(rows):
    """rows for reading deeply nested text: each line's leading whitespace dropped, its
    emphasis moved along, and each run of adjacent lines that only close blocks joined
    into one row that keeps its first line's number. Side-by-side rows join only where
    both sides close blocks or are blank alike, with the same states and finding."""
    def stripped(cell):
        if not cell:
            return cell
        cut = len(cell["text"]) - len(cell["text"].lstrip())
        out = dict(cell, text=cell["text"][cut:])
        if cell.get("emph"):
            out["emph"] = [(max(s - cut, 0), max(e - cut, 0)) for s, e in cell["emph"]]
        return out

    def closer(r):
        cells = [r["left"], r["right"]] if "left" in r else [r]
        return any(cells) and all(c is None or _CLOSER.match(c["text"]) for c in cells)

    def shape(r):
        if "left" in r:
            return ("side", r["fid"], *(c["state"] if c else None for c in (r["left"], r["right"])))
        return ("line", r.get("fid"), r.get("mark"), r.get("sign"), r.get("ghost"))

    out = []
    for r in rows:
        if "left" in r:
            r = dict(r, left=stripped(r["left"]), right=stripped(r["right"]))
        elif "text" in r:
            r = stripped(r)
        else:
            out.append(r)
            continue
        prev = out[-1] if out else None
        if (prev is not None and ("left" in prev or "text" in prev) and closer(prev) and closer(r)
                and shape(prev) == shape(r)):
            if "left" in r:
                out[-1] = dict(prev, **{side: dict(prev[side], text=f"{prev[side]['text']} {r[side]['text']}")
                                        if prev[side] else None for side in ("left", "right")})
            else:
                out[-1] = dict(prev, text=f"{prev['text']} {r['text']}")
        else:
            out.append(r)
    return out


# A stretch of unmarked rows shows `FOLD_KEEP` of them beside a marked row, and folds
# only when `FOLD_MIN_HIDDEN` of them are left over. `hidden_span` is the one place that
# applies this, so what the view shows and what a finding covers cannot disagree.
FOLD_KEEP, FOLD_MIN_HIDDEN = 3, 6


def hidden_span(length, after_mark, before_mark, keep=FOLD_KEEP, min_hidden=FOLD_MIN_HIDDEN):
    """(first, last) of the rows a stretch of `length` unmarked rows folds away, or None
    when the whole stretch stays on screen. `after_mark` and `before_mark` say whether a
    marked row sits on that side of it, which is where the context lines are kept."""
    first = keep if after_mark else 0
    last = length - (keep if before_mark else 0)
    return (first, last) if last - first >= min_hidden else None


def _fold_label(rows):
    """What a fold hides: its lines, and how many side-by-side rows among them add or
    delete something."""
    changed = sum(1 for r in rows if any(c and c["state"] != "same" and not c["quiet"]
                                         for c in (r.get("left"), r.get("right"))))
    return (f"{len(rows)} lines, {changed} with changes outside the findings" if changed
            else f"{len(rows)} unchanged lines")


def fold_rows(rows, keep=FOLD_KEEP, min_hidden=FOLD_MIN_HIDDEN):
    """rows with each long stretch of unmarked lines folded into one
    {"fold": [hidden rows], "label": what it hides} row. The `keep` lines next to a
    marked line stay visible as context; a stretch that reaches the start or end of
    the block folds all the way to it."""
    out, run = [], []

    def flush(after_mark, before_mark):
        span = hidden_span(len(run), after_mark, before_mark, keep, min_hidden)
        if span is None:
            out.extend(run)
        else:
            first, last = span
            out.extend(run[:first])
            out.append({"fold": run[first:last], "label": _fold_label(run[first:last])})
            out.extend(run[last:])
        run.clear()

    seen_mark = False
    for r in rows:
        if r.get("mark") or r.get("ghost") or "toggle" in r:
            flush(seen_mark, True)
            out.append(r)
            seen_mark = True
        else:
            run.append(r)
    flush(seen_mark, False)
    return out


_GUI_SIGN = {"changed": "-", "removed": "-", "new": "+", "added": "+"}


def gui_rows(rec):
    """Rows for the app's block view of a GUI finding: the copy's own lines with
    their indentation and line numbers, and vanilla's lines the copy lacks as ghost
    rows where they belong. A line vanilla changed or deleted is signed '-', one of
    vanilla's '+', and a line lined up with one on the other side carries the words
    that differ (see gui._gui_patch)."""
    start, n, rows = int(rec.get("line") or 1), 0, []
    mark = "review" if rec.get("sev") == "review" else "stale"
    for p in (rec.get("data") or {}).get("patch") or []:
        ghost = p["c"] in ("new", "added")
        row = _row(None if ghost else start + n, p["t"], ghost)
        if p["c"] in _GUI_SIGN:
            row["mark"], row["sign"] = mark, _GUI_SIGN[p["c"]]
            if p.get("e"):
                row["emph"] = [tuple(span) for span in p["e"]]
        rows.append(row)
        if not ghost:
            n += 1
    return rows


def source_rows(rec, mod_root, expanded=True):
    """One toggle per place a duplicate is defined, open when `expanded`, each holding
    that definition's lines read from the mod. A definition no longer at its recorded
    line (the file changed since the run) holds a note instead."""
    name = rec["name"].split(".")[-1]
    files, out = {}, []
    for s in (rec.get("data") or {}).get("sources") or []:
        rel, line = s["file"], s["line"]
        if rel not in files:
            try:
                files[rel] = (Path(mod_root) / rel).read_text(encoding="utf-8-sig").split("\n")
            except OSError:
                files[rel] = []
        lines = files[rel]
        head = lines[line - 1].split("#")[0] if 0 < line <= len(lines) else ""
        blank = {"mark": None, "ghost": False, "fid": None, "id": None}
        if name not in head:
            code = [dict(blank, n=None, text="",
                         note="This definition is no longer at this line; run the audits again to see it.")]
        else:
            text = _brace_extract(lines, line - 1) if "{" in head else None
            code = [dict(blank, n=line + i, text=t, note="")
                    for i, t in enumerate((text or lines[line - 1]).split("\n"))]
        out.append({"toggle": f"{s['how']} · {rel}:{line}", "open": expanded, "rows": code})
    return out
