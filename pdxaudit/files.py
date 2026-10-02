"""Same-path file audit: a mod file at the path of a vanilla file replaces vanilla's
whole file, and nothing else audits it. The override audit reads INJECT and REPLACE,
the GUI audit reads .gui files, and the localization audit reads keys. A vanilla
change to anything inside a replaced script file (an event, a location template, a
setup entry, a plain definition in common/) is lost without this audit.

A script copy is compared definition by definition. Each top-level definition is
measured against vanilla's tracked versions of the same definition in the same
file (see diff3), with its own baseline, so a file whose definitions were updated
at different times is still measured right. Top-level statements that are not
blocks (`namespace = x`) are compared together as one more definition.

Definitions only one side has:
  a vanilla definition the copy lacks is vanilla's addition when vanilla added it
  after the version the copy's definitions match (the file's baseline), and the
  copy's own deletion otherwise; a definition the mod keeps in another file of the
  same folder was moved there and is not missing. A definition the copy keeps that
  vanilla has since deleted is reported too.

Text that is not script (.csv, shaders) is compared line by line with the same
three sides: the copy, vanilla at the copy's baseline, and vanilla now.

A definition or file too large to compare statement by statement (a generated
locator file holds one block of 20,000 entries) is compared as a whole: one
finding says how many lines vanilla changed after the version the copy matches.

A file the tracker holds no history for (an image, a mesh, or a text format older
commits did not record) cannot be measured. It is listed and counted as
informational, never skipped without a word."""

import difflib
import hashlib
import re
import sys
from collections import Counter, namedtuple
from pathlib import Path, PurePosixPath

from . import changes, session
from .config import should_skip
from .gui import audit_window
from .report import Finding
from .tracker import MODULE_ROOTS, game_root, tag_of

# Text the script parser reads, compared definition by definition.
SCRIPT_EXTS = frozenset({".txt", ".map", ".asset"})
# The name that stands for a file's top-level statements that are not blocks.
TOP_LEVEL = "(top level)"
# Above this many lines a text file is compared as a whole: line by line would take
# minutes and list thousands of edits. A script definition splits first (SPLIT_LINES).
LINES_MAX = 20000
# A definition longer than this is audited without the app's block view, which
# holds the copy and two vanilla versions in full.
VIEW_LINES = 5000
# The first bytes of a file decide if it is text.
SNIFF = 8192

Definition = namedtuple("Definition", "body line sig size")
Definition.__doc__ = """One top-level definition of a script file. body: its text.
line: the file line it starts on. sig: its code with comments and layout removed,
equal for definitions that read the same. size: its number of lines, blanked
lines between its parts left out."""

Copy = namedtuple("Copy", "mode rel label line base versions tags result")
Copy.__doc__ = """One definition or file with vanilla changes. mode: 'script' (result is
a changes.Audited), 'lines' (a list of line changes, see line_changes) or 'bulk' (a
bulk_change dict). base: the version tag the copy matches."""


def audited_elsewhere(rel):
    """True for a file another audit reads: .gui files (the GUI audit) and the files
    under a localization folder (the localization audit)."""
    p = PurePosixPath(rel)
    return p.suffix.lower() == ".gui" or (len(p.parts) > 1 and p.parts[1] == "localization")


def mod_files(mod_root):
    """Module-relative paths under the mod's module roots that this audit may read.
    Folders are among them: the caller keeps the paths vanilla has as files."""
    out = []
    for fp in session.mod_paths(mod_root):
        rel = fp.relative_to(mod_root)
        if (len(rel.parts) < 2 or rel.parts[0] not in MODULE_ROOTS or should_skip(rel)
                or audited_elsewhere(rel.as_posix())):
            continue
        out.append(rel.as_posix())
    return out


def scope_of(rel):
    """The folder whose files share one namespace of definitions: the type folder
    in common/ (in_game/common/building_types), else the module's top folder
    (in_game/events). A definition moved within it is still the same one."""
    parts = rel.split("/")
    return "/".join(parts[:3] if len(parts) > 3 and parts[1] == "common" else parts[:2])


def is_text(data):
    return b"\0" not in data[:SNIFF]


def decode(data):
    return data.decode("utf-8-sig", errors="replace")


