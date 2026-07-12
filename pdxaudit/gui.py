"""GUI override audit: shadowing, fork points, pins, stamping."""

import re
import sys
import difflib
import tarfile
import io
import json
import hashlib
from pathlib import Path

from .report import diff_lines, diff_summary
from .tracker import MODULE_ROOTS, _git_archive, get_commits, git

def _norm_clean(line):
    """Whitespace / '='-spacing canonicalization for an already comment-free
    line (see _explode_braces, which strips comments up front)."""
    s = re.sub(r"\s+", " ", line)
    s = re.sub(r"\s*=\s*", " = ", s)
    return s.strip()

def _explode_braces(text):
    """Split `text` so each '{' and '}' outside strings and comments sits on its
    own logical line. Makes single-line (`a = { b }`) and multi-line brace
    formatting compare equal, so a formatter collapsing short blocks onto one
    line cannot masquerade as drift. String- and comment-aware: '#' outside a
    string starts a comment to end of line; braces and '#' inside a "..." literal
    are preserved as content (GUI colour codes like "#R ...#!" stay intact)."""
    out, buf = [], []
    in_str = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if in_str:
            buf.append(c)
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            buf.append(c)
        elif c == '#':  # comment: drop to end of line
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        elif c == '\n':
            out.append("".join(buf)); buf = []
        elif c in "{}":
            out.append("".join(buf)); buf = []
            out.append(c)
        else:
            buf.append(c)
        i += 1
    out.append("".join(buf))
    return out

def _explode_norms(text):
    """Normalized, non-empty logical lines of `text`, brace-granularity
    independent (short `{ }` blocks exploded onto their own lines)."""
    return [n for n in (_norm_clean(l) for l in _explode_braces(text)) if n]

def _containment(mod_text, old_text, new_text):
    """Compare a mod block against vanilla's old→new change by normalized-line
    containment. Returns (missing, kept):
      missing: vanilla-added code lines absent from the mod block
      kept: code lines vanilla removed outright that the mod still carries
                (lines still present anywhere in new vanilla text are moves,
                not removals, and are not flagged; bare-brace lines skipped)
    Brace formatting is normalized on both sides (short `{ }` blocks exploded
    onto their own lines) so a formatter collapsing blocks onto a single line
    cannot read as drift. Comment-only lines are ignored on both sides."""
    mod_norms = set(_explode_norms(mod_text))
    a = _explode_norms(old_text)
    b = _explode_norms(new_text)
    new_norms = set(b)
    d = list(difflib.unified_diff(a, b, n=0))
    added = [l[1:] for l in d if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:] for l in d if l.startswith("-") and not l.startswith("---")]
    missing, kept = [], []
    for n in added:
        if n and n.strip("{} ") and n not in mod_norms and n not in missing:
            missing.append(n)
    for n in removed:
        if (n and n.strip("{} ") and n not in new_norms
                and n in mod_norms and n not in kept):
            kept.append(n)
    return missing, kept

GUI_DEF_HEAD = re.compile(r"^\s*(template|local_template|types)\s+([A-Za-z_][\w.]*)")

GUI_TYPE_HEAD = re.compile(r"^\s*type\s+([A-Za-z_][\w.]*)\s*=")

def _gui_code(line):
    """Code portion of a .gui line: comment stripped, string contents blanked
    (quotes kept), so braces inside comments and strings never count."""
    out = []
    in_str = False
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
                out.append('"')
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append('"')
        elif ch == "#":
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out)

