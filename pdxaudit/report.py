"""Shared diff and formatting primitives."""

import difflib

def diff_lines(old_text, new_text, label="block"):
    if old_text is None:
        return [f"+++ (new block, did not exist in old vanilla)\n"]
    if new_text is None:
        return [f"--- (block removed from vanilla)\n"]
    a = old_text.strip().splitlines(keepends=True)
    b = new_text.strip().splitlines(keepends=True)
    return list(difflib.unified_diff(a, b, fromfile=f"old/{label}",
                                     tofile=f"new/{label}", n=3))

def diff_summary(old_text, new_text):
    if old_text is None or new_text is None:
        return 0, 0, []
    a = old_text.strip().splitlines()
    b = new_text.strip().splitlines()
    d = list(difflib.unified_diff(a, b, n=0))
    added = [l[1:].strip() for l in d if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:].strip() for l in d if l.startswith("-") and not l.startswith("---")]

    def interesting(line):
        s = line.strip().rstrip("{}")
        return len(s) > 1

    key = []
    for r in removed:
        if interesting(r):
            key.append(f"  - {r}")
    for a_line in added:
        if interesting(a_line):
            key.append(f"  + {a_line}")
    return len(added), len(removed), key