def git_blob_id(data):
    """The id git gives a blob of these bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# --- script: definition by definition ----------------------------------------------

def definitions(text):
    """{label: Definition} for the top-level definitions of script `text`, in file
    order. A block's label is the key on the line that opens it; the top-level
    statements that are not blocks share TOP_LEVEL. Several top-level blocks under
    one label are one definition. Its text runs from the first to the last of them,
    with the other definitions between them blanked line for line, so line numbers
    inside it stay those of the file.

    The split reads braces line by line, outside comments and strings, so it is fast
    enough for a 4 MB file; diff3 parses only the definitions that differ. Two
    blocks that share one line are one definition under the first one's key."""
    return session.memo(("files.definitions", text), lambda: _definitions(text))


# A header is a key, or a keyword and a name (`scripted_trigger my_trigger = {`).
_TOP_KEY = re.compile(r'\s*([^\s=#{}"<>!?]+(?:[ \t]+[^\s=#{}"<>!?]+)?)\s*[<>!?]*=')


def _code(line):
    """A line without its comment, with the insides of its strings blanked, so
    neither holds a brace that counts."""
    if '"' not in line:
        cut = line.find("#")
        return line if cut < 0 else line[:cut]
    out, in_str = [], False
    for ch in line:
        if ch == '"':
            in_str = not in_str
            out.append(ch)
        elif in_str:
            out.append(" ")
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out)


def _split(text):
    """[(label, start offset, end offset, normalized code, first line, last line)] for each
    top-level item of `text`, line by line: a block from the line that opens it to
    the line that closes it, or a single top-level statement (label TOP_LEVEL)."""
    items, depth, pos = [], 0, 0
    cur = pending = None
    for n, line in enumerate(text.splitlines(keepends=True), 1):
        code = _code(line)
        opens, closes = code.count("{"), code.count("}")
        if depth == 0 and cur is None:
            stripped = code.strip()
            if opens:
                m = _TOP_KEY.match(code)
                label = m.group(1) if m else (pending[0] if pending else TOP_LEVEL)
                start, first = (pending[1], pending[3]) if pending and not m else (pos, n)
                cur = [label, start, [], first]
                if pending and not m:
                    cur[2].append(pending[2])
                pending = None
            elif stripped:
                if pending:
                    items.append((TOP_LEVEL, pending[1], pos, pending[2], pending[3], pending[3]))
                    pending = None
                m = _TOP_KEY.match(code)
                if m and stripped.endswith("="):
                    pending = (m.group(1), pos, " ".join(stripped.split()), n)
                else:
                    items.append((TOP_LEVEL, pos, pos + len(line), " ".join(stripped.split()), n, n))
        if cur is not None:
            cur[2].append(" ".join(code.split()))
            depth += opens - closes
            if depth <= 0:
                depth = 0
                items.append((cur[0], cur[1], pos + len(line), " ".join(filter(None, cur[2])), cur[3], n))
                cur = None
        pos += len(line)
    if pending:
        items.append((TOP_LEVEL, pending[1], len(text), pending[2], pending[3], pending[3]))
    if cur is not None:                        # a block its braces never close
        items.append((cur[0], cur[1], len(text), " ".join(filter(None, cur[2])), cur[3], n))
    return items


def _definitions(text):
    groups = {}
    for item in _split(text):
        groups.setdefault(item[0], []).append(item)
    out = {}
    for label, members in groups.items():
        first, last = members[0], members[-1]
        if len(members) == 1:
            body = text[first[1]:first[2]]
        else:
            chars = list(text[first[1]:last[2]])
            mine = {(m[1], m[2]) for m in members}
            for other in groups.values():
                for _l, a, b, _n, _f, _e in other:
                    if a >= first[1] and b <= last[2] and (a, b) not in mine:
                        for i in range(a - first[1], b - first[1]):
                            if chars[i] != "\n":
                                chars[i] = " "
            body = "".join(chars)
        out[label] = Definition(body, first[4], "\n".join(m[3] for m in members),
                                sum(m[5] - m[4] + 1 for m in members))
    return out


