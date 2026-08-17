"""Shared diff and formatting primitives, findings, and terminal colouring."""

import os
import re
import sys
import difflib
from collections import namedtuple

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


# ---------------------------------------------------------------------------
# Findings and cross-audit triage
#
# Every audit prints its own detailed section AND returns a list of Finding
# objects. The CLI collects the findings from all audits that ran and renders
# one severity-ranked summary above the detail, so a reader sees what to open
# first, across audits, before wading into any single report. This is the
# layer a human wants and an all-enumerating dump does not provide.
# ---------------------------------------------------------------------------

# Severity tiers, most urgent first.
SEV_BROKEN = "broken"   # the override cannot take effect as written
SEV_STALE = "stale"     # it takes effect but suppresses vanilla's newer content
SEV_REVIEW = "review"   # it may have drifted; a human has to judge
SEV_INFO = "info"       # no action: reconciled or purely informational

_SEV_ORDER = (SEV_BROKEN, SEV_STALE, SEV_REVIEW, SEV_INFO)
_SEV_SYMBOL = {SEV_BROKEN: "✗", SEV_STALE: "✗", SEV_REVIEW: "⚠"}
_SEV_HEAD = {
    SEV_BROKEN: "Broken: won't take effect as written",
    SEV_STALE: "Stale: your copy hides vanilla's newer content",
    SEV_REVIEW: "Review: possible drift, confirm by hand",
}

# A finding is one affected item, tagged with the CLASS of problem it belongs
# to. The class carries the severity, the one-line description, and the single
# shared remedy, so the triage states each of those once and then lists the
# items under it. This keeps output growing one short line per real finding,
# not one paragraph, which is what a large mod needs. `detail` is a short
# per-item extra (a vanilla target, a rename guess); `location` is 'file:line';
# `data` is an optional dict carrying the rich --display payload (diff, patch,
# gap, ...), which the terminal triage ignores.
Finding = namedtuple("Finding", "kind name location detail data")
Finding.__new__.__defaults__ = ("", None)   # detail and data are optional

# kind -> (severity, audit_tag, plural_label, shared_fix). The label reads after
# a count ("103 REPLACE blocks that ..."); the fix is stated once for the class.
KIND = {
    "override_orphaned": (
        SEV_BROKEN, "override", "override targets a block vanilla no longer defines",
        "remove the override, or repoint it at the block vanilla renamed it to"),
    "override_replace_stale": (
        SEV_STALE, "override", "REPLACE blocks that no longer carry vanilla's current lines",
        "merge vanilla's change into your block, or switch to INJECT"),
    "override_inject_overlap": (
        SEV_REVIEW, "override", "INJECT targets where vanilla also changed a key you inject at the top level",
        "check whether your injected key now duplicates or conflicts with vanilla's"),
    "gui_shadow_stale": (
        SEV_STALE, "gui", "shadowed GUI definitions your copy is now behind on",
        "merge vanilla's change into your copy"),
    "gui_file_replaced": (
        SEV_STALE, "gui", "whole-file GUI replacements where vanilla changed underneath",
        "reconcile the changed definitions listed in the GUI detail"),
    "loc_changed": (
        SEV_STALE, "loc", "loc keys vanilla reworded that your override masks",
        "update your override to match, or drop it if the rewording matters"),
    "override_replace_review": (
        SEV_REVIEW, "override", "REPLACE blocks whose match with vanilla could not be confirmed",
        "compare your block against vanilla and reconcile if it matters"),
    # informational: vanilla changed the block but not the keys this INJECT adds,
    # so the injection still lands the same way (counted, not listed).
    "override_inject_context": (SEV_INFO, "override", "", ""),
    "override_absent": (
        SEV_REVIEW, "override", "override targets with no matching name in vanilla",
        "repoint or remove the override, or confirm it is mod-only"),
    "gui_file_review": (
        SEV_REVIEW, "gui", "same-path GUI files where vanilla added or removed its copy",
        "confirm your override still makes sense against vanilla"),
    "gui_van_removed": (
        SEV_REVIEW, "gui", "shadowed GUI definitions vanilla removed",
        "confirm you still want to define them"),
    "gui_new_collision": (
        SEV_REVIEW, "gui", "names vanilla now also defines too",
        "check whether you meant to override vanilla's new definition, or rename yours"),
    "loc_removed": (
        SEV_REVIEW, "loc", "loc keys vanilla removed",
        "remove the override, or confirm the key is mod-only"),
    "loc_collision": (
        SEV_REVIEW, "loc", "loc keys vanilla now also defines",
        "check whether you meant to override vanilla's new key, or rename yours"),
    "deps_key_dropped": (
        SEV_REVIEW, "deps", "keys the mod writes that vanilla dropped",
        "check for a rename and match vanilla's current name"),
    "deps_ref_dropped": (
        SEV_REVIEW, "deps", "names the mod references that vanilla dropped",
        "repoint the reference at vanilla's current name"),
    # informational: counted, never listed
    "override_replace_reconciled": (SEV_INFO, "override", "", ""),
    "override_nonblock": (SEV_INFO, "override", "", ""),
    "gui_reconciled": (SEV_INFO, "gui", "", ""),
}
_KIND_ORDER = list(KIND)

