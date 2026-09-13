"""Script override audit and dependency audit."""

import re
import sys
import difflib
import tarfile
import io
import json
import hashlib
from pathlib import Path
from collections import defaultdict

from .adoption import detect_fork, first_change_after
from .gui import commits_up_to
from .normalize import _explode_norms
from . import ledger, session
from .report import diff_lines, diff_summary, Finding, format_value_lines
from .threeway import ACTIONABLE, classify, classify_scalar, parse, tokenize
from .tracker import MODULE_ROOTS, _git_archive, full_hash
from .config import should_skip

REPLACE_TYPES = ("REPLACE", "TRY_REPLACE")

CLASS_KIND = {
    "frozen": "override_replace_frozen",
    "new_line": "override_replace_new_line",
    "kept_removed": "override_replace_kept_removed",
    "both_changed": "override_replace_both_changed",
    "unclassified": "override_replace_unclassified",
    "commented_out": "override_replace_commented_out",
    "key_removed": "override_replace_key_removed",
    "merged": "override_replace_merged",
}

CLASS_HEADING = {
    "frozen": ("✗", "kept at vanilla's old value"),
    "new_line": ("✗", "vanilla added, missing from your block"),
    "kept_removed": ("✗", "vanilla deleted, still in your block unchanged"),
    "both_changed": ("⚠", "you customized, vanilla changed it too"),
    "unclassified": ("⚠", "could not be matched line by line"),
}

def parse_top_blocks(text):
    blocks = {}
    lines = text.split("\n")
    depth = 0
    name = None
    start = None

    for i, raw in enumerate(lines):
        code = raw.split("#")[0]
        opens = code.count("{")
        closes = code.count("}")

        if depth == 0 and opens > 0 and name is None:
            m = re.match(r"\s*(\S+)\s*=\s*\{", code)
            if m:
                name = m.group(1)
                start = i

        depth += opens - closes

        if depth <= 0 and name is not None:
            blocks[name] = "\n".join(lines[start : i + 1])
            name = None
            start = None
            depth = max(0, depth)

    return blocks

def find_overrides(mod_root):
    results = []
    for fp in session.mod_paths(mod_root):
        if fp.suffix not in (".txt", ".gui"):
            continue
        rel = fp.relative_to(mod_root)
        if rel.parts[0] not in MODULE_ROOTS or should_skip(rel):
            continue
        try:
            text = session.read_text(fp)
        except Exception:
            continue
        for ln, line in enumerate(text.split("\n"), 1):
            m = re.match(r"\s*(TRY_REPLACE|TRY_INJECT|REPLACE|INJECT)\s*:\s*(\w+)", line)
            if m:
                results.append({
                    "type": m.group(1),
                    "block": m.group(2),
                    "file": rel.as_posix(),
                    "line": ln,
                    "category": rel.parent.as_posix(),
                })
    return results

def build_index(vanilla_repo, commit, categories, progress_label=""):
    idx = {}
    cats = sorted(set(categories))

    if progress_label:
        print(f"  {progress_label}: extracting {len(cats)} directories...",
              end="", file=sys.stderr, flush=True)

    raw = _git_archive(vanilla_repo, commit, cats)
    if not raw:
        if progress_label:
            print(" failed!", file=sys.stderr)
        return idx

    try:
        with tarfile.open(fileobj=io.BytesIO(raw), ignore_zeros=True) as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                if not (member.name.endswith(".txt") or member.name.endswith(".gui")):
                    continue

                f = tf.extractfile(member)
                if f is None:
                    continue
                content = f.read().decode("utf-8-sig", errors="replace")
                vfile = member.name
                cat = str(Path(vfile).parent)

                for bname, btext in parse_top_blocks(content).items():
                    key = (cat, bname)
                    if key not in idx:
                        idx[key] = (vfile, btext)
    except tarfile.TarError:
        if progress_label:
            print(" tar parse error!", file=sys.stderr)
        return idx

    if progress_label:
        print(f" {len(idx)} blocks indexed.", file=sys.stderr)
    return idx

BLOCK_CACHE_VERSION = 1

def _block_cache_path(vanilla_repo, commit, categories):
    """Cache file for a commit's block index, keyed by the full commit hash and
    the set of categories indexed. Commit content is immutable, so entries never
    go stale; the version bumps when the parser or index shape changes."""
    full = full_hash(vanilla_repo, commit)
    if not full:
        return None
    cat_key = hashlib.sha1(",".join(sorted(set(categories))).encode()).hexdigest()[:12]
    return Path(vanilla_repo).parent / "cache" / \
        f"blocks-v{BLOCK_CACHE_VERSION}-{full}-{cat_key}.json"

def build_index_cached(vanilla_repo, commit, categories, progress_label=""):
    """build_index with a per-commit disk cache. A plain override run and a
    later --diff run over the same overrides extract the same vanilla blocks; the
    parsed index is memoized under <vanilla-tracker>/cache/ keyed by the
    immutable commit hash so the second run reuses the first's work. Old entries
    are pruned by prune_cache once their commit leaves the tracker."""
    cache = _block_cache_path(vanilla_repo, commit, categories)
    if cache and cache.is_file():
        try:
            data = json.loads(cache.read_text())
            idx = {tuple(k): (v[0], v[1]) for k, v in data}
            if progress_label:
                print(f"  {progress_label}: block index from cache "
                      f"({len(idx)} blocks).", file=sys.stderr)
            return idx
        except (OSError, ValueError, KeyError, IndexError):
            pass
    idx = build_index(vanilla_repo, commit, categories, progress_label)
    if cache and idx:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            payload = [[list(k), [v[0], v[1]]] for k, v in idx.items()]
            tmp = cache.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(cache)
        except OSError:
            pass
    return idx

def block_history(vanilla_repo, commits_old_first, categories, keys):
    """{(category, block): [text or None per commit]} oldest first, for just the
    wanted keys, loading one cached block index at a time."""
    hist = {k: [] for k in keys}
    for i, (h, _msg) in enumerate(commits_old_first):
        idx = build_index_cached(vanilla_repo, h, categories,
                                 f"history {i + 1}/{len(commits_old_first)} ({h[:7]})")
        for k in keys:
            e = idx.get(k)
            hist[k].append(e[1] if e else None)
    return hist

def _brace_extract(lines, start):
    """Return the block text starting at line index `start`, brace-matched.
    None if the braces never balance."""
    depth = 0
    started = False
    for i in range(start, len(lines)):
        code = lines[i].split("#")[0]
        o, c = code.count("{"), code.count("}")
        if o:
            started = True
        depth += o - c
        if started and depth <= 0:
            return "\n".join(lines[start : i + 1])
    return None