def file_baseline(labels, version_labels):
    """Index of the version whose set of definition labels differs least from the
    copy's, the oldest among equals, or None when vanilla has no version."""
    best = None
    for k, theirs in enumerate(version_labels):
        if theirs is None:
            continue
        d = len(labels ^ theirs)
        if best is None or d < best[0]:
            best = (d, k)
    return best and best[1]


def added_at(label, version_labels):
    """Index of the version that starts vanilla's latest unbroken run of holding
    `label`, or None when the newest version does not hold it."""
    k = len(version_labels) - 1
    if version_labels[k] is None or label not in version_labels[k]:
        return None
    while k > 0 and version_labels[k - 1] is not None and label in version_labels[k - 1]:
        k -= 1
    return k


def removed_at(label, version_labels):
    """Index of the version after the newest one that held `label`, when the newest
    does not hold it; None otherwise, or when no version held it."""
    last = len(version_labels) - 1
    if version_labels[last] is not None and label in version_labels[last]:
        return None
    held = [k for k, v in enumerate(version_labels) if v is not None and label in v]
    return held[-1] + 1 if held else None


# --- text that is not script: line by line --------------------------------------

def line_changes(mod_text, versions):
    """(baseline index, changes) for a copy of a text file that is not script.

    The baseline is the version the copy differs from least by lines, the oldest
    among equals. Each change is a dict of kind, since (a version index), line (the
    copy's line where it applies), yours, vanilla and was. A hunk vanilla changed
    after the baseline is taken when the copy holds vanilla's new lines there, not
    taken when the copy still holds the baseline's lines (vanilla_changed,
    vanilla_added or vanilla_removed), and a conflict otherwise (both_changed)."""
    mine = mod_text.split("\n")
    vers = [None if v is None else v.split("\n") for v in versions]

    def cost(v):
        ops = difflib.SequenceMatcher(None, mine, v, autojunk=False).get_opcodes()
        return sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in ops if tag != "equal")

    present = [k for k, v in enumerate(vers) if v is not None]
    if not present:
        return None, []
    b = min(present, key=lambda k: (cost(vers[k]), k))
    base, new, cur = vers[b], vers[-1], len(vers) - 1
    if b == cur or new is None:
        return b, []
    mapped = {}
    for tag, i1, i2, j1, _j2 in difflib.SequenceMatcher(None, base, mine, autojunk=False).get_opcodes():
        if tag == "equal":
            for d in range(i2 - i1):
                mapped[i1 + d] = j1 + d

    def region(i1, i2):
        before = max((mapped[i] for i in range(i1 - 1, -1, -1) if i in mapped), default=-1)
        after = min((mapped[i] for i in range(i2, len(base)) if i in mapped), default=len(mine))
        return before + 1, mine[before + 1:after]

    def since(old_lines, new_lines):
        new_seg, old_seg = "\n".join(new_lines), "\n".join(old_lines)
        for k in range(b + 1, cur + 1):
            text = versions[k]
            if text is None:
                continue
            if (new_seg in text) if new_lines else (old_seg not in text):
                return k
        return cur

    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, base, new, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        old_lines, new_lines = base[i1:i2], new[j1:j2]
        at, theirs = region(i1, i2)
        if theirs == new_lines:
            continue
        if theirs == old_lines:
            kind = {"replace": "vanilla_changed", "insert": "vanilla_added",
                    "delete": "vanilla_removed"}[tag]
        else:
            kind = "both_changed"
        out.append({"kind": kind, "since": since(old_lines, new_lines), "line": at + 1,
                    "yours": _joined(theirs), "vanilla": _joined(new_lines),
                    "was": _joined(old_lines)})
    return b, out


def _joined(lines):
    text = " / ".join(" ".join(ln.split()) for ln in lines if ln.strip())
    return text or None


# --- too large for either: as a whole --------------------------------------------

def _line_counts(text):
    """The multiset of a text's lines with their layout collapsed."""
    return session.memo(("files.lines", text), lambda: Counter(
        " ".join(ln.split()) for ln in text.split("\n") if ln.strip()))


