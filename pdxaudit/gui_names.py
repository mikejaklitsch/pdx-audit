"""GUI names in the dependency audit.

A mod .gui file uses three kinds of vanilla names:

  type       a widget key (`header_action_button_left = {`) or the parent of a
             type (`type my_button = header_action_button_left {`)
  template   `using = name`, for a template or a local_template
  binding    a promote or a function in a `[...]` expression
             (`[ImportExportMarker.GetLocation]`)

A type or a template is a finding when vanilla's .gui files defined it at an older
tracked version, do not define it at the new version, and the mod does not define
it. The tracker holds no engine dump, so the audit cannot know which binding names
the engine knows. A binding name is a finding when vanilla's .gui files used it at
an older version, and no vanilla .gui file or English localization file uses it at
the new version. The finding gives the patch that dropped the name.

The audit reads the .gui files of every module, because a module can use the
definitions of another: in_game uses the font templates of loading_screen and the
tooltip templates of main_menu."""
import bisect
import json
import re
import sys
from collections import defaultdict

from . import diff3, session
from .gui import commits_up_to, mod_gui_files
from .report import Finding
from .tracker import cache_path, full_hash, tag_of

NAMES_CACHE_VERSION = 1
LOC_CACHE_VERSION = 1

_IDENT = re.compile(r"[A-Za-z_]\w*")
# `[complacency|e]` links a game concept: the name is a concept key, not a binding.
_CONCEPT = re.compile(r"\[\s*[A-Za-z_]\w*\s*\|[^\[\]]*\]")
_GUI_PATH = re.compile(r"^[^/]+/gui/.+\.gui$")
_LOC_PATH = re.compile(r"^[^/]+/localization/english/.+\.yml$")


def binding_names(text):
    """The promote and function names in the `[...]` expressions of `text`.

    A name inside single quotes is a string argument and does not count, but a
    `[...]` expression inside the quotes does. Text after `|` up to the `]` is a
    format and does not count. A game-concept link, one name and a format, does
    not count."""
    names = set()
    depth, fmt, i, n = 0, False, 0, len(text)
    while i < n:
        c = text[i]
        if c == "[":
            concept = _CONCEPT.match(text, i)
            if concept:
                i = concept.end()
                continue
            depth += 1
            fmt = False
        elif c == "]":
            if depth:
                depth -= 1
            fmt = False
        elif depth and c == "|":
            fmt = True
        elif depth and c == "'":
            j = text.find("'", i + 1)
            if j == -1:
                break
            names |= binding_names(text[i + 1:j])
            i = j
        elif depth and not fmt and (c.isalpha() or c == "_"):
            m = _IDENT.match(text, i)
            if i == 0 or text[i - 1] not in "$@#":
                names.add(m.group(0))
            i = m.end() - 1
        elif depth and not fmt and c.isdigit():
            while i + 1 < n and (text[i + 1].isalnum() or text[i + 1] in "._"):
                i += 1
        i += 1
    return names


def _unquote(value):
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else value


def file_names(text):
    """{"defs": {(kind, name)}, "uses": [(kind, name, line)], "bindings":
    [(name, line)]} for the .gui `text`. kind is 'type' or 'template'; a
    local_template counts as a template."""
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    line = lambda offset: bisect.bisect_right(starts, offset)
    defs, uses, bindings = set(), [], []

    def strings(value, offset):
        if isinstance(value, str) and "[" in value:
            for name in binding_names(_unquote(value)):
                bindings.append((name, line(offset)))

    def walk(nodes):
        for node in nodes:
            if node.kind == "stmt":
                op, value = node.value
                if node.key == "using" and value:
                    uses.append(("template", _unquote(value), line(node.start)))
                strings(value, node.start)
            elif node.kind == "items":
                for value in node.value:
                    strings(value, node.start)
            elif node.kind == "block":
                head, _sep, rest = node.key.partition(" ")
                op, value = node.value
                if head == "type" and rest:
                    defs.add(("type", rest))
                    if value:
                        uses.append(("type", _unquote(value), line(node.start)))
                elif head in ("template", "local_template") and rest:
                    defs.add(("template", rest))
                elif not rest and _IDENT.fullmatch(head):
                    uses.append(("type", head, line(node.start)))
                walk(node.children)

    walk(diff3.nodes(text))
    return {"defs": defs, "uses": uses, "bindings": bindings}


