"""Shared diff and formatting primitives, findings, and terminal colouring."""

import os
import re
import sys
import difflib
from collections import namedtuple

from .diff3 import CONFLICT_KINDS, VANILLA_KINDS

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
# not one paragraph, which is what a large mod needs.
#   detail    short per-item extra shown in the triage
#   location  'file:line'
#   data      optional rich --display payload, ignored by the terminal triage
#   key       the finding's identity for its fingerprint (see ledger.py): a
#             dict with a "target" plus whatever values make it this finding
#   since     version tag of the vanilla patch the finding comes from
#   base      version tag the finding was measured from
Finding = namedtuple("Finding", "kind name location detail data key since base")
Finding.__new__.__defaults__ = ("", None, None, None, None)

# kind -> (severity, audit_tag, plural_label, shared_fix). The label reads after
# a count ("3 statements vanilla added ..."); the fix is stated once for the class.
KIND = {
    "dupes_multiple_sources": (
        SEV_BROKEN, "dupes", "names defined or overridden in more than one place in the mod",
        "keep one definition, REPLACE or INJECT and merge the others into it"),
    "dupes_define_key": (
        SEV_BROKEN, "dupes", "define keys set in more than one place in the mod",
        "keep one and delete the others"),
    "dupes_gui_definition": (
        SEV_BROKEN, "dupes", "GUI templates or types defined in more than one mod file",
        "keep one definition and delete the others"),
    "override_orphaned": (
        SEV_BROKEN, "override", "override targets a block vanilla no longer defines",
        "remove the override, or point it at the block vanilla replaced it with"),
    "override_unreadable": (
        SEV_BROKEN, "override", "REPLACE blocks whose braces never close",
        "close the block so the game and the audit can read it"),
}

