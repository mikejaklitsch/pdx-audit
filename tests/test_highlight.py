"""Syntax colours from the bundled Paradox Highlight grammar and its Paradox Dark
theme: spans rebuild each line exactly, the theme's colours land on the right
tokens, EU5's own vocabulary is recognised, and state carries across lines."""
import time

from pdxaudit import highlight
from pdxaudit.highlight import highlighter


def _spans(*lines):
    return highlighter().line_spans(list(lines))


def _style(line, word, *before):
    spans = _spans(*before, line)[-1]
    return next((colour, bold, italic) for text, colour, bold, italic in spans if text.strip() == word)


def test_spans_rebuild_every_line_exactly():
    lines = ["REPLACE:food_advance_absolutism = {", "\tlimit = { has_x = yes }",
             '\tname = "a \\"b\\""  # note', "\tvalue >= @my_value",
             "\tscope:target.var:x = 1.5", "}", ""]
    for line, spans in zip(lines, _spans(*lines)):
        assert "".join(text for text, *_style in spans) == line


def test_comments_numbers_strings_and_booleans_take_the_theme_colours():
    assert _style("a = b # a note", "# a note")[0] == "#6A9955"
    assert _style("a = 0.25", "0.25")[0] == "#b5cea8"
    assert _style('a = "text"', '"text"')[0] == "#ce9178"
    assert _style("a = yes", "yes")[:2] == ("#e6d36c", True)


def test_eu5_effects_triggers_and_modifiers_are_recognised():
    assert _style("add_adm = 10", "add_adm")[:2] == ("#DCDCAA", True)
    assert _style("age_in_years > 30", "age_in_years")[:2] == ("#569cd6", True)
    assert _style("a_clan_retainer_cavalry_build_cost_modifier = 0.1",
                  "a_clan_retainer_cavalry_build_cost_modifier")[0] == "#9f92c9"


def test_the_text_after_a_prefix_is_recognised_too():
    assert _style("modifier:fort_level > 0", "fort_level")[0] == "#9f92c9"


def test_state_carries_across_lines():
    assert _style("\tlimit = {", "limit", "x = {")[:2] == ("#C586C0", True)


def test_a_long_block_highlights_quickly():
    block = ["x = {"] + [f"\tadd_adm = {i}  # step {i}" for i in range(400)] + ["}"]
    start = time.time()
    _spans(*block)
    assert time.time() - start < 3.0


def test_a_value_next_to_the_closing_brace_closes_the_block():
    hl = highlight.highlighter()
    stack = [hl.root]
    for _ in range(50):
        _tokens, stack = hl._line("a = { b = c d = 0.00}", stack)
    assert stack == [hl.root]                  # each line closes its own block
    assert _style("a = { d = 0.00}", "}") == _style("a = { d = 0.00 }", "}")


def test_the_worker_process_gives_the_same_spans():
    lines = ["x = {", "\tadd_adm = 10  # a note", "\td = 0.00}"]
    assert highlight.spans_of([lines, ["a = yes"]]) == [highlight.highlighter().line_spans(lines),
                                                        highlight.highlighter().line_spans(["a = yes"])]
