"""Findings ledger: content fingerprints, dismissals, and open-finding tracking.

A finding's id is the SHA-1 of its kind plus its `key`: the target and, for line
findings, the key path and the vanilla and mod values. Line numbers, detail text
and tracker commit hashes never enter it, so the id is identical for anyone on
the same game version and survives unrelated edits. A dismissed finding comes
back on its own when vanilla's value or the mod's value changes, because the id
no longer matches."""
import hashlib
import json

from .report import SEV_INFO, finding_severity

SHORT_ID = 8

# Duplicate definitions must be fixed, never dismissed.
NOT_DISMISSIBLE = frozenset({
    "dupes_multiple_sources", "dupes_define_key", "dupes_gui_definition",
})


def empty_state():
    return {"dismissed": {}, "open": {}, "reviewed_against": {}}


def target_of(f):
    return (f.key or {}).get("target") or f"{f.kind}:{f.name}"


def fingerprint_payload(f):
    key = f.key if f.key is not None else {"target": f"{f.kind}:{f.name}",
                                           "detail": f.detail or ""}
    return {"kind": f.kind, "key": key}


def finding_id(f):
    payload = json.dumps(fingerprint_payload(f), sort_keys=True,
                         ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def short_id(fid):
    return fid[:SHORT_ID]


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


def update_open(state, findings, new_tag):
    """Replace the open-findings map with the actionable findings of this run.
    A finding already open keeps the `since` and `base` recorded when it first
    appeared; findings no longer produced are closed by omission."""
    prev = state.get("open", {})
    fresh = {}
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


def bases_from_state(state, order):
    """target -> version tag to measure from. `order` lists the tracked version
    tags oldest first; tags this tracker does not know are ignored. A recorded
    review (reviewed_against) wins; otherwise the oldest base any open finding
    on that target was measured from."""
    pos = {t: i for i, t in enumerate(order)}
    bases = {}
    for entry in state.get("open", {}).values():
        t, b = entry.get("target"), entry.get("base")
        if t and b in pos and (t not in bases or pos[b] < pos[bases[t]]):
            bases[t] = b
    for t, b in state.get("reviewed_against", {}).items():
        if b in pos:
            bases[t] = b
    return bases