def parse_gui_defs(text):
    """Extract template/local_template/types/type definitions from .gui content.
    Line-granular: headers sharing a line with a second definition are missed,
    and a definition's text may include a trailing brace that closed its parent
    on the same line; both are stable across commits, so comparisons hold.
    Returns (defs, clean). clean=False flags brace anomalies vanilla actually
    ships: a stray extra closer (engine-tolerated; accounting resyncs at 0) or
    a block left open at EOF (its definition is dropped). Emitted defs closed
    properly and are trustworthy either way."""
    lines = text.split("\n")
    defs = []
    depth = 0
    clean = True
    open_defs = []  # {kind, name, start, base, opened}
    for i, raw in enumerate(lines):
        code = _gui_code(raw)
        if depth == 0:
            m = GUI_DEF_HEAD.match(code)
            if m:
                # a still-braceless def at depth 0 was malformed; drop it
                open_defs = [d for d in open_defs if d["opened"]]
                open_defs.append({"kind": m.group(1), "name": m.group(2),
                                  "start": i, "base": 0, "opened": False})
        elif depth == 1 and open_defs and open_defs[0]["kind"] == "types":
            m = GUI_TYPE_HEAD.match(code)
            if m:
                open_defs.append({"kind": "type", "name": m.group(1),
                                  "start": i, "base": 1, "opened": False})
        o, c = code.count("{"), code.count("}")
        if o:
            for d in open_defs:
                d["opened"] = True
        depth += o - c
        while open_defs and open_defs[-1]["opened"] and depth <= open_defs[-1]["base"]:
            d = open_defs.pop()
            defs.append({"kind": d["kind"], "name": d["name"], "line": d["start"] + 1,
                         "text": "\n".join(lines[d["start"]:i + 1])})
        if depth < 0:
            depth = 0
            clean = False
    if depth != 0 or any(d["opened"] for d in open_defs):
        clean = False
    return defs, clean

def mod_gui_files(mod_root):
    """[(rel_path, text), ...] for the mod's .gui files under <module>/gui/."""
    out = []
    for fp in sorted(mod_root.rglob("*.gui")):
        rel = fp.relative_to(mod_root)
        if len(rel.parts) < 3 or rel.parts[0] not in MODULE_ROOTS or rel.parts[1] != "gui":
            continue
        try:
            out.append((str(rel), fp.read_text(encoding="utf-8-sig")))
        except Exception:
            continue
    return out

def build_gui_vanilla(vanilla_repo, commit, modules, label=""):
    """Parse all vanilla .gui under the given modules' gui/ dirs at `commit`.
    Returns (def_idx, file_idx, bad):
      def_idx: (module, kind, name) -> (vfile, text) for template/type;
                 on duplicate names the first file in path-sorted order wins,
                 approximating first-loaded-wins
      file_idx: vfile -> full text (for same-path replacement checks)
      bad: vfiles with brace anomalies (defs still indexed best-effort)"""
    dirs = [f"{m}/gui" for m in sorted(modules)]
    if label:
        print(f"  {label}: extracting {len(dirs)} gui directories...",
              end="", file=sys.stderr, flush=True)
    def_idx, file_idx, bad = {}, {}, []
    raw = _git_archive(vanilla_repo, commit, dirs)
    if not raw:
        if label:
            print(" failed!", file=sys.stderr)
        return def_idx, file_idx, bad
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), ignore_zeros=True) as tf:
            members = [m for m in tf.getmembers()
                       if m.isfile() and m.name.endswith(".gui")]
            for member in sorted(members, key=lambda m: m.name.lower()):
                f = tf.extractfile(member)
                if f is None:
                    continue
                content = f.read().decode("utf-8-sig", errors="replace")
                vfile = member.name
                file_idx[vfile] = content
                defs, clean = parse_gui_defs(content)
                if not clean:
                    bad.append(vfile)
                module = vfile.split("/", 1)[0]
                for d in defs:
                    if d["kind"] not in ("template", "type"):
                        continue
                    key = (module, d["kind"], d["name"])
                    if key not in def_idx:
                        def_idx[key] = (vfile, d["text"])
    except tarfile.TarError:
        if label:
            print(" tar error!", file=sys.stderr)
        return def_idx, file_idx, bad
    if label:
        print(f" {len(def_idx)} defs in {len(file_idx)} files.", file=sys.stderr)
    return def_idx, file_idx, bad

GUI_CACHE_VERSION = 1

def _gui_cache_path(vanilla_repo, commit, modules):
    """Cache file for a commit's parsed GUI index, keyed by the full commit hash
    and the module set. Commit content is immutable, so entries never go stale;
    the version bumps when the parser or index shape changes."""
    full = git(vanilla_repo, "rev-parse", commit).strip()
    if not full:
        return None
    mod_key = hashlib.sha1(",".join(sorted(modules)).encode()).hexdigest()[:12]
    return Path(vanilla_repo).parent / "cache" / \
        f"gui-v{GUI_CACHE_VERSION}-{full}-{mod_key}.json"