def bulk_change(mod_text, versions):
    """For a text too large to compare statement by statement: None when vanilla has
    not changed it since the version the copy matches, else a dict of base (that
    version's index, the one whose lines differ least, the oldest among equals),
    since (the first version after it that differs) and lines (how many lines
    vanilla changed). Layout never counts."""
    mine = _line_counts(mod_text)
    best = None
    for k, v in enumerate(versions):
        if v is None:
            continue
        theirs = _line_counts(v)
        d = sum(((mine - theirs) + (theirs - mine)).values())
        if best is None or d < best[0]:
            best = (d, k)
    if best is None or versions[-1] is None:
        return None
    b = best[1]
    base, new = _line_counts(versions[b]), _line_counts(versions[-1])
    if base == new:
        return None
    since = next(k for k in range(b + 1, len(versions))
                 if versions[k] is not None and _line_counts(versions[k]) != base)
    return {"base": b, "since": since, "lines": sum(((new - base) + (base - new)).values()),
            "vanilla": hashlib.sha1(versions[-1].encode("utf-8")).hexdigest()}


# --- the audit ----------------------------------------------------------------------

def file_target(rel):
    return f"file:{rel}"


def definition_target(rel, label):
    return f"file:{rel}#{label}"


def run_file_audit(mod_root, base, old_hash, old_msg, new_hash, new_msg, args, ctx=None):
    """Compare each mod file at a vanilla file's path with vanilla's versions of that
    file, from the window's start through the new version. `base` is a Base or the
    vanilla tracker's path."""
    from .base import as_base
    base = as_base(base)
    mod_root = Path(mod_root)
    window = audit_window(base, old_hash, new_hash, args, ctx)
    tags = [tag_of(m) for _h, m in window]
    listings = [base.files(h) for h, _m in window]
    exts = [{PurePosixPath(p).suffix.lower() for p in listing} for listing in listings]
    block = getattr(args, "block", None)
    want = bool(getattr(args, "results_file", None))
    dismissed = getattr(ctx, "dismissed", frozenset())
    install = game_root()

    rels = mod_files(mod_root)
    tracked, untracked = [], []
    for rel in rels:
        ext = PurePosixPath(rel).suffix.lower()
        points = [k for k, e in enumerate(exts) if ext in e]
        if points and points[-1] == len(window) - 1:
            ids = [listings[k].get(rel) for k in points]
            if any(ids):
                tracked.append((rel, points, ids))
        elif ext and (install / rel).is_file() and (mod_root / rel).is_file():
            untracked.append(rel)
    only = getattr(ctx, "only_files", None)          # an intent check of some files
    if only is not None:
        tracked = [t for t in tracked if t[0] in only]
        untracked = [r for r in untracked if r in only]
    by_scope = {}
    for rel in rels:
        if PurePosixPath(rel).suffix.lower() in SCRIPT_EXTS:
            by_scope.setdefault(scope_of(rel), []).append(rel)

    print(f"Scanning {len(tracked) + len(untracked)} mod files at a vanilla file's path "
          f"against {len(window)} vanilla versions...", file=sys.stderr)
    blobs = base.blobs({i for _r, _p, ids in tracked for i in ids if i})

    from .registry import Generated
    generated = Generated(mod_root, ctx)
    copies, file_review, added, removed, binary = [], [], [], [], []
    counts = Counter()
    for rel, points, ids in tracked:
        lost = [i for i in ids if i and i not in blobs]
        if lost:
            # A version git could not read is not a version vanilla deleted.
            print(f"Warning: could not read {len(lost)} tracked version(s) of {rel}; "
                  f"file not audited", file=sys.stderr)
            continue
        data = (mod_root / rel).read_bytes()
        if not is_text(data) or any(i and not is_text(blobs[i]) for i in ids):
            binary.append(rel)
            continue
        text = decode(data)
        versions = [decode(blobs[i]) if i else None for i in ids]
        vtags = [tags[k] for k in points]
        is_gen, tool = generated.check(rel)
        if is_gen:
            generated.add(rel, tool, versions, vtags)
            continue
        if versions[-1] is None:
            if not block:
                last = max(k for k, v in enumerate(versions) if v is not None)
                file_review.append((rel, "removed", vtags[last + 1]))
            continue
        if not block and len(versions) > 1 and versions[-2] is None:
            file_review.append((rel, "added", vtags[-1]))

        if PurePosixPath(rel).suffix.lower() not in SCRIPT_EXTS:
            if block:
                continue
            counts["defs"] += 1
            copy = _compare_lines(rel, text, versions, vtags)
            if copy:
                copies.append(copy)
            else:
                counts["current"] += 1
            continue

        mine = definitions(text)
        theirs = [None if v is None else definitions(v) for v in versions]
        audit_level(rel, mine, theirs, vtags, "", 0, block, want, copies, added, removed, counts,
                    mod_root, by_scope)

    untracked_rows = []
    if not block:
        for rel in untracked:
            try:
                same = git_blob_id((mod_root / rel).read_bytes()) == git_blob_id((install / rel).read_bytes())
            except OSError:
                same = None
            untracked_rows.append((rel, same))
        untracked_rows += [(rel, None) for rel in binary]

    if ctx is not None:
        ctx.scanned["files"] = counts["defs"] if block else len(tracked) + len(untracked)

    fixed = iter(changes.distinct([c.result for c in copies if c.mode == "script"]))
    copies = [c._replace(result=next(fixed)) if c.mode == "script" else c for c in copies]

    _print_report(tags, window, new_msg, len(tracked) - len(binary), copies, added, removed,
                  file_review, untracked_rows, counts, args, dismissed)
    generated.print()
    return (_findings(copies, added, removed, file_review, untracked_rows)
            + generated.findings("file_generated"))


