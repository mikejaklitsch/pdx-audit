"""Script override audit and dependency audit."""

import re
import sys
import difflib
import tarfile
import io
import json
from pathlib import Path
from collections import defaultdict

from .report import diff_lines, diff_summary
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

def _enclosing_keys(lines):
    """For each line, the innermost enclosing block key (None at block top).
    Approximate but comment-aware; precise enough to classify a line's context."""
    stack, out = [], []
    for raw in lines:
        code = raw.split("#")[0]
        out.append(stack[-1] if stack else None)
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
    """Changed lines tagged with their enclosing block key.
    Returns (added, removed): lists of (norm_line, enclosing_key)."""
    a, b = old_text.split("\n"), new_text.split("\n")
    ak, bk = _enclosing_keys(a), _enclosing_keys(b)
    an, bn = [_norm(x) for x in a], [_norm(x) for x in b]
    sm = difflib.SequenceMatcher(None, an, bn, autojunk=False)
    added, removed = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            removed += [(an[i], ak[i]) for i in range(i1, i2) if an[i]]
        if tag in ("replace", "insert"):
            added += [(bn[j], bk[j]) for j in range(j1, j2) if bn[j]]
    return added, removed

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
    for n, key in added:
        if n not in mod_norms:
            if n not in missing:
                missing.append(n)
            continue
        cls = _classify_ctx(key, cat)
        if cls == "flow" and n not in inexact:
            inexact.append(n)
        elif cls == "unknown" and n not in review:
            review.append(n)
    for n, key in removed:
        if not n.strip("{} ") or n in new_norms:  # brace-only, or moved not removed
            continue
        if n not in mod_norms:  # correctly dropped
            continue
        cls = _classify_ctx(key, cat)
        if cls == "order_free":
            if n not in kept:
                kept.append(n)
        elif cls == "flow":
            if n not in inexact:
                inexact.append(n)
        elif n not in review:
            review.append(n)
    if missing or kept:
        state = "stale"
    elif review:
        state = "review"
    elif inexact:
        state = "inexact"
    else:
        state = "exact"
    return state, {"missing": missing, "kept": kept, "inexact": inexact, "review": review}

def inject_overlap(mod_root, ov, old_text, new_text):
    """For a changed INJECT target: keys set by both the mod's INJECT block and
    vanilla's changed lines. An overlap means vanilla touched the same key the
    mod overrides; the highest-priority kind of INJECT drift. No overlap does
    NOT mean safe: surrounding context changes can still matter.
    Returns (status, keys); status 'unknown' when there is nothing to compare."""
    if old_text is None or new_text is None:
        return "unknown", []
    block = extract_mod_block(mod_root, ov)
    if block is None:
        return "unknown", []
    mod_keys = set()
    for line in block.split("\n")[1:]:  # skip the INJECT: directive line
        m = IDENT_ASSIGN.match(line.split("#")[0])
        if m:
            mod_keys.add(m.group(1))
    if not mod_keys:
        return "unknown", []
    a = old_text.strip().splitlines()
    b = new_text.strip().splitlines()
    changed_keys = set()
    for l in difflib.unified_diff(a, b, n=0):
        if ((l.startswith("+") and not l.startswith("+++"))
                or (l.startswith("-") and not l.startswith("---"))):
            m = IDENT_ASSIGN.match(l[1:].split("#")[0])
            if m:
                changed_keys.add(m.group(1))
    return "ok", sorted(mod_keys & changed_keys)

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
    usage = mod_referenced_tokens(mod_root)
    print(f"Scanning {len(usage)} referenced identifiers in {mod_root.name}...",
          file=sys.stderr)
    old_vocab = build_vocab(vanilla_repo, old_hash, f"old ({old_hash[:7]})")
    new_vocab = build_vocab(vanilla_repo, new_hash, f"new ({new_hash[:7]})")
    if not old_vocab or not new_vocab:
        print("Could not read vanilla vocabulary (archive failed).", file=sys.stderr)
        sys.exit(1)

    dropped = []
    for tok, sites in usage.items():
        o = old_vocab.get(tok, 0)
        if o > 0 and new_vocab.get(tok, 0) == 0:
            dropped.append((tok, o, sites))
    dropped.sort(key=lambda x: -x[1])

    summary = [f"# Dependency Audit: {old_hash[:7]} → {new_hash[:7]}"]
    if old_msg or new_msg:
        summary.append(f"*{old_msg} → {new_msg}*")
    summary += [
        "",
        f"**{len(usage)}** referenced identifiers checked against vanilla vocabulary.",
        f"- **{len(dropped)}** used by the mod but dropped by vanilla; "
        f"verify (likely renamed/removed)",
        "",
    ]
    print("\n".join(summary))

    if not dropped:
        print("**No referenced vanilla tokens were dropped between these versions.**")
        print()
        print("_Checks `token =` assignments in the mod's .txt against vanilla usage. "
              "A token vanilla merely stopped using (but is still engine-valid) would "
              "also appear here, so treat hits as suspects and confirm with pdx-syntax._")
        return

    print("## Vanilla tokens the mod uses that vanilla dropped")
    print()
    for tok, o, sites in dropped:
        cands = rename_candidates(tok, old_vocab, new_vocab)
        more = f" (+{len(sites) - 1} more)" if len(sites) > 1 else ""
        print(f"### {tok}")
        print(f"- **Vanilla usage:** {o} → 0 between `{old_hash[:7]}` and `{new_hash[:7]}`")
        print(f"- **Mod uses it at:** `{sites[0]}`{more}")
        if cands:
            print(f"- **Rename candidates (new in vanilla):** {', '.join(cands)}")
        print()

    print("---")
    print("Suspects, not confirmed breakage: verify each with "
          "`pdx-syntax modifier|effect|trigger <name>`. Modifier keys fail silently "
          "(no error log), so they matter most; renamed effects/triggers also surface "
          "as data errors in error.log.")