def _mod_lines(mod_root, rel):
    """A mod file's lines. Every override in a file reads it, so a run splits it once."""
    path = mod_root / rel
    return session.memo(("lines", str(path)), lambda: session.read_text(path).split("\n"))

def extract_mod_block(mod_root, ov):
    """Extract the body of a mod override block (REPLACE:/INJECT:name = { ... })."""
    try:
        lines = _mod_lines(mod_root, ov["file"])
    except Exception:
        return None
    start = ov["line"] - 1
    if not (0 <= start < len(lines)):
        return None
    return _brace_extract(lines, start)

def mod_scalar_value(mod_root, ov):
    """The value of a single-value override line (`REPLACE:name = value`), or
    None when the override opens a block."""
    try:
        lines = _mod_lines(mod_root, ov["file"])
    except Exception:
        return None
    if not (0 < ov["line"] <= len(lines)):
        return None
    toks = tokenize(lines[ov["line"] - 1])
    if len(toks) >= 3 and toks[1] == "=" and toks[2] not in ("{", "}"):
        return toks[2]
    return None

def _norm(line):
    """Normalize a script line for containment comparison: strip inline comment,
    collapse whitespace, canonicalize spacing around '=' so `a=b` and `a = b`
    compare equal. Comment-only / blank lines normalize to ''."""
    s = re.sub(r"\s+", " ", line.split("#")[0])
    s = re.sub(r"\s*=\s*", " = ", s)
    return s.strip()

def _block_key(prefix):
    """The key naming the block opened by the '{' at the end of `prefix`."""
    m = re.search(r"([A-Za-z0-9_:.]+)\s*=\s*$", prefix)
    if m:
        return m.group(1)
    m = re.search(r"([A-Za-z0-9_:.]+)\s*$", prefix)  # bare token / weight key
    return m.group(1) if m else "*"

def _enclosing_paths(lines):
    """For each line, the tuple of enclosing block keys, outermost to innermost
    (empty at block top). Keeps the whole stack, not just the innermost key, so a
    changed line can be shown with the path to the sub-block it sits in. Comment-
    aware; approximate but precise enough to place a line."""
    stack, out = [], []
    for raw in lines:
        code = raw.split("#")[0]
        out.append(tuple(stack))
        first_open = code.find("{")
        if first_open == -1:
            for _ in range(code.count("}")):
                if stack:
                    stack.pop()
            continue
        for _ in range(code[:first_open].count("}")):  # closes before the open
            if stack:
                stack.pop()
        rest = code[first_open:]
        net = rest.count("{") - rest.count("}")
        if net > 0:
            stack.append(_block_key(code[:first_open]))
            stack.extend("*" for _ in range(net - 1))
        elif net < 0:
            for _ in range(-net):
                if stack:
                    stack.pop()
    return out

def _breadcrumb(path, norm):
    """A changed line shown with the path to the sub-block it sits in, so a bare
    `estate_building_input` reads as `possible_production_methods[estate_building_input]`
    rather than a token with no home. The outermost element (the override block
    itself) is dropped; '*' marks an anonymous block."""
    rel = list(path[1:])
    return f"{' > '.join(rel)}[{norm}]" if rel else norm

def _top_level_children(block_text):
    """{key: text} for the direct children of a `name = { ... }` block. Handles
    scalar children (`k = v`) and block children (`k = { ... }`); the block text
    includes the header line, and children are the depth-1 assignments. Repeated
    keys are concatenated. This is what an INJECT actually adds at the injection
    point, and the only level at which a vanilla change can collide with it: a
    key nested inside the mod's own added effect (a scope such as `location`) is
    not an injection point, and a same-named vanilla change deep in the block is
    unrelated."""
    lines = block_text.split("\n")
    out = {}
    depth = 0
    opened = False
    key = start = None
    for i, raw in enumerate(lines):
        code = raw.split("#")[0]
        o, c = code.count("{"), code.count("}")
        if not opened:
            if o:                        # the block's own opening brace
                opened = True
                depth += o - c
            continue
        if depth == 1 and key is None:
            m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_.:]*)\s*=", code)
            if m:
                key, start = m.group(1), i
        depth += o - c
        if key is not None and depth <= 1:   # child ended on this line
            text = "\n".join(lines[start:i + 1])
            out[key] = out[key] + "\n" + text if key in out else text
            key = start = None
    return out

def inject_overlap(mod_root, ov, old_text, new_text):
    """Injection-point collisions for a changed INJECT target. An INJECT appends
    its lines as DIRECT children of the vanilla block, so only the mod's
    direct-child keys are injection points, and only vanilla's own top-level
    children can collide with them. Keys nested inside the mod's added effect,
    and same-named changes deep in vanilla, are unrelated and never matched.
    Returns (status, overlaps): overlaps is a list of (key, old_child, new_child)
    for each injected key vanilla added, removed, or changed at the top level;
    an empty list means vanilla left the injection point alone. status 'unknown'
    when there is nothing to compare."""
    if old_text is None or new_text is None:
        return "unknown", []
    block = extract_mod_block(mod_root, ov)
    if block is None:
        return "unknown", []
    mod_keys = set(_top_level_children(block))
    if not mod_keys:
        return "unknown", []
    old_children = _top_level_children(old_text)
    new_children = _top_level_children(new_text)
    overlaps = []
    for k in sorted(mod_keys):
        ov_old, ov_new = old_children.get(k), new_children.get(k)
        if ov_old is None and ov_new is None:
            continue                     # vanilla has no top-level key by this name
        if (ov_old or "").strip() != (ov_new or "").strip():
            overlaps.append((k, ov_old, ov_new))
    return "ok", overlaps

# --- rich report payload for --display -------------------------------------

def _parse_bc(bc):
    """A breadcrumb ('possible_production_methods[estate_building_input]' or a
    bare top-level line) back into (rel_path_tuple, norm_line)."""
    m = re.match(r"^(.*)\[(.*)\]$", bc)
    if m:
        return tuple(p for p in m.group(1).split(" > ") if p), m.group(2)
    return (), bc

def _content_norms(text):
    return {n for n in (_norm(l) for l in (text or "").split("\n")) if n.strip("{} ")}

