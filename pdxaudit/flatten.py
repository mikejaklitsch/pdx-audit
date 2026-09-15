"""Flattening: vanilla and a mod's foundations resolved into one base.

Each foundation applies, in the order the user stores, to what vanilla and the
foundations before it produced, by the engine's rules:

- an exact path, plain definition, GUI template or type, or localization key from a
  later layer replaces the unit; within one source the alphanumerically first file
  name's version is that source's, case-insensitive, and for localization a
  `replace/` copy comes before the rest;
- REPLACE and TRY_REPLACE replace a present unit; REPLACE_OR_CREATE replaces or
  creates it;
- INJECT and TRY_INJECT add their insides to a present unit; INJECT_OR_CREATE adds
  them or creates the unit from them;
- a TRY_ directive on a missing unit does nothing; a foundation's INJECT or REPLACE
  on a missing unit is skipped and not reported;
- a merging type combines entries in load order, and within one source in file name
  order; in an on_action a later layer's `effect` or `trigger` replaces the earlier one;
- a unit a foundation defines twice with no stated winner is flattened from its
  first copy.

Every unit a foundation touches records its owner {layer, version, file}: the layer
whose definition it is. A unit other layers injected into or merged entries into
also lists every contributor in `parts`, in load order. A unit no foundation
touched has no owner entry; its owner is vanilla."""
import re
from collections import Counter, defaultdict
from pathlib import PurePosixPath

from . import diff3, session
from .dupes import scan_script, type_of
from .gui import parse_gui_defs
from .loc import is_replace_loc, loc_entries
from .overrides import IDENT_ASSIGN
from .tracker import MODULE_ROOTS

REPLACES = ("REPLACE", "TRY_REPLACE", "REPLACE_OR_CREATE")
INJECTS = ("INJECT", "TRY_INJECT", "INJECT_OR_CREATE")
CREATES = ("REPLACE_OR_CREATE", "INJECT_OR_CREATE")
ON_ACTION_SINGLE = ("effect", "trigger")
_TOP = re.compile(r"\s*(?:([A-Z][A-Z_]*):)?([^\s=#{}]+)\s*=\s*\{")


def file_order(path):
    """The order one source's files apply in: case-insensitive file name, then path."""
    return (PurePosixPath(path).name.lower(), path.lower())


def loc_order(path):
    return (not is_replace_loc(path), *file_order(path))


def layer_texts(source, commit, want, order=file_order):
    """[(path, text)] of the files of one source version that `want(path)` accepts,
    in the order they apply. A run reads each version's files once."""
    def read():
        files = [(p, b) for p, b in source.tree_files(commit) if want(p)]
        blobs = source.read_blobs([b for _p, b in files])
        # Line endings as the mod's own files read, so a CRLF copy never differs from an LF one.
        return sorted(((p, blobs[b].decode("utf-8-sig", errors="replace").replace("\r\n", "\n"))
                       for p, b in files if b in blobs), key=lambda f: order(f[0]))
    return session.memo(("flatten.layer", source.git_dir, commit, want.__name__, order.__name__), read)


def _code(line):
    in_str = False
    for i, c in enumerate(line):
        if c == '"':
            in_str = not in_str
        elif c == "#" and not in_str:
            return line[:i]
    return line


def top_entries(text):
    """[(prefix, name, text)] for each top-level `name = { ... }` block of a script
    file, in order, repeats and override prefixes included."""
    out, lines, depth, start, head = [], text.split("\n"), 0, None, None
    for i, raw in enumerate(lines):
        code = _code(raw)
        if depth == 0 and start is None:
            m = _TOP.match(code)
            if m:
                start, head = i, (m.group(1) or "", m.group(2))
        depth += code.count("{") - code.count("}")
        if start is not None and depth <= 0:
            out.append((head[0], head[1], "\n".join(lines[start:i + 1])))
            start, head = None, None
            depth = 0
        elif depth < 0:
            depth = 0
    return out


def strip_prefix(text):
    return re.sub(r"^(\s*)[A-Z][A-Z_]*:", r"\1", text, count=1)


def _split(text):
    """(head through '{', insides, the closing '}' and after) of a block's text."""
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return text, "", ""
    return text[:i + 1], text[i + 1:j], text[j:]


def _join(head, *insides):
    body = "\n".join(part.strip("\n") for part in insides if part.strip())
    return f"{head}\n{body}\n}}" if body else f"{head}\n}}"


def inject_text(current, injection):
    """`current` with the insides of `injection` added at its end."""
    head, inside, _close = _split(current)
    return _join(head, inside, _split(injection)[1])


def merge_text(current, addition, on_action=False):
    """A merging type's entries combined. In an on_action the addition's `effect`
    and `trigger` replace the current ones; other keys combine."""
    head, inside, _close = _split(current)
    added = _split(addition)[1]
    if on_action:
        replaced = {n.key for n in diff3.nodes(added) if n.key in ON_ACTION_SINGLE}
        for n in sorted((n for n in diff3.nodes(inside) if n.key in replaced), key=lambda n: -n.start):
            inside = inside[:n.start] + inside[n.end:]
    return _join(head, inside, added)


