"""The lint gate of the intent store.

A deviation is a finding when no rule and no recorded entry explains it, and either
it is not in the baseline, or vanilla changed it after the baseline (a patch
conflicts on it). So old unexplained deviations wait until they change. Each use of
a banned rule, each stale or lost entry and each rule conflict is a finding too.

The check reads the deviations through the per-copy cache (devcache.py), so a run
compares only the copies whose text changed."""
from . import diff3, intent

VANILLA_OR_CONFLICT = set(diff3.VANILLA_KINDS) | set(diff3.CONFLICT_KINDS)
ID_LEN = 16


def set_baseline(it, copies, devs, mod_commit, vanilla, today):
    """Record the deviations that nothing explains now; the gate exempts them."""
    states = intent.entry_states(it, copies, devs)
    ids = sorted({d.id[:ID_LEN] for d in devs
                  if not d.generated and intent.attribute(d, it, states) is None})
    it["baseline"] = {"set_on": today, "mod_commit": mod_commit, "vanilla": vanilla, "deviations": ids}
    return len(ids)


def check(it, copies, devs, order, changed=None):
    """{"vanilla", "findings": [...], "exempt": n, "attributed": n}. `order`: the
    version tags oldest first. `changed`: mod paths to limit the check to, or None."""
    states = intent.entry_states(it, copies, devs)
    base = it.get("baseline")
    findings = []
    if base is None:
        findings.append({"type": "no_baseline",
                         "text": "the intent store has no baseline; run `pdx-audit intent baseline --set`"})
        return {"findings": findings, "exempt": 0, "attributed": 0}
    exempt_ids = set(base.get("deviations", []))
    pos = {t: i for i, t in enumerate(order)}
    base_pos = pos.get(base.get("vanilla"), -1)
    exempt = attributed = 0
    touched = set()
    for d in devs:
        if d.generated or (changed is not None and d.file not in changed):
            continue
        touched.add(d.identity)
        a = intent.attribute(d, it, states)
        if a is not None and a.conflict:
            findings.append(_f("rule_conflict", d, f"rules disagree: {', '.join(a.conflict)}"))
            continue
        if a is not None:
            attributed += 1
            if a.disposition == "banned":
                findings.append(_f("banned", d, f"{a.by} bans this"))
            continue
        after_base = d.since is not None and pos.get(d.since, -1) > base_pos
        if d.id[:ID_LEN] in exempt_ids and not (after_base and d.change in VANILLA_OR_CONFLICT):
            exempt += 1
            continue
        why = ("vanilla changed it after the baseline" if d.id[:ID_LEN] in exempt_ids
               else "new or changed since the baseline")
        findings.append(_f("unattributed", d, f"no rule or entry explains this {d.change} ({why})"))
    for e in it["entries"].values():
        if changed is not None and e["address"]["identity"] not in touched:
            continue
        state, detail = states.get(e["id"], ("lost", ""))
        if state != "recorded":
            findings.append({"type": state, "entry": e["id"], "identity": e["address"]["identity"],
                             "text": f"entry {e['id']} is {state}" + (f": {detail}" if detail else "")})
    return {"findings": findings, "exempt": exempt, "attributed": attributed}


def _f(kind, d, text):
    return {"type": kind, "deviation": d.id[:ID_LEN], "file": d.file, "line": d.line, "change": d.change,
            "identity": d.identity, "path": [s["key"] for s in d.path], "text": text}


def lines(result):
    """One pdx-lint line per finding."""
    out = []
    for f in result["findings"]:
        if "file" in f:
            out.append(f"{f['file']}:{f['line']}: {' > '.join(f['path']) or f['identity']}: {f['text']}")
        else:
            out.append(f"{f.get('identity', 'intent')}: {f['text']}")
    return out