def build_patch(mod_block, missing, old_text, new_text):
    """A 3-way view of the mod block against vanilla old and new. Returns
    (rows, absent, removed_note). Each row is a mod line tagged:
      'context' shared with current vanilla (or structural: header, blank, brace)
      'tracks'  the mod already matches vanilla's newer value (adopted a change)
      'mine'    the mod's own line, in neither vanilla old nor new
      'removed' a line vanilla dropped that the mod still carries
    plus 'add' rows: vanilla-new lines the mod lacks, ghosted in at their target
    sub-block. `absent` is missing lines whose target sub-block is not in the mod
    copy (each with vanilla's version and a like-named mod block if any);
    `removed_note` is vanilla's removed lines the mod never had, for reference."""
    src = mod_block.split("\n")
    paths = _enclosing_paths(src)
    rel = [tuple(p[1:]) for p in paths]
    norms = [_norm(l) for l in src]
    old_n, new_n = _content_norms(old_text), _content_norms(new_text)
    have_vanilla = bool(old_n or new_n)
    existing = set(rel)
    mod_children = _top_level_children(mod_block)
    van_children = _top_level_children(new_text) if new_text else {}

    miss_by_path, absent_raw = {}, []
    for bc in missing:
        pth, nm = _parse_bc(bc)
        if pth and pth not in existing:
            absent_raw.append(bc)
        else:
            miss_by_path.setdefault(pth, []).append(nm)

    insert_at = {}
    for pth, adds in miss_by_path.items():
        anchors = [i for i in range(len(src))
                   if rel[i] == pth and norms[i].strip("{} ")]
        if anchors:
            insert_at.setdefault(max(anchors), []).extend(adds)
        else:
            absent_raw.extend((" > ".join(pth) + "[" + a + "]") if pth else a
                              for a in adds)

    rows = []
    for i, line in enumerate(src):
        nm = norms[i]
        in_new, in_old = nm in new_n, nm in old_n
        if i == 0 or not nm.strip("{} ") or not have_vanilla or (in_new and in_old):
            cls = "context"
        elif in_new:
            cls = "tracks"
        elif in_old:
            cls = "removed"
        else:
            cls = "mine"
        rows.append({"t": line, "c": cls})
        for a in insert_at.get(i, []):
            indent = re.match(r"\s*", src[i]).group(0)
            rows.append({"t": indent + a, "c": "add"})

    def _related(name):
        toks = set(name.split("_"))
        for c in mod_children:
            if c != name and len(toks & set(c.split("_"))) >= 2:
                return c
        return None
    absent = []
    for bc in absent_raw:
        pth, _nm = _parse_bc(bc)
        top = pth[0] if pth else None
        absent.append({"line": bc, "block": top,
                       "vanilla": van_children.get(top, "") if top else "",
                       "related": _related(top) if top else None})

    old_lines = (old_text or "").split("\n")
    old_paths = _enclosing_paths(old_lines)
    old_bc = {}
    for i, l in enumerate(old_lines):
        nm = _norm(l)
        if nm.strip("{} "):
            old_bc.setdefault(nm, _breadcrumb(old_paths[i], nm))
    mod_n = {n for n in norms if n.strip("{} ")}
    mod_lhs = {n.split(" = ", 1)[0] for n in mod_n if " = " in n}
    removed_note = []
    for n in sorted(old_n - new_n - mod_n):
        key = n.split(" = ", 1)[0] if " = " in n else None
        if key and key in mod_lhs:
            continue
        removed_note.append(old_bc.get(n, n))
    return rows, absent, removed_note

def _result_breadcrumb(r, value):
    """A three-way result as a build_patch breadcrumb: 'a > b[key op value]'."""
    line = value if r.key == "@item" else f"{r.key} {r.op or '='} {value}"
    return f"{' > '.join(r.path)}[{line}]" if r.path else line

def line_detail(r, block=None):
    """Short triage text for one three-way result."""
    where = " > ".join(r.path)
    key = r.key or block or ""
    prefix = f"{where} > " if where else ""
    if r.old is None and r.new is None:
        return prefix + r.text
    old_op, new_op, mod_op = r.ops or (None, None, None)
    if len({o for o in (old_op, new_op, mod_op) if o}) > 1:
        # an operator changed (for example = became ?=): show operators too
        def both(op, val):
            return f"{op} {val}" if op else str(val)
        mine = "yours removed" if r.mod is None else f"yours {both(mod_op, r.mod)}"
        if r.old is not None and r.new is not None:
            return f"{prefix}{key}: {mine}, vanilla {both(old_op, r.old)} → {both(new_op, r.new)}"
    if r.cls in ("frozen", "both_changed", "key_removed") and r.old is not None and r.new is not None:
        mine = "yours removed" if r.mod is None else f"yours {r.mod}"
        return f"{prefix}{key}: {mine}, vanilla {r.old} → {r.new}"
    if r.cls == "both_changed" and r.new is not None:
        return f"{prefix}{key}: yours {r.mod}, vanilla added {r.new}"
    if r.cls == "both_changed" and r.old is not None:
        return f"{prefix}{key}: yours {r.mod}, vanilla removed {r.old}"
    return prefix + r.text

def override_report_data(mod_root, ov, is_replace, old_text, new_text, vanilla_file, results=None):
    """The --display payload for one changed REPLACE/INJECT finding: vanilla's
    diff, the mod block as a 3-way patch, and the gap (three-way line classes
    for REPLACE, injection-point overlaps for INJECT)."""
    n_add, n_rem, _ = diff_summary(old_text, new_text)
    data = {
        "type": "REPLACE" if is_replace else "INJECT",
        "vanilla_file": vanilla_file,
        "n_add": n_add, "n_rem": n_rem,
        "diff": "".join(diff_lines(old_text, new_text, ov["block"])).rstrip("\n"),
        "missing": [], "kept": [], "overlap": [], "lines": [],
    }
    if is_replace:
        results = results if results is not None else classify(
            extract_mod_block(mod_root, ov) or "", old_text, new_text)
        data["missing"] = [_result_breadcrumb(r, r.new) for r in results
                           if r.cls in ("new_line", "frozen") and r.new is not None]
        data["kept"] = [_result_breadcrumb(r, r.old) for r in results
                        if r.cls == "kept_removed" and r.old is not None]
        data["lines"] = [{"c": r.cls, "t": line_detail(r, ov["block"])} for r in results]
        missing_for_patch = data["missing"]
    else:
        _status, overlaps = inject_overlap(mod_root, ov, old_text, new_text)
        data["overlap"] = [{"key": k, "old": o or "", "new": n or ""}
                           for k, o, n in overlaps]
        missing_for_patch = []
    mod_block = extract_mod_block(mod_root, ov) or ""
    data["patch"], data["absent"], data["removed_note"] = build_patch(
        mod_block, missing_for_patch, old_text, new_text)
    return data

IDENT_ASSIGN = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*=")

