"""Script override audit and dependency audit."""

import re
import sys
import tarfile
import io
import json
import hashlib
from pathlib import Path
from collections import defaultdict

from . import changes, diff3, ledger, session
from .gui import audit_window, commits_up_to
from .report import diff_lines, diff_summary, Finding
from .tracker import MODULE_ROOTS, _git_archive, cache_path, full_hash, tag_of
from .config import should_skip

REPLACE_TYPES = ("REPLACE", "TRY_REPLACE", "REPLACE_OR_CREATE")
# Directives whose target may be missing: a TRY_ override is then ignored, and an
# _OR_CREATE override creates the object.
MISSING_OK = ("TRY_INJECT", "TRY_REPLACE", "INJECT_OR_CREATE", "REPLACE_OR_CREATE")

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
            m = re.match(r"\s*(TRY_REPLACE|TRY_INJECT|REPLACE_OR_CREATE|INJECT_OR_CREATE|REPLACE|INJECT)"
                         r"\s*:\s*(\w+)", line)
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
    return cache_path(vanilla_repo, f"blocks-v{BLOCK_CACHE_VERSION}-{full}-{cat_key}.json")

def build_index_cached(vanilla_repo, commit, categories, progress_label=""):
    """Does the same as build_index, and also keeps the result on disk.

    An override run and a later --diff run read the same blocks. This function
    writes the parsed index into the cache folder of the tracker. The name of the
    cache file contains the full hash of the commit. The second run then uses the
    result of the first run. prune_cache deletes an entry when its commit is no
    longer in the tracker."""
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

def block_history(base, commits_old_first, categories, keys):
    """{(category, block): [text or None per commit]} oldest first, for just the
    wanted keys, loading one cached block index at a time."""
    from .base import as_base
    base = as_base(base)
    hist = {k: [] for k in keys}
    for i, (h, _msg) in enumerate(commits_old_first):
        idx = base.block_index(h, categories, f"history {i + 1}/{len(commits_old_first)} ({h[:7]})")
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
    return cache_path(vanilla_repo, f"vocab-v{VOCAB_CACHE_VERSION}-{full}.json")

def build_vocab(vanilla_repo, commit, label=""):
    """Returns the number of `token =` occurrences for each token, in the .txt files
    of vanilla at `commit`. The result goes into the cache folder of the tracker,
    with one entry for each commit."""
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
            cache.parent.mkdir(parents=True, exist_ok=True)
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

def run_deps_audit(mod_root, base, old_hash, old_msg, new_hash, new_msg, ctx=None):
    """Names the mod uses that the base used at some tracked version up to the new
    version but no longer uses at it, each with the patch that dropped it. The
    default run looks across every tracked version; a fixed window (--old,
    --full, or no run context) only looks from the old version onward. Names
    the mod itself defines at top level are its own and never flagged. `base` is a
    Base or the vanilla tracker's path."""
    from .base import as_base
    base = as_base(base)
    keys = mod_referenced_tokens(mod_root)
    refs = mod_referenced_values(mod_root)
    own = mod_top_level_names(mod_root)
    if ctx is not None:
        ctx.scanned["deps"] = len(keys) + len(refs)
    print(f"Scanning {len(keys)} keys and {len(refs)} references in {mod_root.name}...",
          file=sys.stderr)

    commits = ctx.commits if ctx is not None else base.commits()
    old_first = list(reversed(commits_up_to(commits, new_hash)))
    fixed_window = ctx.fixed_window if ctx is not None else True
    if fixed_window:
        for i, (h, _m) in enumerate(old_first):
            if old_hash.startswith(h) or h.startswith(old_hash):
                old_first = old_first[i:]
                break
    vocabs = []
    for i, (h, msg) in enumerate(old_first):
        v = base.vocab(h, f"vocabulary {i + 1}/{len(old_first)} ({h[:7]})")
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

def override_target(ov):
    return f"override:{ov['category']}/{ov['block']}"