def mod_gui_names(mod_root):
    """({(kind, name): ['file:line', ...]}, {name: ['file:line', ...]}) for the
    types, templates and binding names the mod's .gui files use, leaving out the
    types and templates the mod defines itself."""
    uses, bindings, own = defaultdict(list), defaultdict(list), set()
    for rel, text in mod_gui_files(mod_root):
        found = file_names(text)
        own |= found["defs"]
        for kind, name, ln in found["uses"]:
            uses[(kind, name)].append(f"{rel}:{ln}")
        for name, ln in found["bindings"]:
            bindings[name].append(f"{rel}:{ln}")
    return {k: v for k, v in uses.items() if k not in own}, dict(bindings)


def _read_cache(path):
    if path and path.is_file():
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            pass
    return None


def _write_cache(path, data):
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")))
        tmp.replace(path)
    except OSError:
        pass


def _texts(base, point, pattern):
    """[text] of the files at `point` whose path matches `pattern`, without a BOM."""
    files = base.files(point)
    ids = [blob for path, blob in files.items() if pattern.match(path)]
    blobs = base.blobs(ids)
    return [blobs[b].decode("utf-8-sig", errors="replace") for b in ids if b in blobs]


def build_vanilla_gui_names(base, point, label=""):
    """{"type": set, "template": set, "binding": set}: the types and templates
    that vanilla's .gui files define at `point`, and the binding names they use.
    The cache folder of the tracker keeps one file for each commit."""
    repo = getattr(base, "repo", None)
    full = full_hash(repo, point) if repo else ""
    cache = cache_path(repo, f"guinames-v{NAMES_CACHE_VERSION}-{full}.json") if full else None
    data = _read_cache(cache)
    if data is None:
        if label:
            print(f"  {label}: reading vanilla GUI names...", end="", file=sys.stderr, flush=True)
        out = {"type": set(), "template": set(), "binding": set()}
        for text in _texts(base, point, _GUI_PATH):
            found = file_names(text)
            for kind, name in found["defs"]:
                out[kind].add(name)
            out["binding"] |= {name for name, _ln in found["bindings"]}
        data = {k: sorted(v) for k, v in out.items()}
        if label:
            print(f" {sum(len(v) for v in data.values())} names.", file=sys.stderr)
        if any(data.values()):
            _write_cache(cache, data)
    return {k: set(v) for k, v in data.items()}


def build_vanilla_loc_bindings(base, point):
    """The binding names in vanilla's English localization files at `point`."""
    repo = getattr(base, "repo", None)
    full = full_hash(repo, point) if repo else ""
    cache = cache_path(repo, f"locbind-v{LOC_CACHE_VERSION}-{full}.json") if full else None
    data = _read_cache(cache)
    if data is None:
        names = set()
        for text in _texts(base, point, _LOC_PATH):
            for raw in text.split("\n"):
                if "[" in raw:
                    names |= binding_names(raw.partition('"')[2])
        data = sorted(names)
        if data:
            _write_cache(cache, data)
    return set(data)


def _gui_names(base, point, label=""):
    return session.memo(("gui_names", id(base), point),
                        lambda: build_vanilla_gui_names(base, point, label))