def _lhs_tokens(text):
    """token -> count of `token =` assignments in `text` (inline comments stripped)."""
    counts = defaultdict(int)
    for raw in text.split("\n"):
        m = IDENT_ASSIGN.match(raw.split("#")[0])
        if m:
            counts[m.group(1)] += 1
    return counts

def mod_referenced_tokens(mod_root):
    """token -> ['file:line', ...] for every LHS assignment in the mod's .txt scripts."""
    usage = defaultdict(list)
    for fp in session.mod_paths(mod_root, ".txt"):
        rel = fp.relative_to(mod_root)
        if not rel.parts or rel.parts[0] not in MODULE_ROOTS or should_skip(rel):
            continue
        try:
            text = session.read_text(fp)
        except Exception:
            continue
        for ln, raw in enumerate(text.split("\n"), 1):
            m = IDENT_ASSIGN.match(raw.split("#")[0])
            if m:
                usage[m.group(1)].append(f"{rel.as_posix()}:{ln}")
    return usage

RHS_IDENT = re.compile(r"^\s*[A-Za-z][A-Za-z0-9_]*\s*=\s*([A-Za-z][A-Za-z0-9_]*)\s*$")
RHS_SKIP = {"yes", "no"}

def mod_referenced_values(mod_root):
    """value -> ['file:line', ...] for every `key = value` line in the mod's
    .txt scripts whose value is a single bare identifier: a name the mod points
    at, as opposed to a number, a block, or a boolean."""
    usage = defaultdict(list)
    for fp in session.mod_paths(mod_root, ".txt"):
        rel = fp.relative_to(mod_root)
        if not rel.parts or rel.parts[0] not in MODULE_ROOTS or should_skip(rel):
            continue
        try:
            text = session.read_text(fp)
        except Exception:
            continue
        for ln, raw in enumerate(text.split("\n"), 1):
            m = RHS_IDENT.match(raw.split("#")[0])
            if m and m.group(1) not in RHS_SKIP:
                usage[m.group(1)].append(f"{rel.as_posix()}:{ln}")
    return usage

VOCAB_CACHE_VERSION = 1

def _vocab_cache_path(vanilla_repo, commit):
    """Cache file for a commit's vocabulary, keyed by the full commit hash;
    commit content is immutable, so entries never go stale. The version bumps
    when the tokenizer changes."""
    full = full_hash(vanilla_repo, commit)
    if not full:
        return None
    return Path(vanilla_repo).parent / "cache" / \
        f"vocab-v{VOCAB_CACHE_VERSION}-{full}.json"

def build_vocab(vanilla_repo, commit, label=""):
    """token -> total `token =` occurrences across all vanilla .txt at `commit`.
    Cached under <vanilla-tracker>/cache/ per commit hash."""
    cache = _vocab_cache_path(vanilla_repo, commit)
    if cache and cache.is_file():
        try:
            vocab = json.loads(cache.read_text())
            if label:
                print(f"  {label}: vocabulary from cache ({len(vocab)} tokens).",
                      file=sys.stderr)
            return vocab
        except (OSError, ValueError):
            pass
    if label:
        print(f"  {label}: reading vanilla vocabulary...",
              end="", file=sys.stderr, flush=True)
    raw = _git_archive(vanilla_repo, commit, None, timeout=180)
    if not raw:
        if label:
            print(" failed!", file=sys.stderr)
        return {}
    vocab = defaultdict(int)
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), ignore_zeros=True) as tf:
            for member in tf.getmembers():
                if not member.isfile() or not member.name.endswith(".txt"):
                    continue
                f = tf.extractfile(member)
                if f is None:
                    continue
                content = f.read().decode("utf-8-sig", errors="replace")
                for t, c in _lhs_tokens(content).items():
                    vocab[t] += c
    except tarfile.TarError:
        if label:
            print(" tar error!", file=sys.stderr)
        return {}
    if label:
        print(f" {len(vocab)} tokens.", file=sys.stderr)
    if cache and vocab:
        try:
            cache.parent.mkdir(exist_ok=True)
            tmp = cache.with_suffix(".tmp")
            tmp.write_text(json.dumps(vocab, separators=(",", ":")))
            tmp.replace(cache)
        except OSError:
            pass
    return vocab

TOP_LEVEL_DEF = re.compile(r"^\s*(?:[A-Z][A-Z_]*:)?([A-Za-z][A-Za-z0-9_]*)\s*=")

def mod_top_level_names(mod_root):
    """Names the mod defines at the top level of its .txt scripts (including
    INJECT/REPLACE targets). References to these are the mod's own."""
    names = set()
    for fp in session.mod_paths(mod_root, ".txt"):
        rel = fp.relative_to(mod_root)
        if not rel.parts or rel.parts[0] not in MODULE_ROOTS or should_skip(rel):
            continue
        try:
            text = session.read_text(fp)
        except Exception:
            continue
        depth = 0
        for raw in text.split("\n"):
            code = raw.split("#")[0]
            if depth == 0:
                m = TOP_LEVEL_DEF.match(code)
                if m:
                    names.add(m.group(1))
            depth = max(0, depth + code.count("{") - code.count("}"))
    return names

