"""Fork detection by adoption.

Given a mod's copy of a block (a REPLACE body, a shadowed GUI definition, or a
whole replaced file) and vanilla's text for it at each tracked version, find the
newest vanilla patch the copy has mostly adopted. That patch is the version the
copy was last synced to, and drift is measured from there.

At each patch where vanilla changed the text, the score is the share of
vanilla's added lines the copy contains plus the share of its removed lines the
copy has dropped. Lines are compared after normalization, so indentation, brace
packing and comments do not matter, and the copy's own extra lines do not count
against it."""
import difflib

from .normalize import _explode_norms

ADOPTED = 0.5      # a patch at or above this share is adopted
PARTIAL = 0.75     # an adopted patch below this share is only partly adopted
MIN_LINES = 3      # a patch smaller than this cannot set the fork point on its own


def _norms(text, norms=_explode_norms):
    return None if text is None else norms(text)


def change_stats(prev, cur, mod_set):
    """(adopted lines, changed lines) for vanilla's prev -> cur change against a
    copy's normalized line set. Brace-only lines are ignored, and a removed line
    still present elsewhere in `cur` is a move, not a removal."""
    cur_set = set(cur)
    added, removed = [], []
    for line in difflib.unified_diff(prev, cur, n=0, lineterm=""):
        if line.startswith(("+++", "---", "@@")):
            continue
        body = line[1:]
        if not body.strip("{} "):
            continue
        if line.startswith("+"):
            added.append(body)
        elif line.startswith("-") and body not in cur_set:
            removed.append(body)
    got = sum(1 for l in added if l in mod_set) + sum(1 for l in removed if l not in mod_set)
    return got, len(added) + len(removed)


def detect_fork(mod_text, texts, stats=None, norms=_explode_norms):
    """texts: vanilla's text at each tracked version, oldest first (None where
    vanilla has no such block). Returns (fork_index, partials): fork_index is the
    index of the newest adopted patch, or None when no patch was adopted;
    partials lists (index, adopted, changed) for patches of at least MIN_LINES
    changes, at or before the fork point, that were adopted only in part.

    `stats(prev_text, cur_text)` may replace the line-based score with any
    (adopted, changed) count; the REPLACE audit scores with its three-way
    classes. `norms` normalizes each text; the GUI audit passes one that reads
    statements, so a patch that only re-lays-out a block is not a change."""
    mod_set = set(norms(mod_text)) if stats is None else None
    normalized = [_norms(t, norms) for t in texts]
    fork = None
    prev_adopted = None
    scored = []
    for i in range(1, len(normalized)):
        a, b = normalized[i - 1], normalized[i]
        if b is None:
            prev_adopted = None
            continue
        if a is None:
            prev_adopted = None       # the block (re)appeared: a fresh start
            continue
        if a == b:
            continue
        if stats is None:
            got, total = change_stats(a, b, mod_set)
        else:
            got, total = stats(texts[i - 1], texts[i])
        if total == 0:
            continue
        score = got / total
        adopted = score >= ADOPTED
        if adopted and (total >= MIN_LINES or prev_adopted is not False):
            fork = i
        scored.append((i, got, total, score))
        prev_adopted = adopted
    partials = [(i, got, total) for i, got, total, score in scored
                if fork is not None and i <= fork and ADOPTED <= score < PARTIAL
                and total >= MIN_LINES]
    return fork, partials


def first_change_after(texts, base_index, upto_index, norms=_explode_norms):
    """Index of the first version after `base_index` (up to `upto_index`) whose
    normalized text differs from the version before it, or None."""
    prev = _norms(texts[base_index], norms)
    for i in range(base_index + 1, upto_index + 1):
        cur = _norms(texts[i], norms)
        if cur != prev:
            return i
        prev = cur
    return None
