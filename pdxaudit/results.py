"""What the --display app shows: a run's findings as records, and the record
actions the app takes without a new run (dismiss, restore, orphaned records).

A run started from the app calls the command line with --results-file, which
writes build_payload's dictionary as JSON. Records keep each finding's kind and
key, so a dismissal made from a record has the same id as one made with
--dismiss."""
import datetime
import hashlib
from pathlib import Path
from stat import S_ISREG

from pdx_utilities.constants import SCAN_TOPDIRS
from pdx_utilities.script_parser import tokenize

from . import ledger
from .config import should_skip
from .overrides import _brace_extract, find_overrides
from .report import KIND, SEV_INFO, Finding, _is_value_finding, change_kind, line_label, value_pair
from .store import open_store
from .worddiff import word_spans


def _split_location(loc):
    loc = loc or ""
    if ":" in loc and loc.rsplit(":", 1)[1].isdigit():
        file, line = loc.rsplit(":", 1)
        return file, line
    return loc or "?", ""


def build_payload(findings, *, mod_name="", old_msg="", new_msg="", new_tag="",
                  selected=(), dismissed=0, triage="", details=(), warnings=()):
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
                change_kind(f.kind), f.key["yours"], f.key["vanilla"], f.since)
        records.append(rec)

    return {"mod": mod_name, "old": old_msg or "", "new": new_msg or "",
            "new_tag": new_tag or "", "selected": list(selected), "records": records,
            "blocks": blocks, "info": info, "dismissed": dismissed, "triage": triage,
            "details": [list(d) for d in details], "warnings": list(warnings)}


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
    """(dismissals, orphaned record files) for the app's Dismissed and Tracker
    pages, read through one opening of the findings record."""
    store, _err = open_store(mod_root)
    if store is None:
        return [], []
    return (_dismissed_entries(store),
            [store.commits_dir / f"{h}.json" for h in store.orphans()])


def _dismissed_entries(store):
    entries = []
    for fid, e in store.state["dismissed"].items():
        kind = e.get("finding", "")
        entries.append({"fid": fid, "id": ledger.short_id(fid), "kind": kind,
                        "label": KIND[kind][2] if kind in KIND else kind,
                        "name": e.get("name", ""), "detail": e.get("detail", ""),
                        "on": e.get("on", ""), "reason": e.get("reason", "")})
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


def block_rows(block):
    """Rows for the app's view of one target: the copy's lines with their numbers,
    and vanilla's side of each flagged change.

    Each row is {n, text, mark, sign, ghost, note, fid, id}. The lines of a flagged
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
            row.update(mark=change["mark"], sign="+", fid=change["fid"], id=ledger.short_id(change["fid"] or ""))
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
                row["sign"] = "-"
                row["fid"] = row["fid"] or c["fid"]
                row["id"] = ledger.short_id(row["fid"] or "")
            if c["vanilla"] is None or inside:
                continue
            shown.append((c["first"], c["last"]))
            if c["first"] == c["last"] and "\n" not in c["vanilla"]:
                text, (start, end) = lines[c["first"] - first], c["cols"]
                ghost = _row(None, text[:start] + c["vanilla"] + text[end:], True)
                ghost.update(mark=c["mark"], sign="+", fid=c["fid"], id=ledger.short_id(c["fid"] or ""))
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


def fold_rows(rows, keep=3, min_hidden=6):
    """rows with each long stretch of unmarked lines folded into one
    {"fold": [hidden rows]} row. The `keep` lines next to a marked line stay
    visible as context; a stretch that reaches the start or end of the block
    folds all the way to it."""
    out, run = [], []

    def flush(after_mark, before_mark):
        lead = keep if after_mark else 0
        tail = keep if before_mark else 0
        hidden = run[lead:len(run) - tail]
        if len(hidden) >= min_hidden:
            out.extend(run[:lead])
            out.append({"fold": hidden})
            out.extend(run[len(run) - tail:])
        else:
            out.extend(run)
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