# Changes a copy lacks or conflicts with (see diff3). A vanilla change is high where
# the block holding it also holds an edit of yours, and mid otherwise.
_TAKE = "take vanilla's new value, or dismiss the finding if keeping the old one is deliberate"
_ADD = "copy the statement in, or dismiss the finding if leaving it out is deliberate"
_DELETE = "delete the statement, or dismiss the finding if keeping it is deliberate"
_CHECK = "check your change still makes sense against vanilla's"
_RESTORE = "check whether vanilla's new version belongs back in your copy"
_BLOCK = "take vanilla's changes to the block, or dismiss the finding if keeping yours is deliberate"
KIND.update({
    "gui_block_changed_high": (
        SEV_STALE, "gui", "GUI blocks vanilla changed in several places, beside or over an edit of yours", _BLOCK),
    "gui_block_changed_mid": (
        SEV_REVIEW, "gui", "GUI blocks vanilla changed in several places", _BLOCK),
    "gui_vanilla_changed_high": (
        SEV_STALE, "gui", "statements your GUI copy keeps at an old vanilla value, beside an edit of yours", _TAKE),
    "gui_vanilla_added_high": (
        SEV_STALE, "gui", "statements vanilla added that your GUI copy lacks, beside an edit of yours", _ADD),
    "gui_vanilla_removed_high": (
        SEV_STALE, "gui", "statements vanilla deleted that your GUI copy still carries, beside an edit of yours",
        _DELETE),
    "gui_both_changed_high": (
        SEV_STALE, "gui", "statements you changed in your GUI copy that vanilla also changed or deleted", _CHECK),
    "gui_removed_changed_high": (
        SEV_STALE, "gui", "statements you deleted from your GUI copy that vanilla has since changed", _RESTORE),
    "override_vanilla_changed_high": (
        SEV_STALE, "override", "statements your REPLACE keeps at an old vanilla value, beside an edit of yours",
        _TAKE),
    "override_vanilla_added_high": (
        SEV_STALE, "override", "statements vanilla added that your REPLACE lacks, beside an edit of yours", _ADD),
    "override_vanilla_removed_high": (
        SEV_STALE, "override", "statements vanilla deleted that your REPLACE still carries, beside an edit of yours",
        _DELETE),
    "override_both_changed_high": (
        SEV_STALE, "override", "statements you changed in your REPLACE that vanilla also changed or deleted", _CHECK),
    "override_removed_changed_high": (
        SEV_STALE, "override", "statements you deleted from your REPLACE that vanilla has since changed", _RESTORE),
    "gui_vanilla_changed_mid": (
        SEV_REVIEW, "gui", "statements your GUI copy keeps at an old vanilla value", _TAKE),
    "gui_vanilla_added_mid": (
        SEV_REVIEW, "gui", "statements vanilla added that your GUI copy lacks", _ADD),
    "gui_vanilla_removed_mid": (
        SEV_REVIEW, "gui", "statements vanilla deleted that your GUI copy still carries", _DELETE),
    "override_vanilla_changed_mid": (
        SEV_REVIEW, "override", "statements your REPLACE keeps at an old vanilla value", _TAKE),
    "override_vanilla_added_mid": (
        SEV_REVIEW, "override", "statements vanilla added that your REPLACE lacks", _ADD),
    "override_vanilla_removed_mid": (
        SEV_REVIEW, "override", "statements vanilla deleted that your REPLACE still carries", _DELETE),
    "loc_changed": (
        SEV_STALE, "loc", "loc keys vanilla reworded that your override masks",
        "update your override to match, or drop it if the rewording matters"),
    "override_inject_overlap": (
        SEV_REVIEW, "override", "INJECT targets where vanilla also changed a key you inject at the top level",
        "check whether your injected key now duplicates or conflicts with vanilla's"),
    "override_absent": (
        SEV_REVIEW, "override", "override targets with no matching name in vanilla",
        "point the override at an existing vanilla name, remove it, or confirm it is mod-only"),
    "gui_file_review": (
        SEV_REVIEW, "gui", "same-path GUI files where vanilla added or removed its copy",
        "confirm your override still makes sense against vanilla"),
    "gui_van_removed": (
        SEV_REVIEW, "gui", "shadowed GUI definitions vanilla removed",
        "confirm you still want to define them"),
    "gui_new_collision": (
        SEV_REVIEW, "gui", "names vanilla now also defines",
        "check whether you meant to override vanilla's new definition, or rename yours"),
    "loc_removed": (
        SEV_REVIEW, "loc", "loc keys vanilla removed",
        "remove the override, or confirm the key is mod-only"),
    "loc_collision": (
        SEV_REVIEW, "loc", "loc keys vanilla now also defines",
        "check whether you meant to override vanilla's new key, or rename yours"),
    "deps_key_dropped": (
        SEV_REVIEW, "deps", "keys the mod writes that vanilla no longer uses",
        "find what vanilla uses now and update or remove the key"),
    "deps_ref_dropped": (
        SEV_REVIEW, "deps", "names the mod references that vanilla no longer uses",
        "find what vanilla uses now and update or remove the reference"),
    "dupes_plain_other_file": (
        SEV_REVIEW, "dupes", "plain definitions of a vanilla name in a file not at vanilla's path",
        "use REPLACE or INJECT, or give the file vanilla's path to replace the whole file"),
    # informational: counted, never listed
    "override_inject_context": (SEV_INFO, "override", "", ""),
    "override_nonblock": (SEV_INFO, "override", "", ""),
    "dupes_file_override_drops": (SEV_INFO, "dupes", "", ""),
})
_KIND_ORDER = sorted(KIND, key=lambda k: _SEV_ORDER.index(KIND[k][0]))

_AUDIT_NAME = {"overrides": "override", "deps": "dependency",
               "gui": "GUI", "loc": "localization", "dupes": "duplicate"}


def finding_severity(f):
    return KIND[f.kind][0]


