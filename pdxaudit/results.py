"""What the --display app shows: a run's findings as records, and the record
actions the app takes without a new run (dismiss, restore, orphaned records).

A run started from the app calls the command line with --results-file, which
writes build_payload's dictionary as JSON. Records keep each finding's kind and
key, so a dismissal made from a record has the same id as one made with
--dismiss."""
import datetime
import hashlib
import re
from pathlib import Path
from stat import S_ISREG

from pdx_utilities.constants import SCAN_TOPDIRS

from . import ledger
from .config import should_skip
from .overrides import _brace_extract, _enclosing_paths, _norm, find_overrides
from .report import KIND, LINE_KINDS, SEV_INFO, Finding, _is_value_finding, line_label, value_pair
from .store import open_store
from .threeway import norm_value
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
    findings; informational ones are counted, not listed. A REPLACE or INJECT
    block's rich view is stored once under `blocks`, and every line finding of
    that block points at it."""
    blocks, block_of = {}, {}
    for f in findings:
        sev, audit = KIND[f.kind][:2]
        if f.data and audit == "override" and sev != SEV_INFO:
            bid = hashlib.sha1(f"{f.location}\n{f.name}".encode("utf-8")).hexdigest()[:12]
            blocks[bid] = f.data
            block_of[(f.location, f.name)] = bid

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
        if audit == "override" and (f.location, f.name) in block_of:
            rec["block"] = block_of[(f.location, f.name)]
        elif f.data:
            rec["data"] = f.data
        if _is_value_finding(f):
            k = f.key
            rec["path"] = line_label(k)
            rec["yours"], rec["vanilla"] = value_pair(
                LINE_KINDS[f.kind], k.get("slot"), k.get("old"), k.get("new"),
                k.get("mod"), k.get("ops"), k.get("text"), f.since)
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

_STATEMENT = re.compile(r"^\s*([^\s=<>!?#{}]+)\s*(\?=|<=|>=|!=|==|=|<|>)\s*(.*?)\s*$")


def _statement(key, value):
    slot = key.get("slot")
    return str(value) if slot == "@item" else f"{slot} {key.get('op') or '='} {value}"


def _line_holds(text, key, value):
    """Whether a script line is the statement `slot = value` (or the list member
    `value`), comparing values the way the three-way classification does."""
    if value is None:
        return False
    code = text.split("#")[0].strip()
    if not code:
        return False
    if key.get("slot") == "@item":
        return norm_value(code) == norm_value(value)
    m = _STATEMENT.match(code)
    return bool(m) and m.group(1) == key.get("slot") and norm_value(m.group(3)) == norm_value(value)


def _row(n, text, ghost):
    return {"n": n, "text": text, "mark": None, "sign": None, "ghost": ghost, "note": "",
            "fid": None, "id": None}


def block_rows(payload, rec):
    """(rows, footer) for the app's view of rec's REPLACE or INJECT block.

    rows are the lines of the mod's block in order, each {n, text, mark, sign,
    ghost, note, fid, id}. A line a finding is about gets mark 'stale' or 'review'
    and a sign: '-' for your line that vanilla changed or deleted, '+' for vanilla's
    line your block lacks (a ghost row, n None, where it belongs), '~' for a line to
    look at for the reason its note gives. A value vanilla changed shows your line
    with vanilla's right under it, the words that differ in each row's `emph`.
    Notes carry only what the lines cannot show, and name the patch only when it
    differs from rec's. footer is [(heading, lines)] for changes the block has no
    line for."""
    data = payload["blocks"][rec["block"]]
    start = int(rec.get("line") or 1)
    patch = data.get("patch") or []
    paths = _enclosing_paths([p["t"] for p in patch if p["c"] != "add"])
    rows, n = [], 0
    for p in patch:
        ghost = p["c"] == "add"
        row = _row(None if ghost else start + n, p["t"], ghost)
        if not ghost:
            row["path"] = tuple(seg.split(" ")[0] for seg in paths[n][1:])
            n += 1
        rows.append(row)

    def find(key, value, ghost):
        path = tuple(seg.split(" ")[0] for seg in key.get("path") or [])
        hits = [r for r in rows if r["ghost"] == ghost and not r["mark"]
                and _line_holds(r["text"], key, value)]
        exact = [r for r in hits if ghost or r.get("path") == path]
        return (exact or hits or [None])[0]

    unplaced = []
    for r in payload["records"]:
        if r.get("block") != rec["block"]:
            continue
        k = r.get("key") or {}
        cls = LINE_KINDS.get(r["kind"])
        since = r.get("since") or ""
        when = f" in {since}" if since and since != rec.get("since") else ""
        old, new, mod = k.get("old"), k.get("new"), k.get("mod")
        # detail describes the change in full for the footer when no line holds it.
        row, mark, sign, note, pair, shadow = None, "stale", "-", "", False, None
        if cls == "frozen":
            row = find(k, mod if mod is not None else old, False)
            detail = f"vanilla changed it to {new}{when}"
            shadow = find(k, new, True)
            if shadow is not None:
                rows.remove(shadow)
            pair, note = True, when and f"changed{when}"
        elif cls == "new_line":
            sign = "+"
            row = find(k, new, True)
            if row is None:
                row = _row(None, "\t" + (k.get("text") or _statement(k, new)), True)
                rows.insert(max(len(rows) - 1, 0), row)
            detail = f"vanilla added this line{when}; your block lacks it"
            note = when and f"added{when}"
        elif cls == "kept_removed":
            row = find(k, mod if mod is not None else old, False)
            detail = f"vanilla deleted this line{when}"
            note = when and f"deleted{when}"
        elif cls == "both_changed":
            mark = "review"
            row = find(k, mod, False)
            if old is not None and new is not None:
                detail = f"vanilla changed it from {old} to {new}{when}; yours is {mod}"
                pair, note = True, f"vanilla had {old} before" + (when and f", changed{when}")
            elif new is not None:
                detail = f"vanilla added {_statement(k, new)}{when}; yours is {mod}"
                pair, note = True, when and f"added{when}"
            else:
                detail = note = f"vanilla deleted this key{when} (it was {old}); you changed it to {mod}"
        elif cls == "unclassified":
            mark, sign = "review", "~"
            row = rows[0] if rows else None
            detail = note = f"vanilla changed this block{when} in a way that could not be matched line by line"
        elif r["kind"] == "override_inject_overlap":
            mark, sign = "review", "~"
            keys = [o["key"] for o in data.get("overlap") or []]
            row = next((x for x in rows if not x["ghost"] and not x["mark"] and x.get("path") == ()
                        and (_STATEMENT.match(x["text"].split("#")[0]) or [None, None])[1] in keys), None)
            detail = note = f"vanilla also changed {', '.join(keys)}{when}"
        else:
            continue
        if row is None:
            unplaced.append(f"{line_label(k) or r['name']}: {detail}")
            continue
        row["mark"], row["sign"] = mark, row["sign"] or sign
        row["fid"], row["id"] = row["fid"] or r["fid"], row["id"] or r["id"]
        noted = row
        if pair:
            indent = re.match(r"\s*", row["text"]).group(0)
            vanilla = shadow or _row(None, indent + _statement(k, new), True)
            vanilla["mark"], vanilla["sign"] = mark, "+"
            rows.insert(rows.index(row) + 1, vanilla)
            row["emph"], vanilla["emph"] = word_spans(row["text"], vanilla["text"])
            noted = vanilla      # under the pair, so your line and vanilla's stay together
        if note:
            noted["note"] = f"{noted['note']}; {note}" if noted["note"] else note

    footer = []
    if unplaced:
        footer.append(("Vanilla changes with no matching line in your block", unplaced))
    if data.get("absent"):
        footer.append(("Vanilla changed parts your block doesn't have",
                       [a["line"] for a in data["absent"]]))
    if data.get("removed_note"):
        footer.append(("Vanilla also removed these; your block never had them",
                       list(data["removed_note"])))
    return rows, footer


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


def source_rows(rec, mod_root):
    """One closed toggle per place a duplicate is defined, each holding that
    definition's lines read from the mod. A definition no longer at its recorded
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
        out.append({"toggle": f"{s['how']} · {rel}:{line}", "open": False, "rows": code})
    return out