def run_deps_audit(mod_root, vanilla_repo, old_hash, old_msg, new_hash, new_msg, ctx=None):
    """Names the mod uses that vanilla used at some tracked version up to the new
    version but no longer uses at it, each with the patch that dropped it. The
    default run looks across every tracked version; a fixed window (--old,
    --full, or no run context) only looks from the old version onward. Names
    the mod itself defines at top level are its own and never flagged."""
    keys = mod_referenced_tokens(mod_root)
    refs = mod_referenced_values(mod_root)
    own = mod_top_level_names(mod_root)
    if ctx is not None:
        ctx.scanned["deps"] = len(keys) + len(refs)
    print(f"Scanning {len(keys)} keys and {len(refs)} references in {mod_root.name}...",
          file=sys.stderr)

    commits = ctx.commits if ctx is not None else _all_commits(vanilla_repo)
    old_first = list(reversed(commits_up_to(commits, new_hash)))
    fixed_window = ctx.fixed_window if ctx is not None else True
    if fixed_window:
        for i, (h, _m) in enumerate(old_first):
            if old_hash.startswith(h) or h.startswith(old_hash):
                old_first = old_first[i:]
                break
    vocabs = []
    for i, (h, msg) in enumerate(old_first):
        v = build_vocab(vanilla_repo, h, f"vocabulary {i + 1}/{len(old_first)} ({h[:7]})")
        if v:
            vocabs.append((_tag(msg), v))
    if not vocabs or not old_first or vocabs[-1][0] != _tag(old_first[-1][1]):
        print("Could not read vanilla vocabulary (archive failed).", file=sys.stderr)
        sys.exit(1)
    new_vocab = vocabs[-1][1]
    earlier = vocabs[:-1]

    def dropped(usage, skip=frozenset()):
        out = []
        for name, sites in usage.items():
            if name in skip or name in own or new_vocab.get(name, 0) > 0:
                continue
            for i in range(len(earlier) - 1, -1, -1):
                count = earlier[i][1].get(name, 0)
                if count > 0:
                    out.append((name, earlier[i][0], vocabs[i + 1][0], count, sites))
                    break
        out.sort(key=lambda x: (x[2], x[0]))
        return out

    dropped_keys = dropped(keys)
    dropped_refs = dropped(refs, skip=set(keys))   # a name the mod also writes is a key

    summary = [f"# Dependency Audit: {vocabs[0][0]} → {vocabs[-1][0]}"]
    if old_msg or new_msg:
        summary.append(f"*every tracked version up to {new_msg}*" if not fixed_window
                       else f"*{old_msg} → {new_msg}*")
    summary += [
        "",
        f"Checked **{len(keys)}** keys and **{len(refs)}** references the mod uses "
        f"against vanilla's vocabulary at {len(vocabs)} tracked versions.",
        f"- **{len(dropped_keys)}** keys the mod writes that vanilla no longer uses",
        f"- **{len(dropped_refs)}** names the mod references that vanilla no longer uses",
        "",
    ]
    print("\n".join(summary))

    if not dropped_keys and not dropped_refs:
        print("**No keys or references the mod uses were dropped by vanilla.**")
        return []

    def section(title, items, verb):
        if not items:
            return
        print(f"## {title}")
        print()
        for name, last, gone, count, sites in items:
            more = f" (+{len(sites) - 1} more)" if len(sites) > 1 else ""
            print(f"### {name}")
            print(f"- **Vanilla:** used {count} times at {last}, gone since {gone}")
            print(f"- **Mod {verb} it at:** `{sites[0]}`{more}")
            print()

    section("Keys the mod writes that vanilla no longer uses", dropped_keys, "writes")
    section("Names the mod references that vanilla no longer uses", dropped_refs, "references")

    findings = []
    for kind, use, items in (("deps_key_dropped", "key", dropped_keys),
                             ("deps_ref_dropped", "reference", dropped_refs)):
        for name, _last, gone, _count, sites in items:
            findings.append(Finding(kind, name, sites[0], f"dropped in {gone}", None,
                                    {"target": f"deps:{name}", "use": use}, gone))
    return findings

def _all_commits(vanilla_repo):
    from .tracker import get_commits
    return get_commits(vanilla_repo)

def names_defined_in_vanilla(vanilla_repo, commit, categories, names):
    """Subset of `names` that appear as an assignment target ('name =') anywhere
    in vanilla's .txt files under `categories` at `commit`, even when not as a
    top-level block. Lets the audit separate a target genuinely absent from
    vanilla (probably renamed or removed) from one the block matcher simply
    could not see, such as a nested definition. This is a name-existence check,
    not a structural parse."""
    names = set(names)
    if not names:
        return set()
    raw = _git_archive(vanilla_repo, commit, sorted(set(categories)))
    if not raw:
        return set()
    pats = {n: re.compile(rf"(?m)^\s*{re.escape(n)}\s*=") for n in names}
    found = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), ignore_zeros=True) as tf:
            for member in tf.getmembers():
                if not member.isfile() or not member.name.endswith(".txt"):
                    continue
                f = tf.extractfile(member)
                if f is None:
                    continue
                content = f.read().decode("utf-8-sig", errors="replace")
                for n in list(names - found):
                    if pats[n].search(content):
                        found.add(n)
                if found == names:
                    break
    except tarfile.TarError:
        return found
    return found

def vanilla_scalar_values(vanilla_repo, commit, categories, names):
    """{(category, name): value} for top-level single-value statements
    (`name = value`) in vanilla's .txt files under `categories` at `commit`."""
    names = set(names)
    out = {}
    if not names:
        return out
    raw = _git_archive(vanilla_repo, commit, sorted(set(categories)))
    if not raw:
        return out
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), ignore_zeros=True) as tf:
            for member in tf.getmembers():
                if not member.isfile() or not member.name.endswith(".txt"):
                    continue
                f = tf.extractfile(member)
                if f is None:
                    continue
                content = f.read().decode("utf-8-sig", errors="replace")
                if not any(n in content for n in names):
                    continue
                cat = str(Path(member.name).parent)
                for key, _op, val in parse(tokenize(content)):
                    if key in names and isinstance(val, str):
                        out.setdefault((cat, key), val)
    except tarfile.TarError:
        return out
    return out

def override_target(ov):
    return f"override:{ov['category']}/{ov['block']}"

def _tag(msg):
    parts = (msg or "").split()
    return parts[0] if parts else ""

def print_inject_section(items, show_diff, mod_root):
    if not items:
        return
    print(f"## Changed INJECT Targets: injection context changed ({len(items)} INJECT)")
    print()
    print("_INJECT adds your lines into the vanilla block; if vanilla reshaped "
          "that block, your lines can land in the wrong place._")
    print()
    for ov, old_entry, new_entry in items:
        vfile = (new_entry or old_entry or ("?",))[0]
        old_text = old_entry[1] if old_entry else None
        new_text = new_entry[1] if new_entry else None
        print(f"### {ov['block']}")
        print(f"- **Type:** {ov['type']}")
        print(f"- **Mod:** `{ov['file']}:{ov['line']}`")
        print(f"- **Vanilla:** `{vfile}`")
        _print_vanilla_change(old_text, new_text, ov["block"], show_diff)
        status, overlaps = inject_overlap(mod_root, ov, old_text, new_text)
        if status == "ok" and overlaps:
            shown = ", ".join(f"`{k}`" for k, _, _ in overlaps[:8])
            more = f" (+{len(overlaps) - 8} more)" if len(overlaps) > 8 else ""
            print(f"  **Injection-point collision:** ⚠ vanilla also defines or "
                  f"changed top-level {shown}{more}, the level this INJECT adds "
                  f"to; check for a duplicate or conflict")
        elif status == "ok":
            print("  **Injection point untouched:** vanilla changed other parts "
                  "of this block, not the keys this INJECT adds; the injection "
                  "still lands the same way")
        print()

def _print_vanilla_change(old_text, new_text, label, show_diff):
    if show_diff:
        d = diff_lines(old_text, new_text, label)
        if d:
            print("```diff")
            print("".join(d).rstrip("\n"))
            print("```")
        return
    if old_text is None:
        print("  *(new block, did not exist at the baseline)*")
    elif new_text is None:
        print("  *(removed from vanilla)*")
    else:
        n_add, n_rem, key = diff_summary(old_text, new_text)
        for line in key[:10]:
            print(line)
        if len(key) > 10:
            print(f"  ... and {len(key) - 10} more lines")
        print(f"  *({n_add} added, {n_rem} removed)*")