_AUDIT_NAME = {"overrides": "override", "deps": "dependency",
               "gui": "GUI", "loc": "localization"}


def finding_severity(f):
    return KIND[f.kind][0]


def render_triage(findings, old_msg, new_msg, selected, detail_shown=True, report=False):
    """The cross-audit summary printed above the per-audit detail. `findings` is
    every audit's Finding list concatenated. Returns Markdown (one string); the
    ColorWriter tints it when stdout is a terminal.

    Findings are grouped by CLASS (severity + kind), most urgent first. Each
    class states its description and its one shared remedy once, then lists its
    affected items one compact line each, so a large mod's real workload shows
    as a long-but-flat list rather than a wall of repeated paragraphs. Nothing
    is hidden: every actionable finding is listed. Informational findings are
    counted but not detailed. With nothing actionable it prints a clean verdict.
    `detail_shown` is False under --summary, when no per-audit detail follows."""
    ran = ", ".join(_AUDIT_NAME.get(s, s) for s in selected)
    title = "# Audit summary"
    if old_msg or new_msg:
        title += f": {old_msg} → {new_msg}"
    lines = [title, ""]

    actionable = [f for f in findings if finding_severity(f) != SEV_INFO]
    n_info = len(findings) - len(actionable)
    if not actionable:
        tail = f" ({n_info} informational)" if n_info else ""
        lines.append(f"Ran {ran}. **No action needed**: everything the mod "
                     f"overrides is current with vanilla.{tail}")
        return "\n".join(lines)

    by_sev = {s: [f for f in actionable if finding_severity(f) == s]
              for s in _SEV_ORDER}
    counts = ", ".join(f"{len(by_sev[s])} {s}" for s in _SEV_ORDER if by_sev[s])
    info_note = f" ({n_info} informational)" if n_info else ""
    lines.append(f"Ran {ran}. **{len(actionable)} findings need attention**: "
                 f"{counts}.{info_note}")
    lines.append("")

    first_sev = None
    for sev in _SEV_ORDER:
        if not by_sev.get(sev):
            continue
        first_sev = first_sev or sev
        sym = _SEV_SYMBOL[sev]
        for kind in _KIND_ORDER:
            if KIND[kind][0] != sev:
                continue
            items = [f for f in actionable if f.kind == kind]
            if not items:
                continue
            _s, _a, label, fix = KIND[kind]
            head = f"{sym} **{len(items)} {label}.**"
            lines.append(head + (f" Fix: {fix}." if fix else ""))
            for f in sorted(items, key=lambda x: x.name):
                loc = (f" `{f.location}`"
                       if f.location and f.location != f.name else "")
                extra = f" ({f.detail})" if f.detail else ""
                lines.append(f"  - `{f.name}`{loc}{extra}")
            lines.append("")

    top = _SEV_HEAD[first_sev].split(":")[0].lower()
    if report:
        closer = "Full detail, grouped by file, is in the HTML report."
    elif detail_shown:
        closer = "Full per-audit detail follows below."
    else:
        closer = "Re-run without --summary for the per-audit detail."
    lines.append(f"**Open the {top} items first.** {closer}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Terminal colouring
#
# The audits emit Markdown. When stdout is a terminal the CLI wraps it in a
# ColorWriter that renders that Markdown as ANSI: headers and emphasis become
# styling, severity symbols tint their line, and ```diff blocks are coloured.
# When output is redirected the wrapper is not installed, so files and pipes
# still receive plain Markdown.
# ---------------------------------------------------------------------------

_ANSI = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "underline": "\033[4m", "red": "\033[31m", "green": "\033[32m",
    "yellow": "\033[33m", "cyan": "\033[36m",
}