def run_gui_names_audit(mod_root, base, old_hash, old_msg, new_hash, new_msg, ctx=None,
                        reported=frozenset()):
    """Findings for the types, templates and binding names the mod's .gui files
    use that vanilla dropped, each with the patch that dropped it. The window is
    the window of run_deps_audit. `reported`: the finding targets that
    run_deps_audit already gives, which this audit does not give again."""
    from .base import as_base
    base = as_base(base)
    uses, bindings = mod_gui_names(mod_root)
    if ctx is not None:
        ctx.scanned["deps"] = ctx.scanned.get("deps", 0) + len(uses) + len(bindings)
    if not uses and not bindings:
        return []

    commits = ctx.commits if ctx is not None else base.commits()
    old_first = list(reversed(commits_up_to(commits, new_hash)))
    fixed_window = ctx.fixed_window if ctx is not None else True
    if fixed_window:
        for i, (h, _m) in enumerate(old_first):
            if old_hash.startswith(h) or h.startswith(old_hash):
                old_first = old_first[i:]
                break
    versions = [(tag_of(msg), _gui_names(base, h, f"gui names {i + 1}/{len(old_first)} ({h[:7]})"))
                for i, (h, msg) in enumerate(old_first)]
    new_names = versions[-1][1]
    if not any(new_names.values()):
        print("Could not read the GUI names of vanilla; the GUI name check did not run.",
              file=sys.stderr)
        return []

    def last_seen(kind, name):
        """(the last version that has the name, the version after it), or None."""
        for i in range(len(versions) - 2, -1, -1):
            if name in versions[i][1][kind]:
                return versions[i][0], versions[i + 1][0]
        return None

    dropped = []
    for (kind, name), sites in uses.items():
        if f"deps:gui/{kind}/{name}" in reported or name in new_names[kind]:
            continue
        seen = last_seen(kind, name)
        if seen:
            dropped.append((kind, name, *seen, sites))
    dropped.sort(key=lambda x: (x[3], x[0], x[1]))

    gone_bindings = []
    candidates = [n for n in bindings if n not in new_names["binding"]]
    if candidates:
        in_loc = build_vanilla_loc_bindings(base, old_first[-1][0])
        for name in sorted(candidates):
            if name in in_loc:
                continue
            seen = last_seen("binding", name)
            if seen:
                gone_bindings.append((name, *seen, bindings[name]))
    gone_bindings.sort(key=lambda x: (x[2], x[0]))

    print()
    print(f"Checked **{len(uses)}** widget keys, type parents and templates, and "
          f"**{len(bindings)}** data-binding names, that the mod's .gui files use.")
    print(f"- **{len(dropped)}** GUI types and templates the mod uses that vanilla no longer defines")
    print(f"- **{len(gone_bindings)}** data-binding names the mod uses that vanilla no longer uses")
    print()

    def sites_line(sites):
        more = f" (+{len(sites) - 1} more)" if len(sites) > 1 else ""
        return f"`{sites[0]}`{more}"

    if dropped:
        print("## GUI types and templates the mod uses that vanilla no longer defines")
        print()
        for kind, name, last, gone, sites in dropped:
            print(f"### {name}")
            print(f"- **Vanilla:** {kind} defined at {last}, gone since {gone}")
            print(f"- **Mod uses it at:** {sites_line(sites)}")
            print()
    if gone_bindings:
        print("## Data-binding names the mod uses that vanilla no longer uses")
        print()
        for name, last, gone, sites in gone_bindings:
            print(f"### {name}")
            print(f"- **Vanilla:** used in .gui files at {last}, used nowhere since {gone}")
            print(f"- **Mod uses it at:** {sites_line(sites)}")
            print()

    findings = []
    for kind, name, _last, gone, sites in dropped:
        findings.append(Finding("deps_gui_dropped", name, sites[0], f"{kind} dropped in {gone}", None,
                                {"target": f"deps:gui/{kind}/{name}", "use": kind}, gone))
    for name, _last, gone, sites in gone_bindings:
        findings.append(Finding("deps_binding_dropped", name, sites[0], f"unused since {gone}", None,
                                {"target": f"deps:binding/{name}", "use": "binding"}, gone))
    return findings


def run_deps(mod_root, base, old_hash, old_msg, new_hash, new_msg, ctx=None):
    """The whole dependency audit: run_deps_audit for script names, GUI templates
    and blocks, then run_gui_names_audit for GUI types, templates and binding names."""
    from .overrides import run_deps_audit
    findings = run_deps_audit(mod_root, base, old_hash, old_msg, new_hash, new_msg, ctx) or []
    reported = {(f.key or {}).get("target") for f in findings}
    return findings + run_gui_names_audit(mod_root, base, old_hash, old_msg, new_hash, new_msg,
                                          ctx, reported)
