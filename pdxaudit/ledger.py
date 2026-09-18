"""Findings ledger: content fingerprints, dismissals, and open-finding tracking.

A finding's id is the SHA-1 of its kind plus its `key`: the target and, for line
findings, the key path and the vanilla and mod values. Line numbers, detail text
and tracker commit hashes never enter it, so the id is identical for anyone on
the same game version and survives unrelated edits. A dismissed finding comes
back on its own when vanilla's value or the mod's value changes, because the id
no longer matches."""
import hashlib
import json

from .report import KIND, SEV_INFO, finding_severity

SHORT_ID = 8

# A kind's audit tag or a target's prefix, as the audit's command-line name.
_AUDITS = {"override": "overrides", "guifile": "gui", "dupesfile": "dupes"}

# Duplicate definitions must be fixed, never dismissed.
NOT_DISMISSIBLE = frozenset({
    "dupes_multiple_sources", "dupes_define_key", "dupes_gui_definition",
    "dupes_loc_key", "dupes_loc_key_same", "dupes_on_action_syntax", "dupes_on_action_key",
})


def empty_state():
    return {"dismissed": {}, "open": {}}


def target_of(f):
    return (f.key or {}).get("target") or f"{f.kind}:{f.name}"


# Key fields the id ignores: `was`, vanilla's statement before a conflicting change,
# follows from vanilla's history and the rest of the key.
_NOT_ID = ("was",)


def fingerprint_payload(f):
    key = f.key if f.key is not None else {"target": f"{f.kind}:{f.name}",
                                           "detail": f.detail or ""}
    if any(k in key for k in _NOT_ID):
        key = {k: v for k, v in key.items() if k not in _NOT_ID}
    return {"kind": f.kind, "key": key}