def _owner(layer, version, file):
    return {"layer": layer, "version": version, "file": file}


# --- GUI ----------------------------------------------------------------------------------

def _gui_file(modules):
    mods = set(modules)

    def gui_file(path):
        parts = path.split("/")
        return len(parts) >= 3 and parts[0] in mods and parts[1] == "gui" and path.endswith(".gui")
    return gui_file


def flatten_gui(vanilla_index, vanilla_tag, layers, modules):
    """(def_idx, file_idx, bad, owners, file_owners) for GUI definitions and files,
    in build_gui_vanilla's shapes, over `layers`: [(source, version, commit)] in load
    order. owners maps a definition key, file_owners a file path, to its owner."""
    def_idx, file_idx, bad = vanilla_index
    defs, files, bad = dict(def_idx), dict(file_idx), list(bad)
    owners, file_owners = {}, {}
    want = _gui_file(modules)
    for source, version, commit in layers:
        texts = layer_texts(source, commit, want)
        if not texts:
            continue
        replaced = {p for p, _t in texts if p in files}
        if replaced:
            lost = {k for k, (vf, _t) in defs.items() if vf in replaced}
            for k in lost:
                del defs[k]
                owners.pop(k, None)
            rest = sorted((p for p in files if p not in replaced), key=str.lower)
            for k in sorted(lost):
                module, kind, name = k
                for p in rest:
                    if not p.startswith(f"{module}/") or name not in files[p]:
                        continue
                    hit = next((d for d in parse_gui_defs(files[p])[0]
                                if d["kind"] == kind and d["name"] == name), None)
                    if hit:
                        defs[k] = (p, hit["text"])
                        if p in file_owners:
                            owners[k] = dict(file_owners[p])
                        break
        seen = set()
        for path, text in texts:
            files[path] = text
            file_owners[path] = _owner(source.id, version, path)
            parsed, clean = parse_gui_defs(text)
            if not clean and path not in bad:
                bad.append(path)
            for d in parsed:
                if d["kind"] not in ("template", "type"):
                    continue
                key = (path.split("/", 1)[0], d["kind"], d["name"])
                if key in seen:
                    continue
                seen.add(key)
                defs[key] = (path, d["text"])
                owners[key] = _owner(source.id, version, path)
    return defs, files, bad, owners, file_owners


# --- script blocks ---------------------------------------------------------------------------

def _in_categories(categories):
    cats = set(categories)

    def script_file(path):
        return path.endswith((".txt", ".gui")) and path.rsplit("/", 1)[0] in cats
    return script_file


def flatten_blocks(vanilla_index, vanilla_tag, layers, categories, merging):
    """(idx, owners) for top-level blocks in the override audit's shape, {(category,
    name): (file, text)}, over `layers` in load order. `merging` holds the merging
    types (`common/on_action`, ...)."""
    idx, owners = dict(vanilla_index), {}
    from_file = defaultdict(set)
    for k, (vf, _t) in idx.items():
        from_file[vf].add(k)
    want = _in_categories(categories)
    for source, version, commit in layers:
        texts = layer_texts(source, commit, want)
        for path, _text in texts:
            for k in from_file.pop(path, ()):
                if k in idx and idx[k][0] == path:
                    del idx[k]
                    owners.pop(k, None)
        defined = set()
        for path, text in texts:
            cat = path.rsplit("/", 1)[0]
            t = type_of(path)
            merged = t in merging
            for prefix, name, block in top_entries(text):
                key, cur = (cat, name), idx.get((cat, name))
                me = _owner(source.id, version, path)
                below = owners.get(key) or _owner("vanilla", vanilla_tag, cur[0] if cur else path)
                if not prefix:
                    if merged and cur is not None:
                        idx[key] = (cur[0], merge_text(cur[1], block, on_action=t == "common/on_action"))
                        owners[key] = dict(below, parts=[*below.get("parts", [dict(below)]), me])
                    elif key not in defined:
                        idx[key], owners[key] = (path, block), me
                        defined.add(key)
                elif prefix in REPLACES:
                    if cur is None and prefix not in CREATES:
                        continue
                    idx[key], owners[key] = (path, strip_prefix(block)), me
                    defined.add(key)
                elif prefix in INJECTS:
                    if cur is None:
                        if prefix not in CREATES:
                            continue
                        idx[key], owners[key] = (path, strip_prefix(block)), me
                        defined.add(key)
                    else:
                        idx[key] = (cur[0], inject_text(cur[1], block))
                        owners[key] = dict(below, parts=[*below.get("parts", [dict(below)]), me])
                from_file[path].add(key)
    return idx, owners