# A definition this large is audited entry by entry (`locations > stockholm`), up to
# this many levels down, before it is compared as a whole.
SPLIT_LINES = 20000
SPLIT_DEPTH = 2


def children(d):
    """{label: Definition} for the entries inside a definition's outer block, with
    their file line numbers, or None when it has no block."""
    i, j = d.body.find("{"), d.body.rfind("}")
    if i < 0 or j <= i:
        return None
    first = d.line + d.body.count("\n", 0, i + 1)
    return {k: v._replace(line=v.line + first - 1) for k, v in definitions(d.body[i + 1:j]).items()}


def audit_level(rel, mine, theirs, vtags, prefix, depth, block, want, copies, added, removed, counts,
                mod_root, by_scope):
    """Audit one level of definitions: the file's top level, or the entries of a
    definition too large to compare whole. Appends to copies, added and removed."""
    labels = [None if t is None else set(t) for t in theirs]
    for label, d in mine.items():
        name = f"{prefix}{label}"
        if depth == 0 and block and label != block and label.split(" ", 1)[0] != block:
            continue
        counts["defs"] += 1
        vdefs = [None if t is None else t.get(label) for t in theirs]
        if all(v is None for v in vdefs):
            counts["own"] += 1
            continue
        if vdefs[-1] is None:
            if label != TOP_LEVEL and not block:
                removed.append((rel, name, d.line, vtags[removed_at(label, labels)]))
            continue
        if vdefs[-1].sig == d.sig or (len({v.sig for v in vdefs if v is not None}) == 1
                                      and not changes.collecting()):
            counts["current"] += 1            # current, or vanilla never changed it
            continue
        vtexts = [None if v is None else v.body for v in vdefs]
        size = max(d.size, vdefs[-1].size)
        if size > SPLIT_LINES and depth < SPLIT_DEPTH:
            inner = children(d)
            vinner = [None if v is None else children(v) for v in vdefs]
            if inner and vinner[-1]:
                counts["defs"] -= 1
                audit_level(rel, inner, vinner, vtags, f"{name} > ", depth + 1, None, want, copies,
                            added, removed, counts, mod_root, by_scope)
                continue
        if size > LINES_MAX or size > SPLIT_LINES:
            change = bulk_change(d.body, vtexts)
            if change:
                copies.append(Copy("bulk", rel, name, d.line, vtags[change["base"]], vtexts, vtags, change))
            else:
                counts["current"] += 1
            continue
        result = changes.audit(
            "file", name, definition_target(rel, name), d.body, vtexts, vtags, rel, d.line,
            want=want and d.body.count("\n") < VIEW_LINES, block_type="File copy",
            vanilla_file=rel, grouped=True)
        if result.flagged:
            copies.append(Copy("script", rel, name, d.line, result.base, vtexts, vtags, result))
        else:
            counts["current"] += 1
    if not block:
        added += [(r, f"{prefix}{label}", since, base) for r, label, since, base in
                  _missing(rel, mine, theirs, labels, vtags, mod_root, by_scope if depth == 0 else {}, counts)]