def color_enabled(mode):
    """Whether to colour, given --color's value ('auto' | 'always' | 'never').
    'auto' colours only a real terminal and honours the NO_COLOR convention."""
    if mode == "always":
        return True
    if mode == "never":
        return False
    return (sys.stdout.isatty()
            and "NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb")


def _wrap(names, text):
    return "".join(_ANSI[n] for n in names) + text + _ANSI["reset"]


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`([^`]+)`")
_ITAL_RE = re.compile(r"(?<!\*)\*(?!\*)([^*]+?)\*(?!\*)")


def _strip_md(s):
    s = _BOLD_RE.sub(r"\1", s)
    s = _CODE_RE.sub(r"\1", s)
    return _ITAL_RE.sub(r"\1", s)


def _inline(s):
    s = _BOLD_RE.sub(lambda m: _wrap(("bold",), m.group(1)), s)
    s = _CODE_RE.sub(lambda m: _wrap(("cyan",), m.group(1)), s)
    s = _ITAL_RE.sub(lambda m: _wrap(("dim",), m.group(1)), s)
    for kw in ("action needed", "orphaned"):
        s = s.replace(kw, _wrap(("red",), kw))
    return s


_SYMBOLS = (("✗", "red"), ("⚠", "yellow"), ("✓", "green"), ("≈", "yellow"))


def _verdict(text):
    t = text.strip()
    if t.startswith("Action needed"):
        return "red"
    if "current with vanilla" in t:
        return "green"
    if t.startswith("No ") and "dropped" in t:
        return "green"
    return None


def _render_md(line):
    for sym, col in _SYMBOLS:
        if sym in line:
            return _wrap((col,), _strip_md(line))
    v = _verdict(_strip_md(line))
    if v:
        return _wrap((v,), _strip_md(line))
    if line.startswith("### "):
        return _wrap(("bold",), _strip_md(line[4:]))
    if line.startswith("## "):
        return _wrap(("bold", "cyan"), _strip_md(line[3:]))
    if line.startswith("# "):
        return _wrap(("bold", "underline"), _strip_md(line[2:]))
    if line.strip() == "---":
        return _wrap(("dim",), "─" * 40)
    # '  - ' / '  + ' are the removed/added previews in diff summaries: plain
    # code text, coloured like a diff. A same-prefixed line carrying Markdown
    # (backtick or bold) is a nested list item instead (the triage rolls items
    # up this way), so it falls through to inline rendering rather than being
    # tinted whole with its markers left raw.
    if line.startswith(("  - ", "  + ")) and "`" not in line and "**" not in line:
        return _wrap(("red" if line.startswith("  - ") else "green",), line)
    return _inline(line)


def _render_diff(line):
    if line.startswith(("+++", "---")):
        return _wrap(("dim",), line)
    if line.startswith("@@"):
        return _wrap(("cyan",), line)
    if line.startswith("+"):
        return _wrap(("green",), line)
    if line.startswith("-"):
        return _wrap(("red",), line)
    return line


class ColorWriter:
    """A stdout wrapper that renders the tool's Markdown as ANSI, one whole
    line at a time. A ```diff fence switches to diff colouring, and the fence
    lines themselves are dropped. Everything the audits emit is newline-
    terminated, so buffering until a newline always yields a complete line."""

    def __init__(self, stream):
        self._stream = stream
        self._buf = ""
        self._in_fence = False

    def _render(self, line):
        if line.startswith("```"):
            self._in_fence = not self._in_fence
            return None
        return _render_diff(line) if self._in_fence else _render_md(line)

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            out = self._render(line)
            if out is not None:
                self._stream.write(out + "\n")

    def flush(self):
        if self._buf:
            out = self._render(self._buf)
            self._buf = ""
            if out is not None:
                self._stream.write(out)
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)