def _top_scripts(path):
    parts = path.split("/")
    return len(parts) >= 4 and parts[0] in MODULE_ROOTS and parts[1] == "common" and path.endswith(".txt")


def flatten_definitions(vanilla_defs, vanilla_tag, layers):
    """The duplicate audit's {"names": {type: {name: [files]}}, "files": {path: [names]}}
    with each foundation's script files added over vanilla's, a file at the same path
    replacing the one below; and owners {(type, name): owner} for names a foundation defines."""
    names = {t: {n: list(fs) for n, fs in v.items()} for t, v in vanilla_defs["names"].items()}
    files = {p: list(ns) for p, ns in vanilla_defs["files"].items()}
    owners = {}
    for source, version, commit in layers:
        texts = layer_texts(source, commit, _top_scripts)
        for path, text in texts:
            t = type_of(path)
            for n in files.get(path, []):
                if path in names.get(t, {}).get(n, []):
                    names[t][n].remove(path)
                    if not names[t][n]:
                        del names[t][n]
            entries, _keys = scan_script(text)
            files[path] = sorted({n for _p, n, _l in entries})
            for prefix, n, _l in entries:
                bucket = names.setdefault(t, {}).setdefault(n, [])
                if path not in bucket:
                    bucket.append(path)
                if not prefix or prefix in REPLACES:
                    owners.setdefault((t, n, source.id), _owner(source.id, version, path))
    by_unit = {}
    for (t, n, _layer), owner in owners.items():
        by_unit[(t, n)] = owner          # the last layer to define a name owns it
    return {"names": names, "files": files}, by_unit


# --- single values, names, vocabulary ---------------------------------------------------------

def _txt_under(categories):
    cats = set(categories)

    def txt_file(path):
        return path.endswith(".txt") and any(path == c or path.startswith(c + "/") for c in cats)
    return txt_file


def flatten_scalars(vanilla_values, layers, categories, names):
    """vanilla_scalar_values' {(category, name): (file, statement)} with foundations'
    top-level `name = value` statements over vanilla's."""
    out = dict(vanilla_values)
    want = _txt_under(categories)
    for source, _version, commit in layers:
        seen = set()
        for path, text in layer_texts(source, commit, want):
            if not any(n in text for n in names):
                continue
            cat = path.rsplit("/", 1)[0]
            for n in diff3.nodes(text):
                key = (cat, n.key)
                if n.kind == "stmt" and n.key in names and n.value[1] is not None and key not in seen:
                    seen.add(key)
                    out[key] = (path, text[n.start:n.end])
    return out


def flatten_names(vanilla_found, layers, categories, names):
    """Names from `names` assigned anywhere in vanilla's or a foundation's scripts."""
    found = set(vanilla_found)
    want = _txt_under(categories)
    pats = {n: re.compile(rf"(?m)^\s*{re.escape(n)}\s*=") for n in names}
    for source, _version, commit in layers:
        for _path, text in layer_texts(source, commit, want):
            found.update(n for n in names - found if pats[n].search(text))
    return found


def _any_txt(path):
    return path.endswith(".txt")


def lhs_tokens(text):
    counts = Counter()
    for raw in text.split("\n"):
        m = IDENT_ASSIGN.match(raw.split("#")[0])
        if m:
            counts[m.group(1)] += 1
    return counts


def flatten_vocab(vanilla_vocab, replaced_texts, layers):
    """vanilla's `token =` counts with the files foundations replace taken out and each
    foundation's .txt files counted in. `replaced_texts` holds vanilla's text of each
    replaced path."""
    vocab = Counter(vanilla_vocab)
    for text in replaced_texts:
        vocab.subtract(lhs_tokens(text))
    for source, _version, commit in layers:
        for _path, text in layer_texts(source, commit, _any_txt):
            vocab.update(lhs_tokens(text))
    return {t: c for t, c in vocab.items() if c > 0}


def layer_paths(layers, want):
    """Every path the layers' files take, which replaces the file at that path below."""
    return {p for source, _v, commit in layers for p, _t in layer_texts(source, commit, want)}


# --- localization --------------------------------------------------------------------------

def _loc_file(path):
    parts = path.split("/")
    return path.endswith(".yml") and len(parts) > 2 and parts[0] in MODULE_ROOTS and parts[1] == "localization"


def flatten_loc(vanilla_values, layers, wanted):
    """(values, owners) for the `wanted` (language, key) pairs: vanilla's values with each
    foundation's over them. Within a foundation a replace/ copy wins, then the first file."""
    values, owners = dict(vanilla_values), {}
    for source, version, commit in layers:
        seen = set()
        for path, text in layer_texts(source, commit, _loc_file, loc_order):
            for lang, key, value, _line in loc_entries(text):
                k = (lang, key)
                if k in wanted and k not in seen:
                    seen.add(k)
                    values[k] = value
                    owners[k] = _owner(source.id, version, path)
    return values, owners