def line_key(target, r):
    """Fingerprint key for one three-way result (see ledger.finding_id)."""
    key = {"target": target, "path": list(r.path), "slot": r.key, "op": r.op,
           "ops": list(r.ops), "old": r.old, "new": r.new, "mod": r.mod}
    if r.old is None and r.new is None:
        key["text"] = r.text
    return key

def _result_label(r, block):
    if r.old is None and r.new is None:
        where = " > ".join(r.path)
        return f"{where} > {r.text}" if where else r.text
    label = " > ".join(list(r.path) + ([r.key] if r.key not in ("", "@item") else []))
    if r.key == "@item":
        label = f"{label} (list member)" if label else "(list member)"
    return label or block

def _print_classes(results, block, since, target, dismissed=frozenset()):
    def fid(r):
        return ledger.finding_id(Finding(CLASS_KIND[r.cls], block, "", "", None,
                                         line_key(target, r)))
    actionable = [r for r in results if r.cls in ACTIONABLE]
    hidden = sum(1 for r in actionable if fid(r) in dismissed)
    actionable = [r for r in actionable if fid(r) not in dismissed]
    first_id = None
    for cls, (sym, heading) in CLASS_HEADING.items():
        group = [r for r in actionable if r.cls == cls]
        if not group:
            continue
        print(f"  {sym} **{heading}**")
        for r in group[:12]:
            sid = ledger.short_id(fid(r))
            first_id = first_id or sid
            print(f"    [{sid}] `{_result_label(r, block)}`")
            for line in format_value_lines(r.cls, r.key, r.old, r.new, r.mod, r.ops,
                                           r.text, since, indent="        "):
                print(line)
        if len(group) > 12:
            print(f"    ... and {len(group) - 12} more")
    info = [r for r in results if r.cls not in ACTIONABLE]
    if info:
        counts = defaultdict(int)
        for r in info:
            counts[r.cls] += 1
        label = {"merged": "already merged", "commented_out": "commented out by you",
                 "key_removed": "key removed by you"}
        shown = ", ".join(f"{n} {label[c]}" for c, n in sorted(counts.items()))
        print(f"  ✓ **deliberate or already done:** {shown}")
    if hidden:
        print(f"  {hidden} dismissed line(s) hidden; list them with `pdx-audit --show-dismissed`")
    if not actionable:
        print("  ✓ **nothing to do in this block**")
    elif first_id:
        print(f'  To keep one as it is: `pdx-audit --dismiss {first_id} --reason "why"`')

