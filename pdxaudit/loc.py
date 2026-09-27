"""Key-based localization audit."""

import re
import sys

from . import session
from .gui import commits_up_to
from .report import Finding
from .tracker import MODULE_ROOTS, read_blobs, tag_of, tree_files
from .config import should_skip

LOC_LANG_RE = re.compile(r"^\ufeff?\s*l_([a-z_]+):\s*(?:#.*)?$")

LOC_KEY_RE = re.compile(r'^\s+([A-Za-z0-9_.\-]+):\s*\d*\s*"(.*)"')

def loc_entries(text):
    """(language, key, value, line) for every entry of one Paradox .yml, repeats
    included, in file order. Language comes from the 'l_<lang>:' header; entries
    are 'KEY:[num] "value"'. Tolerant of blank and comment lines; the value is
    captured to the last quote on the line."""
    lang = None
    for n, line in enumerate(text.splitlines(), 1):
        # A header needs "l_" and an entry needs a quote; testing for those
        # first skips the regexes on lines neither could match.
        if "l_" in line:
            lm = LOC_LANG_RE.match(line)
            if lm:
                lang = lm.group(1)
                continue
        if lang and '"' in line:
            km = LOC_KEY_RE.match(line)
            if km:
                yield lang, km.group(1), km.group(2), n

def parse_loc(text):
    """(language, key) -> value for one Paradox .yml; a repeated key keeps its
    last value."""
    return {(lang, key): value for lang, key, value, _n in loc_entries(text)}

def is_replace_loc(rel):
    """True for a localization file under a `replace/` folder, whose keys override
    the same keys in the mod's other localization files."""
    parts = str(rel).replace("\\", "/").split("/")
    return "localization" in parts and "replace" in parts[parts.index("localization") + 1:-1]

def mod_loc_files(mod_root):
    """[(rel_path, text), ...] for the mod's .yml files under a localization/ dir."""
    out = []
    for fp in session.mod_paths(mod_root, ".yml"):
        rel = fp.relative_to(mod_root)
        if (len(rel.parts) < 3 or rel.parts[0] not in MODULE_ROOTS
                or rel.parts[1] != "localization" or should_skip(rel)):
            continue
        try:
            out.append((rel.as_posix(), session.read_text(fp)))
        except Exception:
            continue
    return out

def build_loc_vanilla(vanilla_repo, commit, wanted, label="", exclude=()):
    """(language, key) -> value at `commit`, restricted to the `wanted` keys the
    mod defines. Reads only vanilla .yml files in the languages the mod uses, in
    tree order so the first file defining a key wins, leaving out the paths in
    `exclude`. Most files are identical across commits, so a run reads and parses
    each distinct file once."""
    langs = {lang for lang, _ in wanted}
    if label:
        print(f"  {label}: reading vanilla localization...",
              end="", file=sys.stderr, flush=True)
    yml = [(path, blob) for path, blob in tree_files(vanilla_repo, commit)
           if path.endswith(".yml") and path not in exclude]
    result = {}
    if not yml:
        if label:
            print(" failed!", file=sys.stderr)
        return result
    files = [(path, blob) for path, blob in yml
             if not langs or any(f"_l_{lang}." in path or f"/{lang}/" in path for lang in langs)]
    raw = read_blobs(vanilla_repo, [blob for _path, blob in files
                                    if not session.cached(("loc", blob))])
    for _path, blob in files:
        if blob not in raw and not session.cached(("loc", blob)):
            continue
        entries = session.memo(("loc", blob), lambda: parse_loc(
            raw[blob].decode("utf-8-sig", errors="replace")))
        for k, v in entries.items():
            if k in wanted and k not in result:
                result[k] = v
    if label:
        print(f" {len(result)}/{len(wanted)} matched.", file=sys.stderr)
    return result

def loc_target(k):
    lang, key = k
    return f"loc:{lang}/{key}"

def _tag(msg):
    return tag_of(msg)

