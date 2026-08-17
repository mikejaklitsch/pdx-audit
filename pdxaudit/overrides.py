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

from .report import diff_lines, diff_summary, Finding
from .tracker import MODULE_ROOTS, _git_archive, git
from .config import should_skip

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
    for fp in sorted(mod_root.rglob("*")):
        if fp.suffix not in (".txt", ".gui"):
            continue
        rel = fp.relative_to(mod_root)
        if rel.parts[0] not in MODULE_ROOTS or should_skip(rel):
            continue
        try:
            text = fp.read_text(encoding="utf-8-sig")
        except Exception:
            continue
        for ln, line in enumerate(text.split("\n"), 1):
            m = re.match(r"\s*(TRY_REPLACE|TRY_INJECT|REPLACE|INJECT)\s*:\s*(\w+)", line)
            if m:
                results.append({
                    "type": m.group(1),
                    "block": m.group(2),
                    "file": str(rel),
                    "line": ln,
                    "category": str(rel.parent),
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
    full = git(vanilla_repo, "rev-parse", commit).strip()
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

def extract_mod_block(mod_root, ov):
    """Extract the body of a mod override block (REPLACE:/INJECT:name = { ... })."""
    try:
        lines = (mod_root / ov["file"]).read_text(encoding="utf-8-sig").split("\n")
    except Exception:
        return None
    start = ov["line"] - 1
    if not (0 <= start < len(lines)):
        return None
    return _brace_extract(lines, start)

def _norm(line):
    """Normalize a script line for containment comparison: strip inline comment,
    collapse whitespace, canonicalize spacing around '=' so `a=b` and `a = b`
    compare equal. Comment-only / blank lines normalize to ''."""
    s = re.sub(r"\s+", " ", line.split("#")[0])
    s = re.sub(r"\s*=\s*", " = ", s)
    return s.strip()

FLOW_KEYS = {
    "limit", "trigger", "allow", "potential", "is_shown", "visible", "filter",
    "effect", "immediate", "option", "if", "else", "else_if", "elseif",
    "while", "switch", "random", "random_list", "hidden_effect",
    "complex_effect", "and", "or", "not", "nand", "nor", "calc_true_if",
    "count", "trigger_if", "trigger_else", "trigger_else_if",
}

FLOW_PREFIXES = ("every_", "random_", "ordered_", "any_")

ORDER_FREE_DIRS = {"static_modifiers", "defines", "modifier_type_definitions"}

def _is_flow_key(key):
    if not key:
        return False
    kl = key.lower()
    if kl in FLOW_KEYS or kl.startswith(FLOW_PREFIXES):
        return True
    return ":" in kl  # scope shift, e.g. scope:actor, c:FRA

def _order_free_category(file_path):
    return any(p in ORDER_FREE_DIRS for p in re.split(r"[\\/]", file_path))

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
    aware; approximate but precise enough to place and classify a line."""
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

def _changed_line_ctxs(old_text, new_text):
    """Changed lines tagged with their enclosing block path.
    Returns (added, removed): lists of (norm_line, path_tuple)."""
    a, b = old_text.split("\n"), new_text.split("\n")
    ap, bp = _enclosing_paths(a), _enclosing_paths(b)
    an, bn = [_norm(x) for x in a], [_norm(x) for x in b]
    sm = difflib.SequenceMatcher(None, an, bn, autojunk=False)
    added, removed = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            removed += [(an[i], ap[i]) for i in range(i1, i2) if an[i]]
        if tag in ("replace", "insert"):
            added += [(bn[j], bp[j]) for j in range(j1, j2) if bn[j]]
    return added, removed

def _breadcrumb(path, norm):
    """A changed line shown with the path to the sub-block it sits in, so a bare
    `estate_building_input` reads as `possible_production_methods[estate_building_input]`
    rather than a token with no home. The outermost element (the override block
    itself) is dropped; '*' marks an anonymous block."""
    rel = list(path[1:])
    return f"{' > '.join(rel)}[{norm}]" if rel else norm

def _classify_ctx(key, cat_order_free):
    """'order_free' | 'flow' | 'unknown' for a changed line's enclosing block."""
    if _is_flow_key(key):
        return "flow"
    if key is None:  # direct child of the override block
        return "order_free" if cat_order_free else "flow"
    kl = key.lower()
    if kl == "modifier" or kl.endswith("_modifier") or kl in ("game_data", "ai_will_do", "weight"):
        return "order_free"
    return "unknown"

def replace_reconciliation(mod_root, ov, old_text, new_text):
    """Graded verdict on how a changed REPLACE relates to vanilla's change.
    Returns (state, detail); state is one of:
      'exact'   mod reflects the change and every changed line sits in an
                order-free block, so membership proves it is in the right place
      'inexact' mod carries the changed lines but at least one is in a
                position-sensitive block (limit/trigger/effect/script value),
                so we cannot confirm it lands in the right sub-block
      'review'  a changed line is in a block we could not classify
      'stale'   a vanilla-added line is absent from the mod, or a removed line
                is still carried in an order-free block
      'unknown' mod block or vanilla text unavailable
    detail holds the per-bucket line lists for reporting."""
    if old_text is None or new_text is None:
        return "unknown", {}
    block = extract_mod_block(mod_root, ov)
    if block is None:
        return "unknown", {}
    mod_norms = {_norm(l) for l in block.split("\n")}
    new_norms = {_norm(l) for l in new_text.split("\n")}
    cat = _order_free_category(ov["file"])
    added, removed = _changed_line_ctxs(old_text, new_text)
    missing, kept, inexact, review = [], [], [], []
    for n, path in added:
        bc = _breadcrumb(path, n)
        if n not in mod_norms:
            if bc not in missing:
                missing.append(bc)
            continue
        cls = _classify_ctx(path[-1] if path else None, cat)
        if cls == "flow" and bc not in inexact:
            inexact.append(bc)
        elif cls == "unknown" and bc not in review:
            review.append(bc)
    for n, path in removed:
        if not n.strip("{} ") or n in new_norms:  # brace-only, or moved not removed
            continue
        if n not in mod_norms:  # correctly dropped
            continue
        bc = _breadcrumb(path, n)
        cls = _classify_ctx(path[-1] if path else None, cat)
        if cls == "order_free":
            if bc not in kept:
                kept.append(bc)
        elif cls == "flow":
            if bc not in inexact:
                inexact.append(bc)
        elif bc not in review:
            review.append(bc)
    if missing or kept:
        state = "stale"
    elif review:
        state = "review"
    elif inexact:
        state = "inexact"
    else:
        state = "exact"
    return state, {"missing": missing, "kept": kept, "inexact": inexact, "review": review}

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

def override_report_data(mod_root, ov, is_replace, old_text, new_text, vanilla_file):
    """The --display payload for one changed REPLACE/INJECT finding: vanilla's
    diff, the mod block as a 3-way patch, and the gap (missing/kept for REPLACE,
    injection-point overlaps for INJECT)."""
    n_add, n_rem, _ = diff_summary(old_text, new_text)
    data = {
        "type": "REPLACE" if is_replace else "INJECT",
        "vanilla_file": vanilla_file,
        "n_add": n_add, "n_rem": n_rem,
        "diff": "".join(diff_lines(old_text, new_text, ov["block"])).rstrip("\n"),
        "missing": [], "kept": [], "overlap": [],
    }
    if is_replace:
        _state, det = replace_reconciliation(mod_root, ov, old_text, new_text)
        data["missing"], data["kept"] = det.get("missing", []), det.get("kept", [])
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
    for fp in sorted(mod_root.rglob("*.txt")):
        rel = fp.relative_to(mod_root)
        if not rel.parts or rel.parts[0] not in MODULE_ROOTS or should_skip(rel):
            continue
        try:
            text = fp.read_text(encoding="utf-8-sig")
        except Exception:
            continue
        for ln, raw in enumerate(text.split("\n"), 1):
            m = IDENT_ASSIGN.match(raw.split("#")[0])
            if m:
                usage[m.group(1)].append(f"{rel}:{ln}")
    return usage

RHS_IDENT = re.compile(r"^\s*[A-Za-z][A-Za-z0-9_]*\s*=\s*([A-Za-z][A-Za-z0-9_]*)\s*$")
RHS_SKIP = {"yes", "no"}

def mod_referenced_values(mod_root):
    """value -> ['file:line', ...] for every `key = value` line in the mod's
    .txt scripts whose value is a single bare identifier: a name the mod points
    at, as opposed to a number, a block, or a boolean."""
    usage = defaultdict(list)
    for fp in sorted(mod_root.rglob("*.txt")):
        rel = fp.relative_to(mod_root)
        if not rel.parts or rel.parts[0] not in MODULE_ROOTS or should_skip(rel):
            continue
        try:
            text = fp.read_text(encoding="utf-8-sig")
        except Exception:
            continue
        for ln, raw in enumerate(text.split("\n"), 1):
            m = RHS_IDENT.match(raw.split("#")[0])
            if m and m.group(1) not in RHS_SKIP:
                usage[m.group(1)].append(f"{rel}:{ln}")
    return usage

VOCAB_CACHE_VERSION = 1

def _vocab_cache_path(vanilla_repo, commit):
    """Cache file for a commit's vocabulary, keyed by the full commit hash;
    commit content is immutable, so entries never go stale. The version bumps
    when the tokenizer changes."""
    full = git(vanilla_repo, "rev-parse", commit).strip()
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

def rename_candidates(token, old_vocab, new_vocab, limit=5):
    """Tokens new to vanilla@new (absent at old) that share a stem with the
    dropped token: likely rename targets. Ranked by shared-prefix length;
    stems shorter than 5 chars are noise, not renames."""
    cands = []
    for t, c in new_vocab.items():
        if t == token or old_vocab.get(t, 0) > 0:
            continue
        cp = 0
        for x, y in zip(t, token):
            if x != y:
                break
            cp += 1
        if cp < 5:
            continue
        cands.append((cp, c, t))
    cands.sort(reverse=True)
    return [t for _, _, t in cands[:limit]]

def run_deps_audit(mod_root, vanilla_repo, old_hash, old_msg, new_hash, new_msg):
    keys = mod_referenced_tokens(mod_root)
    refs = mod_referenced_values(mod_root)
    print(f"Scanning {len(keys)} keys and {len(refs)} references in {mod_root.name}...",
          file=sys.stderr)
    old_vocab = build_vocab(vanilla_repo, old_hash, f"old ({old_hash[:7]})")
    new_vocab = build_vocab(vanilla_repo, new_hash, f"new ({new_hash[:7]})")
    if not old_vocab or not new_vocab:
        print("Could not read vanilla vocabulary (archive failed).", file=sys.stderr)
        sys.exit(1)

    def dropped(usage, skip=frozenset()):
        out = [(name, old_vocab[name], sites)
               for name, sites in usage.items()
               if name not in skip and old_vocab.get(name, 0) > 0
               and new_vocab.get(name, 0) == 0]
        out.sort(key=lambda x: -x[1])
        return out

    dropped_keys = dropped(keys)
    dropped_refs = dropped(refs, skip=set(keys))   # a name the mod also writes is a key

    summary = [f"# Dependency Audit: {old_hash[:7]} → {new_hash[:7]}"]
    if old_msg or new_msg:
        summary.append(f"*{old_msg} → {new_msg}*")
    summary += [
        "",
        f"Checked **{len(keys)}** keys and **{len(refs)}** references the mod uses "
        f"against vanilla's vocabulary.",
        f"- **{len(dropped_keys)}** keys the mod writes that vanilla dropped",
        f"- **{len(dropped_refs)}** names the mod references that vanilla dropped",
        "",
    ]
    print("\n".join(summary))

    if not dropped_keys and not dropped_refs:
        print("**No keys or references the mod uses were dropped between these versions.**")
        return []

    def section(title, items, verb):
        if not items:
            return
        print(f"## {title}")
        print()
        for name, o, sites in items:
            cands = rename_candidates(name, old_vocab, new_vocab)
            more = f" (+{len(sites) - 1} more)" if len(sites) > 1 else ""
            print(f"### {name}")
            print(f"- **Vanilla usage:** {o} → 0 between `{old_hash[:7]}` and `{new_hash[:7]}`")
            print(f"- **Mod {verb} it at:** `{sites[0]}`{more}")
            if cands:
                print(f"- **Rename candidates (new in vanilla):** {', '.join(cands)}")
            print()

    section("Keys the mod writes that vanilla dropped", dropped_keys, "writes")
    section("Names the mod references that vanilla dropped", dropped_refs, "references")

    findings = []
    for name, _o, sites in dropped_keys:
        cands = rename_candidates(name, old_vocab, new_vocab)
        findings.append(Finding("deps_key_dropped", name, sites[0],
                                f"maybe {cands[0]}?" if cands else ""))
    for name, _o, sites in dropped_refs:
        cands = rename_candidates(name, old_vocab, new_vocab)
        findings.append(Finding("deps_ref_dropped", name, sites[0],
                                f"maybe {cands[0]}?" if cands else ""))
    return findings

def names_defined_in_vanilla(vanilla_repo, commit, categories, names):
    """Subset of `names` that appear as an assignment target ('name =') anywhere
    in vanilla's .txt files under `categories` at `commit`, even when not as a
    top-level block. Lets the audit separate a target genuinely absent from
    vanilla (probably renamed or removed) from one the block matcher simply
    could not see, such as a script value or a nested definition. This is a
    name-existence check, not a structural parse."""
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

def print_section(title, items, show_diff, is_replace, mod_root=None):
    if not items:
        return
    tag = "REPLACE" if is_replace else "INJECT"
    print(f"## {title} ({len(items)} {tag})")
    print()
    gloss = ("REPLACE swaps the whole vanilla block for your copy, so anything "
             "vanilla later adds to that block is dropped unless you copy it in."
             if is_replace else
             "INJECT adds your lines into the vanilla block; if vanilla reshaped "
             "that block, your lines can land in the wrong place.")
    print(f"_{gloss}_")
    print()

    for ov, old_entry, new_entry in items:
        old_file = old_entry[0] if old_entry else None
        new_file = new_entry[0] if new_entry else None
        vfile = new_file or old_file or "?"

        print(f"### {ov['block']}")
        print(f"- **Type:** {ov['type']}")
        print(f"- **Mod:** `{ov['file']}:{ov['line']}`")
        print(f"- **Vanilla:** `{vfile}`")

        old_text = old_entry[1] if old_entry else None
        new_text = new_entry[1] if new_entry else None

        if show_diff:
            d = diff_lines(old_text, new_text, ov["block"])
            if d:
                print("```diff")
                print("".join(d).rstrip("\n"))
                print("```")
        else:
            n_add, n_rem, key = diff_summary(old_text, new_text)
            if old_text is None:
                print("  *(new block, did not exist pre-patch)*")
            elif new_text is None:
                print("  *(removed from vanilla)*")
            else:
                preview = key[:10]
                for line in preview:
                    print(line)
                remaining = len(key) - len(preview)
                if remaining > 0:
                    print(f"  ... and {remaining} more lines")
                print(f"  *({n_add} added, {n_rem} removed)*")

        if is_replace and mod_root is not None:
            state, det = replace_reconciliation(mod_root, ov, old_text, new_text)

            def _lines(bucket):
                for m in bucket[:10]:
                    print(f"      `{m}`")
                if len(bucket) > 10:
                    print(f"      ... and {len(bucket) - 10} more")

            if state == "exact":
                print("  **Mod status:** ✓ exact change already present")
            elif state == "inexact":
                print("  **Mod status:** ≈ change present but may not be exact; "
                      "position-sensitive context, review:")
                _lines(det["inexact"])
            elif state == "review":
                print("  **Mod status:** ? cannot confirm change matches current "
                      "state, review:")
                _lines(det["review"])
            elif state == "stale":
                if det["missing"]:
                    print("  **Mod status:** ✗ replacement is MISSING vanilla lines:")
                    _lines(det["missing"])
                if det["kept"]:
                    print("  **Mod status:** ✗ replacement still carries lines vanilla removed:")
                    _lines(det["kept"])
                for label, bucket in (("may not be exact", det["inexact"]),
                                      ("cannot confirm", det["review"])):
                    if bucket:
                        print(f"  **Also ({label}):**")
                        _lines(bucket)

        if not is_replace and mod_root is not None:
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

def run_override_audit(mod_root, vanilla_repo, old_hash, old_msg, new_hash, new_msg, args):
    print(f"Scanning overrides in {mod_root.name}...", file=sys.stderr)
    overrides = find_overrides(mod_root)

    if args.block:
        overrides = [o for o in overrides if o["block"] == args.block]
    if args.category:
        overrides = [o for o in overrides if args.category in o["category"]]
    if not overrides:
        print("No matching overrides found.", file=sys.stderr)
        return []

    print(f"Found {len(overrides)} override directives.", file=sys.stderr)

    categories = list({o["category"] for o in overrides})

    old_idx = build_index_cached(vanilla_repo, old_hash, categories, f"old ({old_hash[:7]})")
    new_idx = build_index_cached(vanilla_repo, new_hash, categories, f"new ({new_hash[:7]})")

    removed = []
    changed_replace = []
    changed_inject = []
    unchanged = []
    not_found = []

    seen = set()
    for ov in overrides:
        key = (ov["category"], ov["block"])
        dedup_key = (ov["type"], key)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        old_e = old_idx.get(key)
        new_e = new_idx.get(key)

        if old_e and not new_e:
            removed.append((ov, old_e, None))
        elif not old_e and not new_e:
            not_found.append(ov)
        elif not old_e and new_e:
            entry = (ov, None, new_e)
            if ov["type"] in ("REPLACE", "TRY_REPLACE"):
                changed_replace.append(entry)
            else:
                changed_inject.append(entry)
        else:
            old_text = old_e[1].strip()
            new_text = new_e[1].strip()
            if old_text == new_text:
                unchanged.append((ov, old_e, new_e))
            else:
                entry = (ov, old_e, new_e)
                if ov["type"] in ("REPLACE", "TRY_REPLACE"):
                    changed_replace.append(entry)
                else:
                    changed_inject.append(entry)

    n_changed = len(changed_replace) + len(changed_inject)

    replace_states = [
        replace_reconciliation(mod_root, ov_e, oe[1] if oe else None,
                               ne[1] if ne else None)[0]
        for ov_e, oe, ne in changed_replace
    ]
    n_stale = replace_states.count("stale")
    n_review = replace_states.count("inexact") + replace_states.count("review")
    n_exact = replace_states.count("exact")

    n_replace = sum(1 for t, _ in seen if t in ("REPLACE", "TRY_REPLACE"))
    n_inject = len(seen) - n_replace
    summary = [f"# Override Audit: {old_hash[:7]} → {new_hash[:7]}"]
    if old_msg or new_msg:
        summary.append(f"*{old_msg} → {new_msg}*")
    summary += [
        "",
        f"**{len(seen)}** unique overrides scanned ({n_replace} REPLACE-type, {n_inject} INJECT-type)",
        f"- **{n_changed}** vanilla blocks changed: action needed",
        f"- **{len(removed)}** vanilla blocks removed: override orphaned",
        f"- **{len(not_found)}** not found in vanilla (mod-only or nested)",
        f"- **{len(unchanged)}** unchanged",
    ]
    if changed_replace:
        summary.append(f"  - changed REPLACE reconciliation: **{n_stale}** stale, "
                       f"**{n_review}** need review, **{n_exact}** already exact")
    summary.append("")
    print("\n".join(summary))

    if removed:
        print("## Removed from Vanilla (orphaned overrides)")
        print()
        for ov, old_e, _ in removed:
            print(f"- **{ov['type']}:{ov['block']}**, "
                  f"`{ov['file']}:{ov['line']}` "
                  f"(was in `{old_e[0]}`)")
        print()

    print_section(
        "Changed REPLACE Blocks: mod is suppressing new vanilla content",
        changed_replace, args.diff, is_replace=True, mod_root=mod_root,
    )

    print_section(
        "Changed INJECT Targets: injection context changed",
        changed_inject, args.diff, is_replace=False, mod_root=mod_root,
    )

    try_injects, defined_elsewhere, absent = [], [], []
    if not_found:
        try_types = ("TRY_INJECT", "TRY_REPLACE")
        try_injects = [o for o in not_found if o["type"] in try_types]
        hard_misses = [o for o in not_found if o["type"] not in try_types]

        if hard_misses:
            # A hard miss is only alarming if the name is truly gone from vanilla.
            # A name that exists but not as a top-level block (a script value, a
            # nested definition) is a matcher limit, not a broken override, so
            # keep the two apart instead of lumping them under one scary heading.
            present = names_defined_in_vanilla(
                vanilla_repo, new_hash,
                {o["category"] for o in hard_misses},
                {o["block"] for o in hard_misses})
            defined_elsewhere = [o for o in hard_misses if o["block"] in present]
            absent = [o for o in hard_misses if o["block"] not in present]

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
            print("These names exist in vanilla but not as a top-level "
                  "`name = {{ ... }}` block (most often a script value or a nested "
                  "definition), so the block audit cannot compare them. This is a "
                  "limit of the matcher, not a broken override.")
            print()
            for ov in defined_elsewhere:
                print(f"- {ov['type']}:{ov['block']}, `{ov['file']}:{ov['line']}`")
            print()

        if try_injects:
            print(f"## TRY_* Overrides Not Found (expected, non-fatal) ({len(try_injects)})")
            print()
            for ov in try_injects:
                print(f"- {ov['type']}:{ov['block']} at `{ov['file']}:{ov['line']}`")
            print()

    if n_changed == 0 and not removed and not absent:
        print("**All overrides are current with vanilla.** No action needed.")
    else:
        print("---")
        review_note = f" (+{n_review} to review)" if n_review else ""
        print(f"**Action needed:** {n_stale} REPLACE blocks stale{review_note}, "
              f"{len(changed_inject)} INJECT targets shifted, "
              f"{len(removed)} orphaned.")
        if not args.diff and n_changed > 0:
            print("Run with `--diff` for full unified diffs.")

    # Findings for the cross-audit triage (the detail above is unchanged).
    # Each finding is one item tagged with its problem class; the class supplies
    # the shared wording and remedy in report.KIND.
    want = getattr(args, "display", None)   # attach rich payload only for --display
    findings = []
    for ov, _oe, _ne in removed:
        findings.append(Finding("override_orphaned", ov["block"],
                                f"{ov['file']}:{ov['line']}"))
    for (ov, oe, ne), state in zip(changed_replace, replace_states):
        loc = f"{ov['file']}:{ov['line']}"
        kind = ("override_replace_stale" if state == "stale"
                else "override_replace_review" if state in ("review", "inexact")
                else "override_replace_reconciled")
        data = (override_report_data(mod_root, ov, True, oe[1] if oe else None,
                                     ne[1] if ne else None, (ne or oe)[0])
                if want else None)
        findings.append(Finding(kind, ov["block"], loc, "", data))
    for ov, oe, ne in changed_inject:
        loc = f"{ov['file']}:{ov['line']}"
        status, overlaps = inject_overlap(mod_root, ov, oe[1] if oe else None,
                                          ne[1] if ne else None)
        data = (override_report_data(mod_root, ov, False, oe[1] if oe else None,
                                     ne[1] if ne else None, (ne or oe)[0])
                if want else None)
        if status == "ok" and overlaps:
            shown = ", ".join(k for k, _, _ in overlaps[:3]) + (
                " ..." if len(overlaps) > 3 else "")
            findings.append(Finding("override_inject_overlap", ov["block"], loc,
                                    f"top-level {shown}", data))
        else:
            # vanilla changed the block but not the keys this INJECT adds: the
            # injection still lands the same way, so this is informational.
            findings.append(Finding("override_inject_context", ov["block"], loc, "", data))
    for ov in absent:
        findings.append(Finding("override_absent", ov["block"],
                                f"{ov['file']}:{ov['line']}"))
    for ov in defined_elsewhere:
        findings.append(Finding("override_nonblock", ov["block"],
                                f"{ov['file']}:{ov['line']}"))
    return findings