def run_override_audit(mod_root, vanilla_repo, old_hash, old_msg, new_hash, new_msg, args, ctx=None):
    print(f"Scanning overrides in {mod_root.name}...", file=sys.stderr)
    overrides = find_overrides(mod_root)

    if args.block:
        overrides = [o for o in overrides if o["block"] == args.block]
    if args.category:
        overrides = [o for o in overrides if args.category in o["category"]]
    if ctx is not None:
        ctx.scanned["overrides"] = len(overrides)
    if not overrides:
        print("No matching overrides found.", file=sys.stderr)
        return []

    print(f"Found {len(overrides)} override directives.", file=sys.stderr)

    categories = sorted({o["category"] for o in overrides})
    new_tag = ctx.new_tag if ctx is not None else _tag(new_msg)
    old_tag = _tag(old_msg)
    fixed_window = ctx.fixed_window if ctx is not None else bool(
        getattr(args, "full", False) or getattr(args, "old", None) or getattr(args, "new", None))
    bases = ctx.bases if ctx is not None else {}

    old_idx = build_index_cached(vanilla_repo, old_hash, categories, f"old ({old_hash[:7]})")
    new_idx = build_index_cached(vanilla_repo, new_hash, categories, f"new ({new_hash[:7]})")

    unique, seen = [], set()
    for ov in overrides:
        dedup_key = (ov["type"], ov["category"], ov["block"])
        if dedup_key not in seen:
            seen.add(dedup_key)
            unique.append(ov)

    # Per-block history for REPLACE baselines (default window only).
    old_first, tags, history, old_index = [], [], {}, None
    if not fixed_window and ctx is not None:
        old_first = list(reversed(commits_up_to(ctx.commits, new_hash)))
        tags = [_tag(m) for _h, m in old_first]
        for i, (h, _m) in enumerate(old_first):
            if old_hash.startswith(h) or h.startswith(old_hash):
                old_index = i
        replace_keys = {(o["category"], o["block"]) for o in unique if o["type"] in REPLACE_TYPES}
        history = block_history(vanilla_repo, old_first, categories, replace_keys)

    index_memo = {}

    def index_at(tag):
        i = len(tags) - 1 - tags[::-1].index(tag)
        h = old_first[i][0]
        if h not in index_memo:
            index_memo[h] = build_index_cached(vanilla_repo, h, categories)
        return index_memo[h]

    def replace_baseline(ov, mod_block):
        """(base_text, vanilla_file, base_tag, how, since)."""
        key = (ov["category"], ov["block"])
        new_e = new_idx.get(key)
        if fixed_window or not history:
            e = old_idx.get(key)
            return (e[1] if e else None, (e or new_e or ("?",))[0], old_tag, "window",
                    new_tag)
        texts = history.get(key, [])
        tag = bases.get(override_target(ov))
        i, how = None, "window"
        if tag in tags:
            j = len(tags) - 1 - tags[::-1].index(tag)
            if texts[j] is not None:
                i, how = j, "recorded"
        if i is None and mod_block is not None:
            # Adoption is judged by the three-way classes: a patch counts as
            # adopted only where the block already merged it or deliberately
            # differs, so a value you customized never moves the baseline past
            # vanilla's change to it.
            def stats(prev, cur):
                results = classify(mod_block, prev or "", cur or "")
                done = sum(1 for r in results
                           if r.cls in ("merged", "commented_out", "key_removed"))
                return done, len(results)
            fork, _parts = detect_fork(mod_block, texts, stats=stats)
            if fork is not None:
                i, how = fork, "detected"
        if i is None:
            i = old_index
        if i is None:
            e = old_idx.get(key)
            return (e[1] if e else None, (e or new_e or ("?",))[0], old_tag, "window",
                    new_tag)
        nxt = first_change_after(texts, i, len(texts) - 1)
        since = tags[nxt] if nxt is not None else new_tag
        e = old_idx.get(key)
        vfile = (new_e or e or ("?",))[0]
        return texts[i], vfile, tags[i], how, since

    removed, changed_replace, changed_inject, unchanged, not_found = [], [], [], [], []
    replace_info = {}
    for ov in unique:
        key = (ov["category"], ov["block"])
        new_e = new_idx.get(key)
        if ov["type"] in REPLACE_TYPES:
            mod_block = extract_mod_block(mod_root, ov)
            base_text, vfile, base_tag, how, since = replace_baseline(ov, mod_block)
            replace_info[id(ov)] = (base_text, vfile, base_tag, how, since, mod_block)
            if base_text is not None and new_e is None:
                removed.append((ov, (vfile, base_text), None))
            elif base_text is None and new_e is None:
                not_found.append(ov)
            elif base_text is not None and _explode_norms(base_text) == _explode_norms(new_e[1]):
                unchanged.append(ov)
            else:
                changed_replace.append((ov, (vfile, base_text) if base_text is not None else None,
                                        new_e))
        else:
            target_tag = bases.get(override_target(ov)) if not fixed_window else None
            old_e = old_idx.get(key)
            if target_tag in tags and old_first:
                old_e = index_at(target_tag).get(key) or old_e
            if old_e and not new_e:
                removed.append((ov, old_e, None))
            elif not old_e and not new_e:
                not_found.append(ov)
            elif old_e and old_e[1].strip() == new_e[1].strip():
                unchanged.append(ov)
            else:
                changed_inject.append((ov, old_e, new_e))

    # Classify each changed REPLACE block three ways.
    classified = []   # (ov, old_entry, new_entry, results)
    for ov, old_e, new_e in changed_replace:
        base_text = old_e[1] if old_e else ""
        mod_block = replace_info[id(ov)][5]
        if mod_block is None:
            results = None
        elif old_e is not None and _explode_norms(base_text) == _explode_norms(new_e[1]):
            results = []
        else:
            results = classify(mod_block, base_text, new_e[1])
        classified.append((ov, old_e, new_e, results))

    # Single-value REPLACEs (`REPLACE:name = value`), which have no block to index.
    scalar_results, try_injects, defined_elsewhere, absent = [], [], [], []
    try_types = ("TRY_INJECT", "TRY_REPLACE")
    hard_misses = [o for o in not_found if o["type"] not in try_types]
    try_injects = [o for o in not_found if o["type"] in try_types]
    scalar_ovs = [(o, mod_scalar_value(mod_root, o)) for o in hard_misses
                  if o["type"] in REPLACE_TYPES]
    scalar_ovs = [(o, v) for o, v in scalar_ovs if v is not None]
    scalar_seen = set()
    if scalar_ovs:
        scalar_cats = {o["category"] for o, _ in scalar_ovs}
        scalar_names = {o["block"] for o, _ in scalar_ovs}
        value_memo = {}

        def values_at(commit):
            if commit not in value_memo:
                value_memo[commit] = vanilla_scalar_values(vanilla_repo, commit, scalar_cats,
                                                           scalar_names)
            return value_memo[commit]

        new_vals = values_at(new_hash)
        for ov, mod_val in scalar_ovs:
            key = (ov["category"], ov["block"])
            base_hash, base_tag, base_i = old_hash, old_tag, old_index
            tag = bases.get(override_target(ov)) if not fixed_window else None
            if tag in tags:
                base_i = len(tags) - 1 - tags[::-1].index(tag)
                base_hash, base_tag = old_first[base_i][0], tag
            base_val = values_at(base_hash).get(key)
            new_val = new_vals.get(key)
            if base_val is None and new_val is None:
                continue
            scalar_seen.add(id(ov))
            r = classify_scalar(mod_val, base_val, new_val)
            if r is None:
                continue
            since = new_tag
            if base_i is not None and old_first and not fixed_window:
                prev = base_val
                for h, m in old_first[base_i + 1:]:
                    cur = values_at(h).get(key)
                    if cur != prev:
                        since = _tag(m)
                        break
            scalar_results.append((ov, r, base_tag, since))
    hard_misses = [o for o in hard_misses if id(o) not in scalar_seen]
    if hard_misses:
        # A hard miss is only alarming if the name is truly gone from vanilla.
        present = names_defined_in_vanilla(
            vanilla_repo, new_hash, {o["category"] for o in hard_misses},
            {o["block"] for o in hard_misses})
        defined_elsewhere = [o for o in hard_misses if o["block"] in present]
        absent = [o for o in hard_misses if o["block"] not in present]

    actionable_lines = sum(1 for *_x, res in classified for r in (res or []) if r.cls in ACTIONABLE)
    unreadable = sum(1 for *_x, res in classified if res is None)
    actionable_lines += unreadable + sum(1 for _o, r, _b, _s in scalar_results if r.cls in ACTIONABLE)
    n_replace = sum(1 for o in unique if o["type"] in REPLACE_TYPES)
    n_inject = len(unique) - n_replace
    overlaps_found = 0

    header = ([f"# Override Audit: per-block baseline → {new_hash[:7]}",
               f"*each REPLACE compared against the vanilla version it was last synced "
               f"to → {new_msg}*"]
              if not fixed_window and history else
              [f"# Override Audit: {old_hash[:7]} → {new_hash[:7]}",
               f"*{old_msg} → {new_msg}*" if (old_msg or new_msg) else ""])
    summary = header + [
        "",
        f"**{len(unique)}** unique overrides scanned ({n_replace} REPLACE-type, {n_inject} INJECT-type)",
        f"- **{len(classified)}** REPLACE blocks vanilla changed since their baseline",
        f"- **{len(changed_inject)}** INJECT targets vanilla changed",
        f"- **{len(scalar_results)}** single-value REPLACEs vanilla changed",
        f"- **{len(removed)}** vanilla blocks removed: override orphaned",
        f"- **{len(absent)}** not found in vanilla, **{len(defined_elsewhere)}** defined but not as a block",
        f"- **{len(unchanged)}** unchanged",
        "",
    ]
    print("\n".join(summary))

    if removed:
        print("## Removed from Vanilla (orphaned overrides)")
        print()
        for ov, old_e, _ in removed:
            print(f"- **{ov['type']}:{ov['block']}**, "
                  f"`{ov['file']}:{ov['line']}` "
                  f"(was in `{old_e[0]}`)")
        print()

    if classified:
        print(f"## Changed REPLACE Blocks ({len(classified)} REPLACE)")
        print()
        print("_REPLACE swaps the whole vanilla block for your copy. Each vanilla change "
              "is compared three ways: vanilla before, your block, vanilla after._")
        print()
        for ov, old_e, new_e, results in classified:
            base_text, vfile, base_tag, how, since, _mb = replace_info[id(ov)]
            print(f"### {ov['block']}")
            print(f"- **Type:** {ov['type']}")
            print(f"- **Mod:** `{ov['file']}:{ov['line']}`")
            print(f"- **Vanilla:** `{vfile}`")
            print(f"- **Measured from:** {base_tag} *({how})*")
            _print_vanilla_change(old_e[1] if old_e else None, new_e[1], ov["block"], args.diff)
            if results is None:
                print("  **Mod status:** ⚠ your REPLACE block could not be read")
            else:
                _print_classes(results, ov["block"], since, override_target(ov),
                               getattr(ctx, "dismissed", frozenset()))
            print()

    if scalar_results:
        print(f"## Changed Single-Value REPLACEs ({len(scalar_results)})")
        print()
        for ov, r, base_tag, since in scalar_results:
            sym = CLASS_HEADING.get(r.cls, ("✓", ""))[0]
            print(f"- {sym} **{ov['block']}** `{ov['file']}:{ov['line']}` "
                  f"*({r.cls.replace('_', ' ')}, measured from {base_tag})*")
            for line in format_value_lines(r.cls, "", r.old, r.new, r.mod, r.ops,
                                           r.text, since, indent="    "):
                print(line)
        print()

    print_inject_section(changed_inject, args.diff, mod_root)

    if absent:
        print(f"## Not Found in Vanilla ({len(absent)})")
        print()
        print("No block, script value, or other `name =` definition by these "
              "names exists in vanilla at the new version. The target was "
              "probably renamed or removed, or the block is mod-only.")
        print()
        for ov in absent:
            print(f"- {ov['type']}:{ov['block']}, `{ov['file']}:{ov['line']}`")
        print()

    if defined_elsewhere:
        print(f"## Defined in Vanilla, but Not as a Top-Level Block "
              f"({len(defined_elsewhere)})")
        print()
        print("These names exist in vanilla but not as a top-level block or "
              "single value (most often a nested definition), so the audit cannot "
              "compare them. This is a limit of the matcher, not a broken override.")
        print()
        for ov in defined_elsewhere:
            print(f"- {ov['type']}:{ov['block']}, `{ov['file']}:{ov['line']}`")
        print()

    if try_injects:
        print(f"## TRY_* Overrides Not Found (expected, non-fixable) ({len(try_injects)})")
        print()
        for ov in try_injects:
            print(f"- {ov['type']}:{ov['block']} at `{ov['file']}:{ov['line']}`")
        print()

    # Findings for the cross-audit triage (the detail above is unchanged).
    want = getattr(args, "results_file", None)   # rich payload only for the --display app
    findings = []
    for ov, old_e, _ne in removed:
        base_tag = replace_info.get(id(ov), (None, None, old_tag))[2]
        findings.append(Finding("override_orphaned", ov["block"], f"{ov['file']}:{ov['line']}",
                                "", None, {"target": override_target(ov)}, new_tag, base_tag))
    for ov, old_e, new_e, results in classified:
        base_text, vfile, base_tag, how, since, mod_block = replace_info[id(ov)]
        loc = f"{ov['file']}:{ov['line']}"
        target = override_target(ov)
        data = None
        if want:
            data = override_report_data(mod_root, ov, True, old_e[1] if old_e else None,
                                        new_e[1], vfile, results or [])
        if results is None:
            findings.append(Finding("override_replace_unclassified", ov["block"], loc,
                                    "your REPLACE block could not be read", data,
                                    {"target": target, "unreadable": True}, since, base_tag))
            continue
        for r in results:
            kind = CLASS_KIND[r.cls]
            key = line_key(target, r)
            findings.append(Finding(kind, ov["block"], loc, line_detail(r, ov["block"]),
                                    data if r.cls in ACTIONABLE else None, key, since, base_tag))
            if r.cls in ACTIONABLE:
                data = None          # the block payload rides on its first actionable line
    for ov, r, base_tag, since in scalar_results:
        key = {"target": override_target(ov), "path": [], "slot": "", "op": r.op,
               "old": r.old, "new": r.new, "mod": r.mod}
        findings.append(Finding(CLASS_KIND[r.cls], ov["block"], f"{ov['file']}:{ov['line']}",
                                line_detail(r, ov["block"]), None, key, since, base_tag))
    for ov, old_e, new_e in changed_inject:
        loc = f"{ov['file']}:{ov['line']}"
        status, overlaps = inject_overlap(mod_root, ov, old_e[1] if old_e else None,
                                          new_e[1] if new_e else None)
        data = (override_report_data(mod_root, ov, False, old_e[1] if old_e else None,
                                     new_e[1] if new_e else None, (new_e or old_e)[0])
                if want else None)
        if status == "ok" and overlaps:
            overlaps_found += 1
            shown = ", ".join(k for k, _, _ in overlaps[:3]) + (
                " ..." if len(overlaps) > 3 else "")
            gap = hashlib.sha1(json.dumps([[k, o, n] for k, o, n in overlaps]).encode()).hexdigest()
            findings.append(Finding("override_inject_overlap", ov["block"], loc,
                                    f"top-level {shown}", data,
                                    {"target": override_target(ov), "overlap": gap}, new_tag,
                                    old_tag))
        else:
            # vanilla changed the block but not the keys this INJECT adds: the
            # injection still lands the same way, so this is informational.
            findings.append(Finding("override_inject_context", ov["block"], loc, "", data,
                                    {"target": override_target(ov)}))
    for ov in absent:
        findings.append(Finding("override_absent", ov["block"], f"{ov['file']}:{ov['line']}",
                                "", None, {"target": override_target(ov)}))
    for ov in defined_elsewhere:
        findings.append(Finding("override_nonblock", ov["block"], f"{ov['file']}:{ov['line']}",
                                "", None, {"target": override_target(ov)}))

    n_stale = sum(1 for f in findings if f.kind in (
        "override_replace_frozen", "override_replace_new_line", "override_replace_kept_removed"))
    n_review = sum(1 for f in findings if f.kind in (
        "override_replace_both_changed", "override_replace_unclassified"))
    if not n_stale and not n_review and not overlaps_found and not removed and not absent:
        print("**All overrides are current with vanilla.** No action needed.")
    else:
        print("---")
        print(f"**Action needed:** {n_stale} stale REPLACE lines, {n_review} to review, "
              f"{overlaps_found} INJECT collisions, {len(removed)} orphaned.")
        if not args.diff and (classified or changed_inject):
            print("Run with `--diff` for full unified diffs.")
    return findings