def run_loc_audit(mod_root, base, old_hash, old_msg, new_hash, new_msg, args, ctx=None):
    """Key-based localization audit: for each loc key the mod redefines, report
    whether the base changed that key's value or removed it. A key with an open
    finding from an earlier run is measured from that finding's base version, so
    a change carries forward until it is fixed or dismissed. `base` is a Base or the
    vanilla tracker's path."""
    from .base import as_base
    base = as_base(base)
    files = mod_loc_files(mod_root)
    if not files:
        print("No mod .yml localization files found.", file=sys.stderr)
        if ctx is not None:
            ctx.scanned["loc"] = 0
        return []
    # A key in a replace/ file overrides the mod's other copies, so it is the mod's value.
    mod_keys = {}
    for rel, text in sorted(files, key=lambda f: not is_replace_loc(f[0])):
        for k, v in parse_loc(text).items():
            mod_keys.setdefault(k, (v, rel))
    if args.block:
        mod_keys = {k: v for k, v in mod_keys.items() if k[1] == args.block}
    if ctx is not None:
        ctx.scanned["loc"] = len(mod_keys)
    if not mod_keys:
        print("No localization keys defined by the mod.", file=sys.stderr)
        return []
    wanted = set(mod_keys)
    print(f"Scanning {len(wanted)} localization keys the mod defines...",
          file=sys.stderr)
    old_v = base.loc(old_hash, wanted, f"old ({old_hash[:7]})")
    new_v = base.loc(new_hash, wanted, f"new ({new_hash[:7]})")
    new_tag = ctx.new_tag if ctx is not None else _tag(new_msg)
    old_tag = _tag(old_msg)

    # Keys carried forward from open findings, measured from their base version.
    carried, old_first, tags = {}, [], []
    if ctx is not None:
        old_first = list(reversed(commits_up_to(ctx.commits, new_hash)))
        tags = [_tag(m) for _h, m in old_first]
    if ctx is not None and not ctx.fixed_window and ctx.bases:
        for k in wanted:
            tag = ctx.bases.get(loc_target(k))
            if tag in tags and tag != old_tag:
                carried[k] = len(tags) - 1 - tags[::-1].index(tag)
    # A window wider than one version dates each change by the version that made it.
    old_pos = next((i for i, (h, _m) in enumerate(old_first) if old_hash.startswith(h) or h.startswith(old_hash)),
                   None)
    spanned = set()
    if old_pos is not None and old_pos < len(old_first) - 2:
        spanned = {k for k in wanted if k not in carried and old_v.get(k) != new_v.get(k)}
    searched = set(carried) | spanned
    memo = {}

    def values_at(i):
        if i not in memo:
            memo[i] = base.loc(old_first[i][0], searched)
        return memo[i]

    changed, removed, new_coll, unchanged, mod_only = [], [], [], [], 0
    for k in sorted(wanted):
        start = carried.get(k, old_pos if k in spanned else None)
        if k in carried:
            ov, base_tag = values_at(start).get(k), tags[start]
        else:
            ov, base_tag = old_v.get(k), old_tag
        since = new_tag
        if start is not None:
            for j in range(start + 1, len(old_first)):
                if values_at(j).get(k) != ov:
                    since = tags[j]
                    break
        nv = new_v.get(k)
        modval, modfile = mod_keys[k]
        if ov is None and nv is None:
            mod_only += 1
        elif ov is not None and nv is None:
            removed.append((k, ov, modfile, since, base_tag))
        elif ov is None and nv is not None:
            new_coll.append((k, nv, modfile, since, base_tag))
        elif ov != nv:
            changed.append((k, ov, nv, modval, modfile, since, base_tag))
        else:
            unchanged.append(k)

    summary = [f"# Localization Audit: {old_hash[:7]} → {new_hash[:7]}"]
    if old_msg or new_msg:
        summary.append(f"*{old_msg} → {new_msg}*")
    summary += [
        "",
        f"**{len(wanted)}** localization keys the mod defines",
        f"- **{len(changed)}** vanilla changed the string: your override may be "
        f"masking a reworded value",
        f"- **{len(removed)}** vanilla removed the key: override orphaned",
        f"- **{len(new_coll)}** vanilla newly added a key the mod also defines",
        f"- **{len(unchanged)}** unchanged, **{mod_only}** mod-only (not overrides)",
        "",
    ]
    print("\n".join(summary))

    if changed:
        print(f"## Changed Vanilla Strings ({len(changed)})")
        print()
        print("Vanilla changed these values; your override still shows its own "
              "text, so any rewording or correction vanilla made is suppressed.")
        print()
        for (lang, key), ov, nv, modval, modfile, since, base_tag in changed:
            print(f"### {key} ({lang})")
            print(f"- **Mod:** `{modfile}` = \"{modval}\"")
            print(f"- **Vanilla at {base_tag}:** \"{ov}\"")
            print(f"- **Vanilla now:** \"{nv}\"")
            print()

    if removed:
        print(f"## Keys Removed from Vanilla ({len(removed)})")
        print()
        for (lang, key), _ov, modfile, _since, _base in removed:
            print(f"- **{key}** ({lang}), `{modfile}`; the mod key no longer "
                  f"overrides anything")
        print()

    if new_coll:
        print(f"## New Name Collisions ({len(new_coll)})")
        print()
        print("Vanilla added a key the mod already defines; the mod's value wins "
              "or loses by load order.")
        print()
        for (lang, key), nv, modfile, _since, _base in new_coll:
            print(f"- **{key}** ({lang}), mod `{modfile}` vs new vanilla \"{nv}\"")
        print()

    if not changed and not removed and not new_coll:
        print("**All overridden localization keys are current with vanilla.**")
    else:
        print("---")
        print(f"**Action needed:** {len(changed)} changed strings, "
              f"{len(removed)} orphaned keys, {len(new_coll)} new collisions.")

    def key(k, **fields):
        return {"target": loc_target(k), **fields}

    findings = []
    for k, ov, nv, modval, modfile, since, base_tag in changed:
        findings.append(Finding("loc_changed", k[1], modfile, k[0], None,
                                key(k, old=ov, new=nv, mod=modval), since, base_tag))
    for k, ov, modfile, since, base_tag in removed:
        findings.append(Finding("loc_removed", k[1], modfile, k[0], None,
                                key(k, change="removed", old=ov), since, base_tag))
    for k, nv, modfile, since, base_tag in new_coll:
        findings.append(Finding("loc_collision", k[1], modfile, k[0], None,
                                key(k, change="added", new=nv), since, base_tag))
    return findings
