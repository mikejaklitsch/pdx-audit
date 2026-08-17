"""Terminal colouring: the ColorWriter renders the audits' Markdown as ANSI
while leaving redirected (non-terminal) output as plain Markdown."""
import io

from pdxaudit.report import ColorWriter, color_enabled


def _render(text):
    buf = io.StringIO()
    w = ColorWriter(buf)
    w.write(text)
    w.flush()
    return buf.getvalue()


def test_color_mode_always_and_never():
    assert color_enabled("always") is True
    assert color_enabled("never") is False


def test_headers_and_emphasis_become_ansi():
    out = _render("## Section\n- **bold** and `code`\n")
    assert "\033[" in out          # ANSI codes emitted
    assert "##" not in out         # header marker stripped
    assert "**" not in out         # bold marker stripped
    assert "`" not in out          # code marker stripped
    assert "Section" in out and "bold" in out and "code" in out


def test_severity_symbol_tints_its_line():
    out = _render("  **Mod status:** ✗ replacement is MISSING vanilla lines:\n")
    assert "\033[31m" in out       # red
    assert "✗" in out


def test_dash_prefix_is_diff_red_only_without_markdown():
    # a plain diff-summary removal line is coloured red like a diff
    assert "\033[31m" in _render("  - upkeep = 5\n")
    # a same-prefixed triage rollup line carrying Markdown is a list item: its
    # code span renders cyan and the backticks are consumed, not left raw red
    out = _render("  - `some/file.gui` (2): A, B\n")
    assert "\033[36m" in out            # code span cyan
    assert "`" not in out               # backticks stripped
    assert "\033[31m  - `" not in out   # not the raw-red diff path


def test_diff_fence_colours_and_is_dropped():
    out = _render("```diff\n+added\n-removed\n```\n")
    assert "```" not in out        # fence lines dropped
    assert "\033[32m+added" in out # add line green
    assert "\033[31m-removed" in out  # remove line red


def test_fence_closes_and_following_header_renders():
    # after a diff block, later output must return to Markdown rendering
    # rather than staying stuck in diff colouring
    buf = io.StringIO()
    w = ColorWriter(buf)
    w.write("```diff\n")
    w.write("-gone")            # diff body arrives without a trailing newline
    w.write("\n```\n")          # then the newline and closing fence
    w.write("## After\n")
    w.flush()
    out = buf.getvalue()
    assert "```" not in out                  # both fence lines dropped
    assert "\033[31m-gone" in out            # diff body coloured red
    assert "\033[1m\033[36mAfter" in out     # header after the fence, bold cyan