def _tag(msg):
    return tag_of(msg)

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
            print(f"  **Injection-point collision:** ✗ vanilla also defines or "
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

def mod_scalar_text(mod_root, ov):
    """The line of a single-value override (`REPLACE:name = value`) with its prefix
    blanked to spaces, so offsets into it are columns of the mod's line; None when
    the override opens a block."""
    try:
        lines = _mod_lines(mod_root, ov["file"])
    except Exception:
        return None
    if not (0 < ov["line"] <= len(lines)):
        return None
    line = lines[ov["line"] - 1]
    m = re.match(r"\s*[A-Z_]+\s*:\s*", line)
    if not m:
        return None
    text = " " * m.end() + line[m.end():]
    top = diff3.nodes(text)
    if top and top[0].kind == "stmt" and top[0].key == ov["block"] and top[0].value[1] is not None:
        return text
    return None

def vanilla_scalar_values(vanilla_repo, commit, categories, names):
    """{(category, name): (vanilla file, statement text)} for top-level single-value
    statements (`name = value`) in vanilla's .txt files under `categories` at `commit`."""
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
                for n in diff3.nodes(content):
                    if n.kind == "stmt" and n.key in names and n.value[1] is not None:
                        out.setdefault((cat, n.key), (member.name, content[n.start:n.end]))
    except tarfile.TarError:
        return out
    return out

def _same_statements(a, b):
    """True when two texts read the same statement for statement."""
    return [n.sig for n in diff3.nodes(a)] == [n.sig for n in diff3.nodes(b)]

def _statements_of(text, keys, first_line):
    """{key: [(line number, line text)]} for every top-level statement of `text` whose
    key is in `keys`, numbering from `first_line`. A key can be written more than once
    in one block, and every one of them belongs to the injection point."""
    out = defaultdict(list)
    lines = (text or "").split("\n")
    for node in diff3.body(diff3.nodes(text or "")):
        if node.key not in keys:
            continue
        a = text.count("\n", 0, node.start)
        b = text.count("\n", 0, max(node.end - 1, node.start))
        out[node.key] += [(first_line + i, lines[i]) for i in range(a, b + 1)]
    return out

def inject_block(mod_root, ov, overlaps, finding, vanilla_file, vanilla_text=None):
    """The block view of an INJECT whose top-level keys vanilla also changed: each
    of your top-level statements with such a key is marked stale, since the key's
    final value is no longer what it was, with vanilla's current statement under it.

    `pairs` holds, for each of those keys, your statements and vanilla's current ones
    side by side. Only the keys you inject are there: the rest of vanilla's block is
    not an injection point, and your INJECT not holding it is not a difference."""
    text = extract_mod_block(mod_root, ov) or ""
    fid = ledger.finding_id(finding)
    new_of = {k: n for k, _o, n in overlaps}
    line_at = lambda offset: ov["line"] + text.count("\n", 0, offset)
    col = lambda offset: offset - (text.rfind("\n", 0, offset) + 1)
    rows = []
    for node in diff3.body(diff3.nodes(text)):
        if node.key not in new_of:
            continue
        rows.append({"kind": "inject_overlap", "mark": "stale", "fid": fid, "since": finding.since,
                     "first": line_at(node.start), "last": line_at(max(node.end - 1, node.start)),
                     "cols": [col(node.start), col(node.end)], "anchor": None, "inside": False,
                     "vanilla": new_of[node.key]})
    mine = _statements_of(text, set(new_of), ov["line"])
    theirs = _statements_of(vanilla_text, set(new_of), 1) if vanilla_text else {}
    pairs = [{"key": k, "fid": fid, "mark": "stale", "since": finding.since,
              "yours": mine.get(k, []), "vanilla": theirs.get(k, [])}
             for k in sorted(new_of) if mine.get(k) or theirs.get(k)]
    return {"type": "INJECT", "file": ov["file"], "line": ov["line"], "lines": text.split("\n"),
            "vanilla_file": vanilla_file, "changes": rows, "pairs": pairs}

def run_override_audit(mod_root, base, old_hash, old_msg, new_hash, new_msg, args, ctx=None):
    """Compare each REPLACE with the base's versions of its block from the window's
    start through the new version, and check each INJECT's injection point. `base`
    is a Base or the vanilla tracker's path."""
    from .base import as_base
    base = as_base(base)
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
    want = bool(getattr(args, "results_file", None))
    dismissed = getattr(ctx, "dismissed", frozenset())

    unique, seen = [], set()
    for ov in overrides:
        dedup_key = (ov["type"], ov["category"], ov["block"])
        if dedup_key not in seen:
            seen.add(dedup_key)
            unique.append(ov)
    replaces = [o for o in unique if o["type"] in REPLACE_TYPES]
    injects = [o for o in unique if o["type"] not in REPLACE_TYPES]

    window = audit_window(base, old_hash, new_hash, args, ctx)
    tags = [_tag(m) for _h, m in window]
    new_idx = base.block_index(new_hash, categories, f"new ({new_hash[:7]})")
    history = block_history(base, window, categories,
                            {(o["category"], o["block"]) for o in replaces}) if replaces else {}

    removed, changed_replace, unchanged, not_found, unreadable = [], [], [], [], []
    for ov in replaces:
        key = (ov["category"], ov["block"])
        texts = history[key]
        if texts[-1] is None:
            present = [i for i, t in enumerate(texts) if t is not None]
            if not present:
                not_found.append(ov)
                continue
            was = base.block_index(window[present[-1]][0], categories).get(key)
            removed.append((ov, (was or ("?",))[0], tags[present[-1] + 1], None))
            continue
        mod_block = extract_mod_block(mod_root, ov)
        if mod_block is None:
            unreadable.append(ov)
            continue
        vfile = (new_idx.get(key) or ("?",))[0]
        result = changes.audit("override", ov["block"], override_target(ov), mod_block, texts, tags,
                               ov["file"], ov["line"], unwrap=True, want=want, block_type="REPLACE",
                               vanilla_file=vfile)
        if result.flagged:
            changed_replace.append((ov, vfile, texts, result))
        else:
            unchanged.append(ov)

    # INJECT targets compare the window's old version, or an open finding's base,
    # with the new version. A change is dated by the first version after that one
    # whose block differs.
    changed_inject, inject_when = [], {}
    if injects:
        old_idx = base.block_index(old_hash, categories, f"old ({old_hash[:7]})")
        all_first = list(reversed(commits_up_to(ctx.commits, new_hash))) if ctx is not None else []
        all_tags = [_tag(m) for _h, m in all_first]
        old_pos = next((i for i, (h, _m) in enumerate(all_first)
                        if old_hash.startswith(h) or h.startswith(old_hash)), None)

        def dated(key, start, old_e):
            for j in range(start + 1, len(all_first)):
                e = base.block_index(all_first[j][0], categories).get(key)
                if not e or not old_e or not _same_statements(old_e[1], e[1]):
                    return all_tags[j]
            return new_tag

        for ov in injects:
            key = (ov["category"], ov["block"])
            new_e, old_e = new_idx.get(key), old_idx.get(key)
            start, base_tag = old_pos, old_tag
            target_tag = None if fixed_window else bases.get(override_target(ov))
            if target_tag in all_tags:
                start, base_tag = len(all_tags) - 1 - all_tags[::-1].index(target_tag), target_tag
                old_e = base.block_index(all_first[start][0], categories).get(key) or old_e
            since = dated(key, start, old_e) if start is not None and old_e else new_tag
            if old_e and not new_e:
                removed.append((ov, old_e[0], since, base_tag))
            elif not old_e and not new_e:
                not_found.append(ov)
            elif old_e and _same_statements(old_e[1], new_e[1]):
                unchanged.append(ov)
            else:
                changed_inject.append((ov, old_e, new_e))
                inject_when[id(ov)] = (start, base_tag)

    def overlap_since(ov, old_e):
        """The first version after the INJECT's start whose block changes a key it injects."""
        start, key = inject_when[id(ov)][0], (ov["category"], ov["block"])
        if start is None or not old_e:
            return new_tag
        for j in range(start + 1, len(all_first)):
            e = base.block_index(all_first[j][0], categories).get(key)
            status, found = inject_overlap(mod_root, ov, old_e[1], e[1] if e else None)
            if status == "ok" and found:
                return all_tags[j]
        return new_tag

    # Single-value REPLACEs (`REPLACE:name = value`), which have no block to index.
    hard_misses = list(not_found)
    scalar_ovs = [(o, mod_scalar_text(mod_root, o)) for o in hard_misses if o["type"] in REPLACE_TYPES]
    scalar_ovs = [(o, t) for o, t in scalar_ovs if t is not None]
    scalar_results, scalar_seen = [], set()
    if scalar_ovs:
        values = [base.scalar_values(h, {o["category"] for o, _ in scalar_ovs}, {o["block"] for o, _ in scalar_ovs})
                  for h, _m in window]
        for ov, text in scalar_ovs:
            key = (ov["category"], ov["block"])
            entries = [v.get(key) for v in values]
            present = [i for i, e in enumerate(entries) if e]
            if not present:
                continue
            scalar_seen.add(id(ov))
            if entries[-1] is None:
                removed.append((ov, entries[present[-1]][0], tags[present[-1] + 1], None))
                continue
            texts = [e[1] if e else None for e in entries]
            result = changes.audit("override", ov["block"], override_target(ov), text, texts, tags,
                                   ov["file"], ov["line"], want=want, block_type="REPLACE",
                                   vanilla_file=entries[-1][0])
            if result.block is not None:
                result.block["lines"] = [_mod_lines(mod_root, ov["file"])[ov["line"] - 1]]
            if result.flagged:
                scalar_results.append((ov, entries[-1][0], texts, result))
            else:
                unchanged.append(ov)
    hard_misses = [o for o in hard_misses if id(o) not in scalar_seen]
    expected_misses = [o for o in hard_misses if o["type"] in MISSING_OK]
    hard_misses = [o for o in hard_misses if o["type"] not in MISSING_OK]
    defined_elsewhere, absent = [], []
    if hard_misses:
        # A hard miss is only alarming if the name is truly gone from vanilla.
        present = base.names_defined(new_hash, {o["category"] for o in hard_misses},
                                     {o["block"] for o in hard_misses})
        defined_elsewhere = [o for o in hard_misses if o["block"] in present]
        absent = [o for o in hard_misses if o["block"] not in present]

    # An _OR_CREATE override whose target vanilla removed creates the object instead
    # of being orphaned.
    created = [r for r in removed if r[0]["type"].endswith("_OR_CREATE")]
    removed = [r for r in removed if not r[0]["type"].endswith("_OR_CREATE")]

    fixed = iter(changes.distinct([item[-1] for item in changed_replace + scalar_results]))
    changed_replace = [(*item[:-1], next(fixed)) for item in changed_replace]
    scalar_results = [(*item[:-1], next(fixed)) for item in scalar_results]

    n_changes =sum(len(item[-1].flagged) for item in changed_replace + scalar_results)
    summary = [
        f"# Override Audit: {tags[0]} → {tags[-1]}",
        f"*each REPLACE compared with vanilla's {len(window)} tracked versions up to {new_msg}*",
        "",
        f"**{len(unique)}** unique overrides scanned ({len(replaces)} REPLACE-type, "
        f"{len(injects)} INJECT-type)",
        f"- **{len(changed_replace)}** REPLACE blocks with vanilla changes to take or check "
        f"({sum(len(item[-1].flagged) for item in changed_replace)} changes)",
        f"- **{len(changed_inject)}** INJECT targets vanilla changed",
        f"- **{len(scalar_results)}** single-value REPLACEs vanilla changed",
        f"- **{len(removed)}** vanilla blocks removed: override orphaned",
        f"- **{len(created)}** vanilla blocks removed that an _OR_CREATE override now creates",
        f"- **{len(absent)}** not found in vanilla, **{len(defined_elsewhere)}** defined but not as a block",
        f"- **{len(unchanged)}** current with vanilla",
        "",
    ]
    print("\n".join(summary))

    if removed:
        print("## Removed from Vanilla (orphaned overrides)")
        print()
        for ov, vfile, since, _base in removed:
            print(f"- **{ov['type']}:{ov['block']}**, `{ov['file']}:{ov['line']}` "
                  f"(was in `{vfile}`, removed in {since})")
        print()

    if created:
        print(f"## Removed from Vanilla, Now Created by the Override ({len(created)})")
        print()
        for ov, vfile, since, _base in created:
            print(f"- **{ov['type']}:{ov['block']}**, `{ov['file']}:{ov['line']}` "
                  f"(was in `{vfile}`, removed in {since}); the override now creates it")
        print()

    if unreadable:
        print(f"## Unreadable REPLACE Blocks ({len(unreadable)})")
        print()
        for ov in unreadable:
            print(f"- ✗ **{ov['type']}:{ov['block']}**, `{ov['file']}:{ov['line']}`: its braces never close")
        print()

    if changed_replace:
        print(f"## Changed REPLACE Blocks ({len(changed_replace)} REPLACE)")
        print()
        print("_REPLACE swaps the whole vanilla block for your copy. Each difference is "
              "attributed with vanilla's history: a change vanilla made that your copy "
              "lacks, or one that meets an edit of yours._")
        print()
        for ov, vfile, texts, result in changed_replace:
            changes.print_target(ov["block"], [("Type", ov["type"]), ("Mod", f"`{ov['file']}:{ov['line']}`"),
                                               ("Vanilla", f"`{vfile}`")],
                                 result, texts, tags, args.diff, dismissed)

    if scalar_results:
        print(f"## Changed Single-Value REPLACEs ({len(scalar_results)})")
        print()
        for ov, vfile, texts, result in scalar_results:
            changes.print_target(ov["block"], [("Type", ov["type"]), ("Mod", f"`{ov['file']}:{ov['line']}`"),
                                               ("Vanilla", f"`{vfile}`")],
                                 result, texts, tags, args.diff, dismissed)

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

    if expected_misses:
        print(f"## Targets Vanilla Does Not Define (expected for TRY_ and _OR_CREATE) "
              f"({len(expected_misses)})")
        print()
        for ov in expected_misses:
            effect = "creates it" if ov["type"].endswith("_OR_CREATE") else "ignored"
            print(f"- {ov['type']}:{ov['block']} at `{ov['file']}:{ov['line']}`: {effect}")
        print()

    findings = []
    for ov, _vfile, since, base in removed:
        findings.append(Finding("override_orphaned", ov["block"], f"{ov['file']}:{ov['line']}",
                                "", None, {"target": override_target(ov)}, since, base))
    for ov, _vfile, since, base in created:
        findings.append(Finding("override_now_created", ov["block"], f"{ov['file']}:{ov['line']}",
                                "", None, {"target": override_target(ov)}, since, base))
    for ov in unreadable:
        findings.append(Finding("override_unreadable", ov["block"], f"{ov['file']}:{ov['line']}",
                                "", None, {"target": override_target(ov)}))
    for *_item, result in changed_replace + scalar_results:
        findings += result.findings
    overlaps_found = 0
    for ov, old_e, new_e in changed_inject:
        loc = f"{ov['file']}:{ov['line']}"
        status, overlaps = inject_overlap(mod_root, ov, old_e[1] if old_e else None,
                                          new_e[1] if new_e else None)
        if status == "ok" and overlaps:
            overlaps_found += 1
            shown = ", ".join(k for k, _, _ in overlaps[:3]) + (
                " ..." if len(overlaps) > 3 else "")
            gap = hashlib.sha1(json.dumps([[k, o, n] for k, o, n in overlaps]).encode()).hexdigest()
            f = Finding("override_inject_overlap", ov["block"], loc, f"top-level {shown}", None,
                        {"target": override_target(ov), "overlap": gap}, overlap_since(ov, old_e),
                        inject_when[id(ov)][1])
            if want:
                f = f._replace(data=inject_block(mod_root, ov, overlaps, f, (new_e or old_e)[0],
                                                 new_e[1] if new_e else None))
            findings.append(f)
        else:
            # vanilla changed the block but not the keys this INJECT adds: the
            # injection still lands the same way, so this is informational.
            findings.append(Finding("override_inject_context", ov["block"], loc, "", None,
                                    {"target": override_target(ov)}))
    for ov in absent:
        findings.append(Finding("override_absent", ov["block"], f"{ov['file']}:{ov['line']}",
                                "", None, {"target": override_target(ov)}))
    for ov in defined_elsewhere:
        findings.append(Finding("override_nonblock", ov["block"], f"{ov['file']}:{ov['line']}",
                                "", None, {"target": override_target(ov)}))

    if (not n_changes and not overlaps_found and not removed and not created and not absent
            and not unreadable):
        print("**All overrides are current with vanilla.** No action needed.")
    else:
        print("---")
        print(f"**Action needed:** {n_changes} REPLACE changes to take or check, "
              f"{overlaps_found} INJECT collisions, {len(removed)} orphaned.")
        if not args.diff and (changed_replace or changed_inject):
            print("Run with `--diff` for vanilla's changes since each copy's version.")
    return findings
