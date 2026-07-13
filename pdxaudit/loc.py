"""Key-based localization audit."""

import re
import sys
import tarfile
import io

from .tracker import MODULE_ROOTS, _git_archive
from .config import should_skip

LOC_LANG_RE = re.compile(r"^\ufeff?\s*l_([a-z_]+):\s*(?:#.*)?$")

LOC_KEY_RE = re.compile(r'^\s+([A-Za-z0-9_.\-]+):\s*\d*\s*"(.*)"')

def parse_loc(text):
    """(language, key) -> value for one Paradox .yml. Language comes from the
    'l_<lang>:' header; entries are 'KEY:[num] "value"'. Tolerant of blank and
    comment lines; the value is captured to the last quote on the line."""
    lang = None
    out = {}
    for line in text.splitlines():
        lm = LOC_LANG_RE.match(line)
        if lm:
            lang = lm.group(1)
            continue
        km = LOC_KEY_RE.match(line)
        if km and lang:
            out[(lang, km.group(1))] = km.group(2)
    return out

def mod_loc_files(mod_root):
    """[(rel_path, text), ...] for the mod's .yml files under a localization/ dir."""
    out = []
    for fp in sorted(mod_root.rglob("*.yml")):
        rel = fp.relative_to(mod_root)
        if (not rel.parts or rel.parts[0] not in MODULE_ROOTS
                or "localization" not in rel.parts or should_skip(rel)):
            continue
        try:
            out.append((str(rel), fp.read_text(encoding="utf-8-sig")))
        except Exception:
            continue
    return out

def build_loc_vanilla(vanilla_repo, commit, wanted, label=""):
    """(language, key) -> value at `commit`, restricted to the `wanted` keys the
    mod defines. Scans all vanilla .yml but records only wanted keys, so the
    result stays small. Files whose language the mod does not use are skipped."""
    langs = {lang for lang, _ in wanted}
    if label:
        print(f"  {label}: reading vanilla localization...",
              end="", file=sys.stderr, flush=True)
    raw = _git_archive(vanilla_repo, commit, ["*.yml"], timeout=180)
    result = {}
    if not raw:
        if label:
            print(" failed!", file=sys.stderr)
        return result
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), ignore_zeros=True) as tf:
            for m in tf.getmembers():
                if not m.isfile() or not m.name.endswith(".yml"):
                    continue
                if langs and not any(f"_l_{lang}." in m.name or f"/{lang}/" in m.name
                                     for lang in langs):
                    continue
                f = tf.extractfile(m)
                if f is None:
                    continue
                content = f.read().decode("utf-8-sig", errors="replace")
                for k, v in parse_loc(content).items():
                    if k in wanted and k not in result:
                        result[k] = v
    except tarfile.TarError:
        if label:
            print(" tar error!", file=sys.stderr)
        return result
    if label:
        print(f" {len(result)}/{len(wanted)} matched.", file=sys.stderr)
    return result

def run_loc_audit(mod_root, vanilla_repo, old_hash, old_msg, new_hash, new_msg, args):
    """Key-based localization audit: for each loc key the mod redefines, report
    whether vanilla changed that key's value or removed it between snapshots."""
    files = mod_loc_files(mod_root)
    if not files:
        print("No mod .yml localization files found.", file=sys.stderr)
        return
    mod_keys = {}
    for rel, text in files:
        for k, v in parse_loc(text).items():
            mod_keys.setdefault(k, (v, rel))
    if args.block:
        mod_keys = {k: v for k, v in mod_keys.items() if k[1] == args.block}
    if not mod_keys:
        print("No localization keys defined by the mod.", file=sys.stderr)
        return
    wanted = set(mod_keys)
    print(f"Scanning {len(wanted)} localization keys the mod defines...",
          file=sys.stderr)
    old_v = build_loc_vanilla(vanilla_repo, old_hash, wanted, f"old ({old_hash[:7]})")
    new_v = build_loc_vanilla(vanilla_repo, new_hash, wanted, f"new ({new_hash[:7]})")

    changed, removed, new_coll, unchanged, mod_only = [], [], [], [], 0
    for k in sorted(wanted):
        ov, nv = old_v.get(k), new_v.get(k)
        modval, modfile = mod_keys[k]
        if ov is None and nv is None:
            mod_only += 1
        elif ov is not None and nv is None:
            removed.append((k, modfile))
        elif ov is None and nv is not None:
            new_coll.append((k, nv, modfile))
        elif ov != nv:
            changed.append((k, ov, nv, modval, modfile))
        else:
            unchanged.append(k)

    summary = [f"# Localization Audit: {old_hash[:7]} → {new_hash[:7]}"]
    if old_msg or new_msg:
        summary.append(f"*{old_msg} → {new_msg}*")
    summary += [
        "",
        f"**{len(wanted)}** localization keys the mod overrides",
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
        for (lang, key), ov, nv, modval, modfile in changed:
            print(f"### {key} ({lang})")
            print(f"- **Mod:** `{modfile}` = \"{modval}\"")
            print(f"- **Vanilla old:** \"{ov}\"")
            print(f"- **Vanilla new:** \"{nv}\"")
            print()

    if removed:
        print(f"## Keys Removed from Vanilla ({len(removed)})")
        print()
        for (lang, key), modfile in removed:
            print(f"- **{key}** ({lang}), `{modfile}`; the mod key no longer "
                  f"overrides anything")
        print()

    if new_coll:
        print(f"## New Name Collisions ({len(new_coll)})")
        print()
        print("Vanilla added a key the mod already defines; the mod's value wins "
              "or loses by load order.")
        print()
        for (lang, key), nv, modfile in new_coll:
            print(f"- **{key}** ({lang}), mod `{modfile}` vs new vanilla \"{nv}\"")
        print()

    if not changed and not removed and not new_coll:
        print("**All overridden localization keys are current with vanilla.**")
    else:
        print("---")
        print(f"**Action needed:** {len(changed)} changed strings, "
              f"{len(removed)} orphaned keys, {len(new_coll)} new collisions.")