def build_gui_vanilla_cached(vanilla_repo, commit, modules, label=""):
    """build_gui_vanilla with a per-commit disk cache. Fork detection reads the
    GUI index at every tracked commit; without caching that whole-history scan
    would fall on every audit, so the parsed index is memoized under
    <vanilla-tracker>/cache/ keyed by the immutable commit hash."""
    cache = _gui_cache_path(vanilla_repo, commit, modules)
    if cache and cache.is_file():
        try:
            data = json.loads(cache.read_text())
            def_idx = {tuple(k): (v[0], v[1]) for k, v in data["defs"]}
            if label:
                print(f"  {label}: gui index from cache "
                      f"({len(def_idx)} defs).", file=sys.stderr)
            return def_idx, data["files"], data["bad"]
        except (OSError, ValueError, KeyError, IndexError):
            pass
    def_idx, file_idx, bad = build_gui_vanilla(vanilla_repo, commit, modules, label)
    if cache:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "defs": [[list(k), [v[0], v[1]]] for k, v in def_idx.items()],
                "files": file_idx,
                "bad": bad,
            }
            cache.write_text(json.dumps(payload))
        except OSError:
            pass
    return def_idx, file_idx, bad

def _loads_after_vanilla(mod_file, vfile):
    """True when the mod .gui file does not sort before the vanilla file
    defining the same name. First-loaded definition wins and load order follows
    path sort (hence the aaa_ prefix convention), so a mod file sorting after
    vanilla's probably never applies. Heuristic; verify in game."""
    return mod_file.lower() >= vfile.lower()

def _def_level_changes(old_text, new_text):
    """Definition names that changed/appeared/disappeared between two versions
    of one .gui file."""
    od, _ = parse_gui_defs(old_text)
    nd, _ = parse_gui_defs(new_text)
    om = {(d["kind"], d["name"]): d["text"] for d in od}
    nm = {(d["kind"], d["name"]): d["text"] for d in nd}
    changed = sorted(k for k in om if k in nm and om[k].strip() != nm[k].strip())
    added = sorted(k for k in nm if k not in om)
    removed = sorted(k for k in om if k not in nm)
    return changed, added, removed

def _print_containment(mod_text, old_text, new_text, subject):
    missing, kept = _containment(mod_text, old_text, new_text)
    if not missing and not kept:
        print(f"  **Mod status:** ✓ {subject} already contains vanilla's change")
        return
    if missing:
        print(f"  **Mod status:** ✗ {subject} is MISSING vanilla lines:")
        for m in missing[:10]:
            print(f"      `{m}`")
        if len(missing) > 10:
            print(f"      ... and {len(missing) - 10} more")
    if kept:
        print(f"  **Mod status:** ✗ {subject} still carries lines vanilla removed:")
        for m in kept[:10]:
            print(f"      `{m}`")
        if len(kept) > 10:
            print(f"      ... and {len(kept) - 10} more")

def _text_distance(a, b):
    """Lightweight similarity distance between two block/file texts: the number
    of changed (added + removed) lines under a normalized line diff. Lower means
    more similar. None on either side is treated as maximally distant."""
    if a is None or b is None:
        return 10 ** 9
    add, rem, _ = diff_summary(a, b)
    return add + rem

FORK_PIN_RE = re.compile(r"#\s*pdx-audit\s+fork-point\s*:\s*(\S+)", re.IGNORECASE)

def parse_fork_pin(text, max_lines=20):
    """Return the version token from a '# pdx-audit fork-point: <version>'
    comment near the top of a mod .gui file, or None. Only the first few lines
    are scanned so the marker cannot be picked up from deep inside the file."""
    for line in text.splitlines()[:max_lines]:
        m = FORK_PIN_RE.search(line)
        if m:
            return m.group(1)
    return None

