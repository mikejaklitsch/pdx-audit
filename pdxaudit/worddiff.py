"""The words that differ between two versions of one statement, for the app's
block view."""
import difflib
import re

_TOKEN = re.compile(r"[\w.]+|\s+|[^\w\s]")


def word_spans(yours, vanilla):
    """([(start, end)] in yours, [(start, end)] in vanilla): the words that differ
    between two versions of one statement, as character ranges, the way a word
    diff marks them. Indentation is left out, each range is trimmed of whitespace
    at its edges, and ranges only whitespace apart join."""
    sides = []
    for text in (yours, vanilla):
        indent = len(text) - len(text.lstrip())
        sides.append([(indent + m.start(), indent + m.end(), m.group())
                      for m in _TOKEN.finditer(text[indent:])])
    a, b = sides
    matcher = difflib.SequenceMatcher(None, [t[2] for t in a], [t[2] for t in b], autojunk=False)
    spans_a, spans_b = [], []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            continue
        for tokens, lo, hi, text, spans in ((a, i1, i2, yours, spans_a), (b, j1, j2, vanilla, spans_b)):
            if lo == hi:
                continue
            start, end = tokens[lo][0], tokens[hi - 1][1]
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end - 1].isspace():
                end -= 1
            if start == end:
                continue
            if spans and not text[spans[-1][1]:start].strip():
                spans[-1] = (spans[-1][0], end)
            else:
                spans.append((start, end))
    return spans_a, spans_b