def _compare_lines(rel, text, versions, vtags):
    """A Copy for a text file that is not script, or None when it is current."""
    if max(text.count("\n"), versions[-1].count("\n")) > LINES_MAX:
        change = bulk_change(text, versions)
        return change and Copy("bulk", rel, None, 1, vtags[change["base"]], versions, vtags, change)
    b, found = line_changes(text, versions)
    return found and Copy("lines", rel, None, 1, vtags[b], versions, vtags, found)


def _missing(rel, mine, theirs, labels, vtags, mod_root, by_scope, counts):
    """(rel, label, since, base) for each definition vanilla added to the file after
    the copy's baseline that the copy lacks and no other mod file of its folder
    keeps. Definitions the copy left out on purpose are counted."""
    fb = file_baseline(set(mine), labels)
    missing = [(label, added_at(label, labels)) for label in theirs[-1] if label not in mine]
    late = [(label, k) for label, k in missing if k is not None and fb is not None and k > fb]
    counts["deleted"] += len(missing) - len(late)
    if not late:
        return []
    elsewhere = set()
    for other in by_scope.get(scope_of(rel), ()):
        if other != rel:
            elsewhere |= _names(mod_root / other)
    out = []
    for label, k in late:
        if label in elsewhere or label.split(" ", 1)[0] in elsewhere:
            counts["deleted"] += 1          # the mod keeps it in another file
        else:
            out.append((rel, label, vtags[k], vtags[fb]))
    return out


def _names(path):
    """Names a mod file defines or overrides at its top level: plain definitions and
    the targets of INJECT, REPLACE and their variants."""
    try:
        text = session.read_text(path)
    except (OSError, UnicodeDecodeError):
        return set()
    out = set()
    for label in definitions(text):
        key = label.split(" ", 1)[0]
        out.add(label)
        prefix, sep, rest = key.partition(":")
        out.add(rest if sep and prefix.isupper() else key)
    return out


def _n_changes(c):
    return len(c.result.flagged) if c.mode == "script" else len(c.result) if c.mode == "lines" else 1