def resolve_pin_commit(vanilla_repo, token, commits):
    """Resolve a fork-point pin token (version tag or commit hash) to a tracked
    (hash, msg) pair, or None when it matches no snapshot."""
    resolved = git(vanilla_repo, "rev-parse", "--verify", "--quiet",
                   f"{token}^{{commit}}").strip()
    if resolved:
        for h, msg in commits:
            if resolved.startswith(h) or h.startswith(resolved):
                return (h, msg)
    for h, msg in commits:
        if msg.split() and msg.split()[0] == token:
            return (h, msg)
    return None

def build_fork_baselines(vanilla_repo, commits, modules, mdefs, mod_file_texts):
    """Per-file fork-point detection for the GUI audit.

    For each mod shadow definition and each same-path replacement file, pick the
    baseline vanilla version to measure drift from. A file carrying a
    '# pdx-audit fork-point: <version>' pin uses that version outright;
    otherwise the tracked commit whose text is *closest* to the mod's copy
    (fewest changed lines) is taken as the version the mod was forked from.
    Measuring drift from the fork point forward, instead of from a fixed oldest
    commit, avoids re-reporting vanilla changes the mod already copied in (a
    wide --full window otherwise reports the entire history of vanilla changes,
    most of which the copy already contains).

    Returns (def_base, file_base, pin_errors, pin_stale):
      def_base: (module, kind, name) -> (vfile, text, fork_hash, fork_msg, pinned)
      file_base: rel -> (text, fork_hash, fork_msg, pinned)
      pin_errors: rel -> unresolvable pin token
      pin_stale: rel -> (pinned_tag, detected_tag) when a pin no longer matches
                   the file's contents"""
    all_idx = {}
    for i, (h, _msg) in enumerate(commits):
        di, fi, _ = build_gui_vanilla_cached(
            vanilla_repo, h, modules,
            f"fork scan {i + 1}/{len(commits)} ({h[:7]})")
        all_idx[h] = (di, fi)

    pins, pin_errors = {}, {}
    for rel, text in mod_file_texts.items():
        token = parse_fork_pin(text)
        if not token:
            continue
        hit = resolve_pin_commit(vanilla_repo, token, commits)
        if hit:
            pins[rel] = hit
        else:
            pin_errors[rel] = token

    def _closest(target, getter):
        best = None
        for h, msg in commits:
            t = getter(all_idx[h])
            if t is None:
                continue
            dist = _text_distance(target, t)
            if best is None or dist < best[0]:
                best = (dist, h, msg, t)
        return best

    def_base = {}
    for d in mdefs:
        key = (d["module"], d["kind"], d["name"])
        if key in def_base:
            continue
        pin = pins.get(d.get("file"))
        if pin:
            ph, pmsg = pin
            v = all_idx[ph][0].get(key)
            if v:
                def_base[key] = (v[0], v[1], ph, pmsg, True)
            continue
        best = _closest(d["text"], lambda idx, k=key: (idx[0].get(k) or (None, None))[1])
        if best:
            _dist, h, msg, _t = best
            v = all_idx[h][0].get(key)
            def_base[key] = (v[0], v[1], h, msg, False)

    file_base = {}
    pin_stale = {}
    for rel, mtext in mod_file_texts.items():
        pin = pins.get(rel)
        if pin:
            ph, pmsg = pin
            t = all_idx[ph][1].get(rel)
            if t is not None:
                file_base[rel] = (t, ph, pmsg, True)
                # A pin is authoritative, but detect whether the file's contents
                # have moved on from it (e.g. a mod updated to a newer patch
                # without updating the pin) and flag the pin as possibly stale.
                best = _closest(mtext, lambda idx, r=rel: idx[1].get(r))
                if best and best[1] != ph and best[0] < _text_distance(mtext, t):
                    pin_stale[rel] = (pmsg.split()[0] if pmsg else "?",
                                      best[2].split()[0] if best[2] else "?")
            continue
        best = _closest(mtext, lambda idx, r=rel: idx[1].get(r))
        if best:
            _dist, h, msg, t = best
            file_base[rel] = (t, h, msg, False)
    return def_base, file_base, pin_errors, pin_stale