def change_kind(kind):
    """The diff3 change kind a finding kind was made from, or None."""
    for change in VANILLA_KINDS + CONFLICT_KINDS:
        if kind.endswith((f"_{change}_high", f"_{change}_mid")):
            return change
    return None


def brief(text, width=120):
    """One statement's text for a terminal line, cut at `width` characters."""
    return text if len(text) <= width else text[:width - 1] + "…"


def value_pair(change, yours, vanilla, since):
    """(yours, vanilla) texts for one change: your statement, and what vanilla did."""
    tag = f"  ({since})" if since else ""
    yours = brief(yours) if yours is not None else None
    vanilla = brief(vanilla) if vanilla is not None else None
    if change == "vanilla_changed":
        return yours, f"{yours}  →  {vanilla}{tag}"
    if change == "vanilla_added":
        return "(missing)", f"added {vanilla}{tag}"
    if change == "vanilla_removed":
        return yours, f"deleted{tag}"
    if change == "both_changed":
        return f"**{yours}**", (f"changed to {vanilla}{tag}" if vanilla is not None else f"deleted{tag}")
    return "(removed)", f"changed to {vanilla}{tag}"


def line_label(key):
    """'a > b' for the blocks holding a change ('' at the top)."""
    return " > ".join(key.get("path") or [])


def _is_value_finding(f):
    return change_kind(f.kind) is not None and "yours" in (f.key or {})


def render_triage(findings, old_msg, new_msg, selected, detail_shown=True,
                  new_tag=None, dismissed=0):
    """The cross-audit summary printed above the per-audit detail. `findings` is
    every audit's visible (not dismissed) Finding list. Returns Markdown (one
    string); the ColorWriter tints it when stdout is a terminal.

    Findings are grouped by CLASS (severity + kind), most urgent first. Each
    class states its description and its one shared remedy once, then lists its
    affected items one compact line each, prefixed with the id `--dismiss`
    takes (duplicates, which cannot be dismissed, carry none). When `new_tag`
    is given, findings from an earlier patch than `new_tag` are listed in a
    separate "still open" section. Informational findings are counted but not
    detailed; `dismissed` findings are only counted."""
    from .ledger import finding_id, is_dismissible, short_id

    ran = ", ".join(_AUDIT_NAME.get(s, s) for s in selected)
    title = "# Audit summary"
    if old_msg or new_msg:
        title += f": {old_msg} → {new_msg}"
    lines = [title, ""]
    dismissed_line = (f"{dismissed} dismissed findings hidden; list them with "
                      f"`pdx-audit --show-dismissed`." if dismissed else None)

    actionable = [f for f in findings if finding_severity(f) != SEV_INFO]
    n_info = len(findings) - len(actionable)
    if not actionable:
        tail = f" ({n_info} informational)" if n_info else ""
        lines.append(f"Ran {ran}. **No action needed**: everything the mod "
                     f"overrides is current with vanilla.{tail}")
        if dismissed_line:
            lines += ["", dismissed_line]
        return "\n".join(lines)

    by_sev = {s: [f for f in actionable if finding_severity(f) == s]
              for s in _SEV_ORDER}
    counts = ", ".join(f"{len(by_sev[s])} {s}" for s in _SEV_ORDER if by_sev[s])
    info_note = f" ({n_info} informational)" if n_info else ""
    lines.append(f"Ran {ran}. **{len(actionable)} findings need attention**: "
                 f"{counts}.{info_note}")
    lines.append("")

    earlier = [f for f in actionable if new_tag and f.since and f.since != new_tag]
    earlier_ids = {id(f) for f in earlier}
    current = [f for f in actionable if id(f) not in earlier_ids]

    def section(items, show_since):
        for sev in _SEV_ORDER:
            sym = _SEV_SYMBOL.get(sev, "")
            for kind in _KIND_ORDER:
                if KIND[kind][0] != sev:
                    continue
                group = [f for f in items if f.kind == kind]
                if not group:
                    continue
                _s, _a, label, fix = KIND[kind]
                head = f"{sym} **{len(group)} {label}.**"
                lines.append(head + (f" Fix: {fix}." if fix else ""))
                first_id = None
                for f in sorted(group, key=lambda x: (x.name, x.detail or "")):
                    sid = short_id(finding_id(f)) if is_dismissible(f) else None
                    first_id = first_id or sid
                    fid = f"[{sid}] " if sid else ""
                    loc = (f" `{f.location}`"
                           if f.location and f.location != f.name else "")
                    if _is_value_finding(f):
                        k = f.key
                        label = line_label(k)
                        lines.append(f"  - {fid}`{f.name}`{loc}" + (f" {label}" if label else ""))
                        yours, vanilla = value_pair(change_kind(f.kind), k["yours"], k["vanilla"], f.since)
                        lines.extend([f"      yours:    {yours}", f"      vanilla:  {vanilla}"])
                        continue
                    bits = [b for b in (f.detail, f"since {f.since}" if show_since else "") if b]
                    extra = f" ({'; '.join(bits)})" if bits else ""
                    lines.append(f"  - {fid}`{f.name}`{loc}{extra}")
                if first_id:
                    lines.append(f'    To keep one as it is: `pdx-audit --dismiss {first_id} --reason "why"`')
                lines.append("")

    if earlier:
        if current:
            lines.append(f"## This patch{f' ({new_tag})' if new_tag else ''} and current state")
            lines.append("")
            section(current, False)
        lines.append("## Still open from earlier patches")
        lines.append("")
        section(earlier, True)
    else:
        section(current, False)

    first_sev = next(s for s in _SEV_ORDER if by_sev.get(s))
    top = _SEV_HEAD[first_sev].split(":")[0].lower()
    if detail_shown:
        closer = "Full per-audit detail follows below."
    else:
        closer = "Re-run without --summary for the per-audit detail."
    if dismissed_line:
        lines.append(dismissed_line)
    if any(is_dismissible(f) for f in actionable):
        lines.append('To keep a finding as it is, dismiss it: `pdx-audit --dismiss <id> --reason "why"`. '
                     "It stays hidden in later runs until vanilla's value or yours changes; "
                     "`pdx-audit --undismiss <id>` brings it back.")
    if any(not is_dismissible(f) for f in actionable):
        lines.append("Duplicate definitions cannot be dismissed; fix them in the mod.")
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

