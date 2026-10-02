"""Rename candidates: what vanilla wrote in place of a name it dropped.

The audit never guesses a rename from the spelling. It measures it. For each vanilla
file that used the dropped name at the last version that had it, it compares that
file with the same file at the next version, line by line, with the indentation
removed. A line that used the name and that the next version replaced votes for
each name of the same kind that the replacement lines use and the replaced lines
did not. The name with the most votes is the candidate when it holds at least half
of the sites. A candidate is evidence for a review finding. No tool applies it."""
import difflib
from collections import Counter

MIN_SHARE = 0.5


def candidates(old_files, new_files, targets, sites):
    """{target: (new name, votes, sites)} for the targets with a candidate.

    old_files, new_files: {path: text} of vanilla at the last version that had the
    targets and at the next version. targets: a set of (kind, name). sites(text):
    {line number: {(kind, name)}} for the uses in a text."""
    targets = set(targets)
    votes = {t: Counter() for t in targets}
    totals = Counter()
    for path, old in old_files.items():
        if not any(name in old for _kind, name in targets):
            continue
        old_sites = sites(old)
        if not any(set(s) & targets for s in old_sites.values()):
            continue
        new = new_files.get(path)
        if new is None:
            for s in old_sites.values():
                totals.update(set(s) & targets)
            continue
        new_sites = sites(new)
        a = [line.strip() for line in old.split("\n")]
        b = [line.strip() for line in new.split("\n")]
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
            if tag == "equal" or tag == "insert":
                continue
            before = set().union(*(old_sites.get(ln, set()) for ln in range(i1 + 1, i2 + 1)))
            after = set().union(*(new_sites.get(ln, set()) for ln in range(j1 + 1, j2 + 1)))
            for t in before & targets:
                hits = sum(1 for ln in range(i1 + 1, i2 + 1) if t in old_sites.get(ln, ()))
                totals[t] += hits
                for kind, name in after - before:
                    if kind == t[0] and name != t[1]:
                        votes[t][name] += hits
    out = {}
    for t in targets:
        if votes[t] and totals[t]:
            name, count = votes[t].most_common(1)[0]
            if count >= MIN_SHARE * totals[t]:
                out[t] = (name, min(count, totals[t]), totals[t])
    return out


def describe(candidate):
    """The text a finding shows for a candidate."""
    name, count, total = candidate
    return f"vanilla uses {name} in its place at {count} of {total} sites"