def run_gui_audit(mod_root, vanilla_repo, old_hash, old_msg, new_hash, new_msg, args):
    files = mod_gui_files(mod_root)
    if not files:
        print("No mod .gui files found.", file=sys.stderr)
        sys.exit(0)

    mdefs = []
    skipped = 0  # file-scoped kinds: local_template, types containers
    for rel, text in files:
        defs, clean = parse_gui_defs(text)
        if not clean:
            print(f"Warning: unbalanced braces in mod file {rel}; "
                  f"definitions parsed best-effort", file=sys.stderr)
        module = rel.split("/", 1)[0]
        for d in defs:
            if d["kind"] in ("template", "type"):
                d["file"] = rel
                d["module"] = module
                mdefs.append(d)
            else:
                skipped += 1
    if args.block:
        mdefs = [d for d in mdefs if d["name"] == args.block]

    modules = sorted({rel.split("/", 1)[0] for rel, _ in files})
    print(f"Scanning {len(mdefs)} GUI definitions in {len(files)} mod .gui files...",
          file=sys.stderr)
    old_idx, old_files, bad_old = build_gui_vanilla_cached(
        vanilla_repo, old_hash, modules, f"old ({old_hash[:7]})")
    new_idx, new_files, bad_new = build_gui_vanilla_cached(
        vanilla_repo, new_hash, modules, f"new ({new_hash[:7]})")
    if not new_idx and not new_files:
        print("Could not read vanilla .gui files (archive failed).", file=sys.stderr)
        sys.exit(1)
    if bad_old or bad_new:
        bad_files = sorted(set(bad_old) | set(bad_new))
        print(f"Warning: {len(bad_files)} vanilla .gui file(s) have unbalanced "
              f"braces (vanilla's own); parsed best-effort:", file=sys.stderr)
        for vf in bad_files:
            print(f"  {vf}", file=sys.stderr)

    # Fork-point baselines are the default: each override is compared against the
    # tracked vanilla version it was forked from (per-file pin or nearest match),
    # so drift is measured from that point forward. An explicit fixed window
    # (--full / --old / --new) opts out and compares against old_hash instead.
    fixed_window = bool(args.full or args.old or args.new)
    fork_defs, fork_files, pin_errors, pin_stale = {}, {}, {}, {}
    if not fixed_window:
        mod_file_texts = {rel: text for rel, text in files}
        fork_defs, fork_files, pin_errors, pin_stale = build_fork_baselines(
            vanilla_repo, get_commits(vanilla_repo), modules, mdefs, mod_file_texts)
        for rel, token in sorted(pin_errors.items()):
            print(f"Warning: {rel} pins fork-point '{token}', which matches no "
                  f"tracked snapshot; falling back to auto-detection.",
                  file=sys.stderr)
        for rel, (pinned_tag, detected_tag) in sorted(pin_stale.items()):
            print(f"Warning: {rel} pins fork-point {pinned_tag}, but its contents "
                  f"look closer to {detected_tag}; the pin may be stale "
                  f"(--stamp-fork-points --refresh updates it).", file=sys.stderr)

    # Files that replace a vanilla file at the same path are audited at file
    # level below; their definitions are replacements, not shadows.
    same_path = {rel for rel, _ in files if rel in old_files or rel in new_files}

    changed, new_coll, van_removed, unchanged = [], [], [], []
    mod_only = 0
    for d in mdefs:
        if d["file"] in same_path:
            continue
        key = (d["module"], d["kind"], d["name"])
        if fork_defs:
            fb = fork_defs.get(key)
            o = (fb[0], fb[1]) if fb else None
        else:
            o = old_idx.get(key)
        n = new_idx.get(key)
        if not o and not n:
            mod_only += 1
        elif o and not n:
            van_removed.append((d, o))
        elif not o and n:
            new_coll.append((d, n))
        elif o[1].strip() == n[1].strip():
            unchanged.append((d, n))
        else:
            changed.append((d, o, n))

    # Split changed shadows by whether the mod copy already carries vanilla's
    # change. A definition vanilla edited whose mod shadow already reflects the
    # edit is reconciled, not drift, and must not be reported as action-needed.
    stale_changed, reconciled_changed = [], []
    for d, o, n in changed:
        missing, kept = _containment(d["text"], o[1], n[1])
        (stale_changed if (missing or kept) else reconciled_changed).append((d, o, n))

    replaced = []
    same_path_unchanged = []
    for rel, _text in files:
        if fork_files:
            fb = fork_files.get(rel)
            ot = fb[0] if fb else None
        else:
            ot = old_files.get(rel)
        nt = new_files.get(rel)
        if ot is None and nt is None:
            continue
        if ot is None or nt is None or ot.strip() != nt.strip():
            replaced.append((rel, ot, nt))
        else:
            same_path_unchanged.append(rel)

    if not fixed_window:
        header = [
            f"# GUI Override Audit: per-file fork point → {new_hash[:7]}",
            f"*each override compared against the vanilla version it was forked "
            f"from → {new_msg}*",
        ]
    else:
        header = [f"# GUI Override Audit: {old_hash[:7]} → {new_hash[:7]} (fixed window)"]
        if old_msg or new_msg:
            header.append(f"*{old_msg} → {new_msg}*")
    header.append("")
    print("\n".join(header))

    if fork_files:
        print("## Detected Fork Points")
        print()
        print("The version each replaced file was measured against. `pinned` means "
              "the file carries a `# pdx-audit fork-point:` comment; the rest are "
              "auto-detected as the closest tracked version. Pin a file to lock its "
              "baseline (see `--stamp-fork-points`).")
        print()
        for rel in sorted(fork_files):
            _t, _h, fmsg, pinned = fork_files[rel]
            tag = fmsg.split()[0] if fmsg else "?"
            print(f"- `{rel}`: {tag}" + ("  *(pinned)*" if pinned else ""))
        print()
    shadow_defs = [d for d in mdefs if d["file"] not in same_path]
    n_tmpl = sum(1 for d in shadow_defs if d["kind"] == "template")
    n_type = len(shadow_defs) - n_tmpl
    extras = []
    if len(mdefs) > len(shadow_defs):
        extras.append(f"{len(mdefs) - len(shadow_defs)} in same-path files "
                      f"audited at file level")
    if skipped:
        extras.append(f"{skipped} file-scoped skipped")
    counts = [
        f"**{len(shadow_defs)}** shadow-capable definitions ({n_tmpl} template, "
        f"{n_type} type) in **{len(files)}** mod .gui files"
        + ("; " + "; ".join(extras) if extras else ""),
        f"- **{len(stale_changed)}** shadowed vanilla definitions changed and not "
        f"reconciled: action needed",
    ]
    if reconciled_changed:
        counts.append(f"- **{len(reconciled_changed)}** shadowed definitions vanilla "
                      f"changed but the mod copy already reconciled")
    counts += [
        f"- **{len(replaced)}** same-path file replacements where vanilla's file "
        f"changed: action needed",
        f"- **{len(new_coll)}** new name collisions (vanilla added a same-name definition)",
        f"- **{len(van_removed)}** shadowed definitions removed from vanilla",
        f"- **{len(unchanged)}** unchanged shadows, **{mod_only}** mod-only definitions",
        "",
    ]
    print("\n".join(counts))

    def _print_changed_def(d, o, n):
        print(f"### {d['name']}")
        print(f"- **Kind:** {d['kind']}")
        print(f"- **Mod:** `{d['file']}:{d['line']}`")
        print(f"- **Vanilla:** `{n[0]}`")
        fb = fork_defs.get((d["module"], d["kind"], d["name"]))
        if fb:
            print(f"- **Forked from:** {fb[2][:7]} ({fb[3]})")
        if _loads_after_vanilla(d["file"], n[0]):
            print("- **Load order:** ⚠ mod file does not sort before the vanilla "
                  "file; the mod definition may never apply (verify)")
        if args.diff:
            dl = diff_lines(o[1], n[1], d["name"])
            if dl:
                print("```diff")
                sys.stdout.write("".join(dl))
                print("```")
        else:
            n_add, n_rem, key_lines = diff_summary(o[1], n[1])
            for line in key_lines[:10]:
                print(line)
            if len(key_lines) > 10:
                print(f"  ... and {len(key_lines) - 10} more lines")
            print(f"  *({n_add} added, {n_rem} removed)*")
        _print_containment(d["text"], o[1], n[1], "mod copy")
        print()

    if stale_changed:
        print(f"## Changed Shadowed Definitions: mod GUI override is suppressing "
              f"vanilla changes ({len(stale_changed)})")
        print()
        for d, o, n in stale_changed:
            _print_changed_def(d, o, n)

    if reconciled_changed:
        print(f"## Changed Shadowed Definitions: mod copy already reconciled "
              f"({len(reconciled_changed)})")
        print()
        print("Vanilla changed these definitions and the mod's shadow copy already "
              "carries the change: informational, no action needed (verify in game "
              "if desired).")
        print()
        for d, o, n in reconciled_changed:
            _print_changed_def(d, o, n)

    if replaced:
        print(f"## Same-Path File Replacements: vanilla file changed underneath "
              f"({len(replaced)})")
        print()
        print("The mod file fully replaces the vanilla file at the same path, so "
              "vanilla's changes to its version are suppressed. Definitions "
              "inside these files are audited here, not as shadows.")
        print()
        for rel, ot, nt in replaced:
            print(f"### {rel}")
            fb = fork_files.get(rel)
            if fb:
                tag = fb[2].split()[0] if fb[2] else "?"
                print(f"- **Forked from:** {fb[1][:7]} ({tag})"
                      + ("  *(pinned)*" if fb[3] else ""))
            if ot is None:
                print("  *(vanilla added this file; the mod file now overrides "
                      "a file that did not exist before)*")
            elif nt is None:
                print("  *(vanilla removed this file; the mod copy is now the only one)*")
            else:
                ch, ad, rm = _def_level_changes(ot, nt)
                def _fmt(ks):
                    s = ", ".join(f"{k}:{name}" for k, name in ks[:8])
                    return s + (" …" if len(ks) > 8 else "")
                if ch:
                    print(f"  - changed defs: {_fmt(ch)}")
                if ad:
                    print(f"  - added defs: {_fmt(ad)}")
                if rm:
                    print(f"  - removed defs: {_fmt(rm)}")
                n_add, n_rem, _ = diff_summary(ot, nt)
                print(f"  *({n_add} lines added, {n_rem} removed in vanilla's version)*")
            print()

    if new_coll:
        print(f"## New Name Collisions: vanilla now defines a name the mod also "
              f"defines ({len(new_coll)})")
        print()
        for d, n in new_coll:
            order = (" (⚠ vanilla may load first)"
                     if _loads_after_vanilla(d["file"], n[0])
                     else " (mod loads first)")
            print(f"- **{d['kind']}:{d['name']}**, mod `{d['file']}:{d['line']}` "
                  f"vs vanilla `{n[0]}`{order}")
        print()

    if van_removed:
        print(f"## Shadowed Definitions Removed from Vanilla ({len(van_removed)})")
        print()
        for d, o in van_removed:
            print(f"- **{d['kind']}:{d['name']}**, mod `{d['file']}:{d['line']}` "
                  f"(was in `{o[0]}`); the mod copy is now the only definition")
        print()

    if args.include_unchanged and (unchanged or same_path_unchanged):
        print(f"## Unchanged ({len(unchanged) + len(same_path_unchanged)})")
        print()
        for d, n in unchanged:
            print(f"- {d['kind']}:{d['name']}, shadows `{n[0]}`")
        for rel in same_path_unchanged:
            print(f"- file:{rel}, replaces same-path vanilla file (unchanged)")
        print()

    if not stale_changed and not replaced and not new_coll:
        if reconciled_changed:
            print("**All GUI overrides are current with vanilla** "
                  f"({len(reconciled_changed)} changed but already reconciled).")
        else:
            print("**All GUI overrides are current with vanilla.**")
    else:
        print("---")
        print(f"**Action needed:** {len(stale_changed)} shadowed definitions drifted, "
              f"{len(replaced)} replaced files changed, "
              f"{len(new_coll)} new collisions.")
        if not args.diff and stale_changed:
            print("Run with `--diff` for full unified diffs.")
    print()
    print("_Implicit GUI overrides: the first-loaded definition of a template/type "
          "name wins; load order is approximated by case-insensitive path sort. "
          "Findings are suspects to verify in game._")