def finding_id(f):
    payload = json.dumps(fingerprint_payload(f), sort_keys=True,
                         ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def short_id(fid):
    return fid[:SHORT_ID]


# The key fields distinct() adds to findings whose content matches another's.
_PLACE = ("file", "occurrence")


def _content(f):
    """`f` without the key fields distinct() adds."""
    if f.key and any(k in f.key for k in _PLACE):
        return f._replace(key={k: v for k, v in f.key.items() if k not in _PLACE})
    return f


def _file_line(f):
    file, sep, line = (f.location or "").rpartition(":")
    return (file, int(line)) if sep and line.isdigit() else (f.location or "", 0)


def distinct(findings):
    """`findings` with no id shared. Findings whose content matches, such as the same
    change in two copies of a block, add their `file` to the key, and those in one file
    also add their `occurrence` there, counting from 1 in line order. A finding no other
    matches keeps its content id. A block view's change rows follow their finding's id."""
    out = [_content(f) for f in findings]
    groups = {}
    for i, f in enumerate(out):
        groups.setdefault(finding_id(f), []).append(i)
    for members in groups.values():
        if len(members) < 2:
            continue
        by_file = {}
        for i in members:
            by_file.setdefault(_file_line(out[i])[0], []).append(i)
        for file, same in by_file.items():
            same.sort(key=lambda i: _file_line(out[i])[1])
            for n, i in enumerate(same, 1):
                key = dict(out[i].key if out[i].key is not None else fingerprint_payload(out[i])["key"],
                           file=file)
                if len(same) > 1:
                    key["occurrence"] = n
                out[i] = out[i]._replace(key=key)

    renames = {}
    for f, g in zip(findings, out):
        if f.data and "changes" in f.data:
            renames.setdefault(id(f.data), {})[finding_id(f)] = finding_id(g)
    views = {}
    for i, f in enumerate(findings):
        names = renames.get(id(f.data)) if f.data else None
        if names and any(old != new for old, new in names.items()):
            if id(f.data) not in views:
                views[id(f.data)] = dict(f.data, changes=[
                    dict(c, fid=names[c["fid"]]) if c.get("fid") in names else c for c in f.data["changes"]])
            out[i] = out[i]._replace(data=views[id(f.data)])
    return out


def is_actionable(f):
    return finding_severity(f) != SEV_INFO


def is_dismissible(f):
    return is_actionable(f) and f.kind not in NOT_DISMISSIBLE


def split_dismissed(findings, state):
    """(visible findings, number of actionable findings hidden by a dismissal)."""
    dismissed = (state or {}).get("dismissed", {})
    visible, hidden = [], 0
    for f in findings:
        if is_dismissible(f) and finding_id(f) in dismissed:
            hidden += 1
        else:
            visible.append(f)
    return visible, hidden


def _resolve(prefix, ids):
    p = (prefix or "").strip().lower()
    hits = [i for i in ids if p and i.startswith(p)]
    return p, hits


def dismiss(state, findings, prefixes, reason, today):
    """Dismiss current findings by id prefix. Returns (done, errors): done is a
    list of (full_id, finding); errors are messages for ids that matched
    nothing, matched several findings, or name a finding that cannot be
    dismissed."""
    by_id = {}
    for f in findings:
        if is_actionable(f):
            by_id.setdefault(finding_id(f), f)
    done, errors = [], []
    for prefix in prefixes:
        p, hits = _resolve(prefix, by_id)
        if not hits:
            errors.append(f"no current finding has id {p}")
            continue
        if len(hits) > 1:
            errors.append(f"id {p} is ambiguous ({len(hits)} findings match); "
                          f"use more characters")
            continue
        fid, f = hits[0], by_id[hits[0]]
        if f.kind in NOT_DISMISSIBLE:
            errors.append(f"[{short_id(fid)}] {f.name}: duplicate definitions "
                          f"cannot be dismissed; fix the duplicate instead")
            continue
        entry = {"finding": f.kind, "name": f.name, "target": target_of(f),
                 "detail": f.detail or "", "on": today}
        if reason:
            entry["reason"] = reason
        state["dismissed"][fid] = entry
        done.append((fid, f))
    return done, errors


def undismiss(state, prefixes):
    """Remove dismissals by id prefix. Returns (removed, errors); removed is a
    list of (full_id, entry)."""
    removed, errors = [], []
    for prefix in prefixes:
        p, hits = _resolve(prefix, state["dismissed"])
        if not hits:
            errors.append(f"no dismissed finding has id {p}")
            continue
        if len(hits) > 1:
            errors.append(f"id {p} is ambiguous ({len(hits)} dismissals match); "
                          f"use more characters")
            continue
        removed.append((hits[0], state["dismissed"].pop(hits[0])))
    return removed, errors


def audit_of(entry):
    """Returns the name of the audit that makes a record entry. The name has the
    form of the --overrides option."""
    audit = KIND[entry["finding"]][1] if entry.get("finding") in KIND else None
    return _AUDITS.get(audit, audit)


def update_open(state, findings, new_tag, covers=None, produced=None):
    """Replaces the open findings that this run examined with its actionable findings.

    `covers(entry)` tells the function if the run examined the audit of an entry. By
    default, the run examines each entry. The function does not change an entry that
    the run did not examine. A finding that is already open keeps the `since` value
    and the `base` value from its first appearance. The function removes a finding
    that the run no longer makes.

    `produced` contains the id of each finding that the run made, and includes the
    dismissed findings. The function marks a dismissal `gone` if the run examined it
    but did not make it."""
    covers = covers or (lambda _entry: True)
    prev = state.get("open", {})
    fresh = {fid: e for fid, e in prev.items() if not covers(e)}
    if produced is not None:
        for fid, e in state.get("dismissed", {}).items():
            if fid in produced:
                e.pop("gone", None)
            elif covers(e):
                e["gone"] = True
    for f in findings:
        if not is_actionable(f):
            continue
        fid = finding_id(f)
        old = prev.get(fid) or {}
        entry = {"finding": f.kind, "name": f.name, "target": target_of(f),
                 "detail": f.detail or "",
                 "since": old.get("since") or f.since or new_tag}
        base = old.get("base") or f.base
        if base:
            entry["base"] = base
        fresh[fid] = entry
    state["open"] = fresh


# The kinds whose open findings carry their base into later runs: the checks that
# compare a window of versions rather than a copy's whole history.
CARRIED = frozenset({"override_inject_overlap", "loc_changed"})


def bases_from_state(state, order):
    """Returns the version tag from which to measure each target.

    For each target, the tag is the oldest base of an open finding of a CARRIED kind.
    A check only makes its window larger, and only from its own earlier findings. `order` lists the version tags of the tracker, oldest first. The result
    maps a target to a tag. The function ignores a tag that is not in `order`."""
    positions = {t: i for i, t in enumerate(order)}
    bases = {}
    for entry in state.get("open", {}).values():
        if entry.get("finding") not in CARRIED:
            continue
        t, b = entry.get("target"), entry.get("base")
        if not t or b not in positions:
            continue
        if t not in bases or positions[b] < positions[bases[t]]:
            bases[t] = b
    return bases