def _print_report(tags, window, new_msg, n_files, copies, added, removed, file_review,
                  untracked_rows, counts, args, dismissed):
    n_changes = sum(_n_changes(c) for c in copies)
    print(f"# Same-Path File Audit: {tags[0]} → {tags[-1]}")
    print(f"*each file copy compared with vanilla's {len(window)} tracked versions up to {new_msg}, "
          f"definition by definition*")
    print()
    print("\n".join([
        f"**{n_files}** mod files replace a vanilla file at the same path; "
        f"**{len(untracked_rows)}** more have no vanilla history to compare with",
        f"- **{len(copies)}** definitions or files with vanilla changes to take or check "
        f"({n_changes} changes)",
        f"- **{len(added)}** definitions vanilla added that a file copy lacks",
        f"- **{len(removed)}** definitions vanilla deleted that a file copy still has",
        f"- **{len(file_review)}** same-path files vanilla added or removed",
        f"- **{counts['current']}** definitions current with vanilla or never changed by it, "
        f"**{counts['own']}** of the mod's own, **{counts['deleted']}** vanilla definitions the copies "
        f"leave out",
        "",
    ]))
    if copies:
        print(f"## File Copies Vanilla Changed ({len({c.rel for c in copies})} files)")
        print()
        print("The mod file replaces vanilla's file at the same path, so a change vanilla made "
              "to any definition in it is lost unless the copy takes it.")
        print()
    for c in sorted(copies, key=lambda c: (c.rel, c.line)):
        if c.mode == "script":
            changes.print_target(c.label, [("File", f"`{c.rel}:{c.line}`")], c.result, c.versions,
                                 c.tags, args.diff, dismissed)
            continue
        print(f"### {c.label or c.rel}")
        print(f"- **File:** `{c.rel}:{c.line}`")
        print(f"- **Your copy matches:** {c.base}")
        if c.mode == "bulk":
            print(f"  ⚠ vanilla changed {c.result['lines']} lines after {c.base} "
                  f"*(since {c.tags[c.result['since']]})*; too large to compare statement by statement")
        else:
            for ch in c.result:
                print(f"  ⚠ `{c.rel}:{ch['line']}` {ch['kind'].replace('_', ' ')} "
                      f"*(since {c.tags[ch['since']]})*")
                print(f"        yours:    {ch['yours'] or '(missing)'}")
                print(f"        vanilla:  {ch['vanilla'] or '(deleted)'}")
        print()
    if added:
        print(f"## Definitions Vanilla Added That a File Copy Lacks ({len(added)})")
        print()
        print("The copy replaces vanilla's whole file, so the game does not have these definitions.")
        print()
        for rel, label, since, base in added:
            print(f"- ✗ **{label}** in `{rel}`: vanilla added it in {since}; the copy matches {base}")
        print()
    if removed:
        print(f"## Definitions Vanilla Deleted That a File Copy Still Has ({len(removed)})")
        print()
        for rel, label, line, since in removed:
            print(f"- ⚠ **{label}** at `{rel}:{line}`: vanilla deleted it in {since}")
        print()
    if file_review:
        print(f"## Same-Path Files Vanilla Added or Removed ({len(file_review)})")
        print()
        for rel, change, since in file_review:
            what = ("vanilla added this file; your file replaces it" if change == "added"
                    else "vanilla removed this file; your copy is now the only one")
            print(f"- ⚠ `{rel}`: {what} *({since})*")
        print()
    if untracked_rows:
        print(f"## Not Audited: No Vanilla History ({len(untracked_rows)})")
        print()
        print("The tracker holds no versions of these files (images, meshes, or a text format older "
              "commits did not record), so nothing tells your edits from vanilla's changes. Each is "
              "compared with the installed game only.")
        print()
        for rel, same in untracked_rows:
            print(f"- `{rel}`: {_UNTRACKED[same]}")
        print()
    if not (copies or added or removed or file_review):
        print("**All same-path file copies are current with vanilla.**")
    else:
        print("---")
        print(f"**Action needed:** {n_changes} changes in {len(copies)} definitions or files, "
              f"{len(added)} definitions a copy lacks, {len(removed)} a copy keeps after vanilla "
              f"deleted them.")
        if not args.diff and any(c.mode == "script" for c in copies):
            print("Run with `--diff` for vanilla's changes since each definition's version.")
    print()


_UNTRACKED = {True: "identical to the installed game", False: "differs from the installed game",
              None: "binary, not compared"}


def _findings(copies, added, removed, file_review, untracked_rows):
    findings = []
    for c in copies:
        if c.mode == "script":
            findings += c.result.findings
        elif c.mode == "bulk":
            target = definition_target(c.rel, c.label) if c.label else file_target(c.rel)
            findings.append(Finding(
                "file_bulk_changed", c.label or c.rel, f"{c.rel}:{c.line}",
                f"vanilla changed {c.result['lines']} lines", None,
                {"target": target, "base": c.base, "vanilla": c.result["vanilla"]},
                c.tags[c.result["since"]], c.base))
        else:
            for ch in c.result:
                findings.append(Finding(
                    f"file_{ch['kind']}_mid", c.rel, f"{c.rel}:{ch['line']}", "", None,
                    {"target": file_target(c.rel), "path": [], "yours": ch["yours"],
                     "vanilla": ch["vanilla"], "was": ch["was"]},
                    c.tags[ch["since"]], c.base))
    for rel, label, since, base in added:
        findings.append(Finding("file_def_added", label, rel, f"added in {since}", None,
                                {"target": definition_target(rel, label)}, since, base))
    for rel, label, line, since in removed:
        findings.append(Finding("file_def_removed", label, f"{rel}:{line}", f"deleted in {since}", None,
                                {"target": definition_target(rel, label)}, since))
    for rel, change, since in file_review:
        findings.append(Finding("file_review", rel, rel, f"vanilla {change}", None,
                                {"target": file_target(rel), "change": change}, since))
    for rel, same in untracked_rows:
        findings.append(Finding("file_untracked", rel, rel, _UNTRACKED[same], None,
                                {"target": file_target(rel)}))
    return findings