def _stamp_add(path, tag):
    """Insert a fork-point comment as the first line, preserving a leading BOM."""
    raw = path.read_bytes()
    bom = b""
    if raw.startswith(b"\xef\xbb\xbf"):
        bom, raw = raw[:3], raw[3:]
    line = f"# pdx-audit fork-point: {tag}\n".encode("utf-8")
    path.write_bytes(bom + line + raw)

def _stamp_update(path, tag):
    """Rewrite the existing fork-point comment in place, preserving a leading
    BOM and everything else about the file."""
    raw = path.read_bytes()
    bom = b""
    if raw.startswith(b"\xef\xbb\xbf"):
        bom, raw = raw[:3], raw[3:]
    text = raw.decode("utf-8")
    new_text, n = FORK_PIN_RE.subn(f"# pdx-audit fork-point: {tag}", text, count=1)
    if n:
        path.write_bytes(bom + new_text.encode("utf-8"))

def run_stamp_fork_points(mod_root, vanilla_repo, commits, refresh=False):
    """Detect each same-path replacement file's fork point and offer to write a
    '# pdx-audit fork-point: <version>' comment at the top of it. Shows the full
    plan and requires interactive confirmation before touching any file, because
    it modifies the mod's own source .gui files. With refresh=True it also
    rewrites pins whose version no longer matches the file's contents."""
    files = mod_gui_files(mod_root)
    if not files:
        print("No mod .gui files found.", file=sys.stderr)
        return
    modules = sorted({rel.split("/", 1)[0] for rel, _ in files})
    mod_file_texts = {rel: text for rel, text in files}
    print("Detecting fork points...", file=sys.stderr)
    _def_base, file_base, _pin_err, pin_stale = build_fork_baselines(
        vanilla_repo, commits, modules, [], mod_file_texts)

    adds, updates, stale_left, kept = [], [], [], []
    for rel in sorted(file_base):
        _t, _h, fmsg, pinned = file_base[rel]
        tag = fmsg.split()[0] if fmsg else None
        if pinned:
            if rel in pin_stale:
                old_tag, new_tag = pin_stale[rel]
                (updates if refresh else stale_left).append((rel, old_tag, new_tag))
            else:
                kept.append(rel)
        elif tag:
            adds.append((rel, tag))

    if kept:
        print(f"Already pinned and current (left untouched): {len(kept)}")
    if stale_left:
        print(f"Pins that look stale ({len(stale_left)}): re-run with --refresh "
              f"to update:")
        for rel, old_tag, new_tag in stale_left:
            print(f"  - {rel}: pinned {old_tag}, looks like {new_tag}")
    if not adds and not updates:
        print("No fork-point comments to write. Nothing to do.")
        return

    print()
    print("⚠  This will MODIFY the following files in your mod:")
    print()
    for rel, tag in adds:
        print(f"  {rel}")
        print(f"      add:    # pdx-audit fork-point: {tag}")
    for rel, old_tag, new_tag in updates:
        print(f"  {rel}")
        print(f"      change: {old_tag} -> # pdx-audit fork-point: {new_tag}")
    print()
    print(f"{len(adds) + len(updates)} file(s) will be changed "
          f"({len(adds)} added, {len(updates)} updated). This writes to your mod's "
          f"source .gui files.")

    if not sys.stdin.isatty():
        print("Refusing to modify files without an interactive confirmation. "
              "Run this in a terminal.", file=sys.stderr)
        sys.exit(1)
    try:
        answer = input("Proceed? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer not in ("y", "yes"):
        print("Aborted. No files were modified.")
        return

    written = 0
    for rel, tag in adds:
        try:
            _stamp_add(mod_root / rel, tag)
            written += 1
        except OSError as e:
            print(f"  skip {rel}: {e}", file=sys.stderr)
    for rel, _old_tag, new_tag in updates:
        try:
            _stamp_update(mod_root / rel, new_tag)
            written += 1
        except OSError as e:
            print(f"  skip {rel}: {e}", file=sys.stderr)
    print(f"Done. Modified {written} file(s).")