_VALUE_LINE = re.compile(r"^(\s+)(yours:|vanilla:)(\s+)(.*)$")
_PATCH_TAG = re.compile(r"^(.*?)(\s+)(\([^()]*\))$")


def _render_value_line(m):
    """`yours:` / `vanilla:` lines: labels and vanilla's old value dim, vanilla's
    new or added value green, the patch tag dim, your value as written."""
    indent, label, gap, rest = m.groups()
    tag = ""
    if label == "vanilla:":
        t = _PATCH_TAG.match(rest)
        if t:
            rest, tag = t.group(1), t.group(2) + _wrap(("dim",), t.group(3))
    if label == "yours:":
        body = _inline(rest)
    elif "  →  " in rest:
        old, new = rest.rsplit("  →  ", 1)
        body = _wrap(("dim",), old) + _wrap(("dim",), "  →  ") + _wrap(("green",), new)
    elif rest.startswith(("added ", "changed to ")):
        verb = "added " if rest.startswith("added ") else "changed to "
        body = _wrap(("dim",), verb) + _wrap(("green",), rest[len(verb):])
    else:
        body = _wrap(("dim",), rest)
    return indent + _wrap(("dim",), label) + gap + body + tag


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
    m = _VALUE_LINE.match(line)
    if m:
        return _render_value_line(m)
    if any(sym in line for sym, _col in _SYMBOLS):
        rendered = _inline(line)
        for sym, col in _SYMBOLS:
            rendered = rendered.replace(sym, _wrap((col,), sym))
        return rendered
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