def print_section(title, items, show_diff, is_replace, mod_root=None):
    if not items:
        return
    tag = "REPLACE" if is_replace else "INJECT"
    print(f"## {title} ({len(items)} {tag})")
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
                sys.stdout.write("".join(d))
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
            status, keys = inject_overlap(mod_root, ov, old_text, new_text)
            if status == "ok":
                if keys:
                    shown = ", ".join(f"`{k}`" for k in keys[:8])
                    more = f" (+{len(keys) - 8} more)" if len(keys) > 8 else ""
                    print(f"  **Inject overlap:** ⚠ vanilla changed keys this "
                          f"INJECT also sets: {shown}{more}")
                else:
                    print("  **Inject overlap:** changed lines do not set keys "
                          "this INJECT sets; context may still matter, review "
                          "the change")
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
        return

    print(f"Found {len(overrides)} override directives.", file=sys.stderr)

    categories = list({o["category"] for o in overrides})

    old_idx = build_index(vanilla_repo, old_hash, categories, f"old ({old_hash[:7]})")
    new_idx = build_index(vanilla_repo, new_hash, categories, f"new ({new_hash[:7]})")

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

    if not_found:
        try_types = ("TRY_INJECT", "TRY_REPLACE")
        try_injects = [o for o in not_found if o["type"] in try_types]
        hard_misses = [o for o in not_found if o["type"] not in try_types]

        if hard_misses:
            print(f"## Not Found in Vanilla ({len(hard_misses)})")
            print()
            print("These targets don't exist as top-level blocks in vanilla "
                  "(may be mod-only blocks, nested overrides, or category mismatches).")
            print()
            for ov in hard_misses:
                print(f"- {ov['type']}:{ov['block']}, `{ov['file']}:{ov['line']}`")
            print()

        if try_injects:
            print(f"## TRY_* Overrides Not Found (expected, non-fatal) ({len(try_injects)})")
            print()
            for ov in try_injects:
                print(f"- {ov['type']}:{ov['block']} at `{ov['file']}:{ov['line']}`")
            print()

    if args.include_unchanged and unchanged:
        print(f"## Unchanged ({len(unchanged)})")
        print()
        for ov, _, new_e in unchanged:
            print(f"- {ov['type']}:{ov['block']}, `{new_e[0]}`")
        print()

    if n_changed == 0 and not removed:
        print("**All overrides are current with vanilla.** No action needed.")
    else:
        print("---")
        review_note = f" (+{n_review} to review)" if n_review else ""
        print(f"**Action needed:** {n_stale} REPLACE blocks stale{review_note}, "
              f"{len(changed_inject)} INJECT targets shifted, "
              f"{len(removed)} orphaned.")
        if not args.diff and n_changed > 0:
            print("Run with `--diff` for full unified diffs.")
