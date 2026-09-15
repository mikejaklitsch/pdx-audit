"""Sources a mod is compared with besides vanilla, and the choices that name them.

A source is the vanilla tracker, a git repository or a folder. For each mod the
user chooses its foundations, the dependencies that load before it in the order
the user stores, and its adopted sources, the mods whose code it absorbed. The
mod's declared dependencies only produce suggestions. Nothing here writes into a
mod or a source; everything lives in the per-user data folder:

    <data>/<mod id>/sources.json      the mod's chosen sources
    <data>/sources/registry.json      storage key -> source id and folder
    <data>/sources/<key>.json         a source's patch assignments and snapshots
    <data>/sources/repo.git           the tracker holding folder sources' snapshots
    <data>/sources/scan.json          the cached suggestion scan
    <data>/sources/cache/             parsed indexes of source versions
    <data>/sources/tracker.lock       held while the shared tracker is written

The command line and the app call the same functions here, and both read the
stored files fresh, so a change made in one shows in the other."""
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from pdx_utilities.constants import SCAN_TOPDIRS
from pdx_utilities.paths import DEFAULT_VANILLA_ROOT, canonical_path, path_key

from . import tracker
from .config import cfg
from .safety import RefusedRemoval, remove_file
from .store import data_root, read_mod_id

VANILLA, GIT, FOLDER = "vanilla", "git", "folder"
FOUNDATION, ADOPTED = "foundation", "adopted"
ROLE_FIELD = {FOUNDATION: "foundations", ADOPTED: "adopted"}
KEY_RE = r"[A-Za-z0-9_.\-]{1,40}-[0-9a-f]{6}"
SNAPSHOT_EXTS = (".txt", ".yml", ".gui")
SNAPSHOT_INDEX_NAME = "pdx-audit-source-snapshot.index"
SNAPSHOT_INDEX_RE = r"pdx-audit-source-snapshot\.index"


class SourceError(Exception):
    """A source action that cannot be done as asked; the message says why."""


# --- storage ------------------------------------------------------------------------

def sources_dir():
    return data_root() / "sources"


def registry_path():
    return sources_dir() / "registry.json"


def choices_path(mod_id):
    return data_root() / mod_id / "sources.json"


def key_info_path(key):
    return sources_dir() / f"{key}.json"


def shared_repo():
    return sources_dir() / "repo.git"


def source_cache_dir():
    return sources_dir() / "cache"


def scan_path():
    return sources_dir() / "scan.json"


def lock_path():
    return sources_dir() / "tracker.lock"


def _read_json(path, default):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def _write_json(path, data):
    """Write through a temporary file renamed into place."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}"
    try:
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                remove_file(tmp, path.parent, rf"\.{re.escape(path.name)}\.tmp-\d+")
            except (RefusedRemoval, OSError):
                pass


def _git(git_dir, *args, timeout=120):
    """stdout of a git command against `git_dir`, or '' when it fails."""
    try:
        r = subprocess.run(["git", "-c", "safe.directory=*", f"--git-dir={git_dir}", *args],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout if r.returncode == 0 else ""


def _git_ok(git_dir, *args, timeout=600):
    try:
        r = subprocess.run(["git", "-c", "safe.directory=*", f"--git-dir={git_dir}", *args],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


# --- metadata and kinds ------------------------------------------------------------------

def read_metadata(root):
    try:
        data = json.loads((Path(root) / ".metadata" / "metadata.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _metadata_id(data):
    mid = data.get("id")
    return mid if isinstance(mid, str) and mid else None


def source_id_of(path):
    """A folder's source id: its metadata id, or the folder name when there is none."""
    return _metadata_id(read_metadata(path)) or Path(canonical_path(path)).name


def declared_dependencies(mod_root):
    """Ids the mod's metadata declares as dependencies, sorted."""
    rels = read_metadata(mod_root).get("relationships")
    return sorted({r["id"] for r in (rels if isinstance(rels, list) else [])
                   if isinstance(r, dict) and r.get("rel_type") == "dependency"
                   and isinstance(r.get("id"), str) and r["id"]})


def in_workshop(path):
    """True for a folder inside a Steam workshop content folder."""
    parts = [p.lower() for p in Path(canonical_path(path)).parts]
    return any(parts[i:i + 3] == ["steamapps", "workshop", "content"] for i in range(len(parts)))


def git_dir_of(path):
    """The git directory of a working copy whose top is `path`, or None."""
    if not (Path(path) / ".git").exists():
        return None
    try:
        r = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(path), "rev-parse",
                            "--absolute-git-dir", "--show-toplevel"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = r.stdout.strip().splitlines()
    if r.returncode != 0 or len(lines) != 2 or path_key(lines[1]) != path_key(path):
        return None
    return lines[0]


def detect_kind(path):
    """A workshop folder is a folder source even when it ships a .git, since Steam
    replaces its files without updating that repository; any other folder with a
    working .git is a git source; everything else is a folder source."""
    if in_workshop(path):
        return FOLDER
    return GIT if git_dir_of(path) else FOLDER


# --- registry and choices ----------------------------------------------------------------

def registry():
    """{key: {"id": source id, "path": canonical folder}}."""
    reg = _read_json(registry_path(), {})
    return {k: v for k, v in reg.items()
            if re.fullmatch(KEY_RE, k) and isinstance(v, dict) and isinstance(v.get("path"), str)}


def _key_for_path(reg, path):
    want = path_key(path)
    return next((k for k, e in reg.items() if path_key(e["path"]) == want), None)


def new_key(source_id):
    stem = re.sub(r"[^A-Za-z0-9_.\-]", "_", source_id)[:40].strip(".") or "source"
    return f"{stem}-{secrets.token_hex(3)}"


def load_choices(mod_id, dependencies=None):
    """The mod's stored choices. Dismissed suggestions are dropped when the mod's
    declared dependencies differ from the ones declared when they were dismissed."""
    data = _read_json(choices_path(mod_id), {})
    out = {"foundations": [], "adopted": [], "ignored_suggestions": []}
    for field in ("foundations", "adopted"):
        for e in data.get(field) or []:
            if isinstance(e, dict) and isinstance(e.get("key"), str) and isinstance(e.get("path"), str):
                entry = {"key": e["key"], "path": e["path"]}
                if e.get("kind") in (GIT, FOLDER):
                    entry["kind"] = e["kind"]
                if field == "adopted":
                    entry["rename"] = [{"from": r["from"], "to": r["to"]} for r in e.get("rename") or []
                                       if isinstance(r, dict) and isinstance(r.get("from"), str)
                                       and isinstance(r.get("to"), str)]
                out[field].append(entry)
    ignored = [s for s in data.get("ignored_suggestions") or [] if isinstance(s, str)]
    when = data.get("dependencies_when_ignored")
    if ignored and (dependencies is None or when == dependencies):
        out["ignored_suggestions"] = ignored
        out["dependencies_when_ignored"] = when if isinstance(when, list) else []
    return out


def save_choices(mod_id, choices):
    data = {"foundations": [], "adopted": [], "ignored_suggestions": list(choices["ignored_suggestions"])}
    for field in ("foundations", "adopted"):
        for e in choices[field]:
            entry = {"key": e["key"], "path": e["path"]}
            if e.get("kind"):
                entry["kind"] = e["kind"]
            if field == "adopted" and e.get("rename"):
                entry["rename"] = e["rename"]
            data[field].append(entry)
    if data["ignored_suggestions"]:
        data["dependencies_when_ignored"] = choices.get("dependencies_when_ignored") or []
    _write_json(choices_path(mod_id), data)


def key_info(key):
    info = _read_json(key_info_path(key), {})
    info["patches"] = info.get("patches") if isinstance(info.get("patches"), dict) else {}
    info["snapshots"] = info.get("snapshots") if isinstance(info.get("snapshots"), dict) else {}
    return info


def save_key_info(key, info):
    _write_json(key_info_path(key), info)


def _mod_id(mod_root):
    mod_id, err = read_mod_id(mod_root)
    if err:
        raise SourceError(err)
    return mod_id


# --- sources ----------------------------------------------------------------------------

class Source:
    """One source. `versions()` lists (commit, tag, patch) in the source's own
    history order, oldest first; `patch` is the vanilla version tag the version
    belongs to, or None while it has none."""

    def __init__(self, id, key, kind, path, role=None, rename=(), stored_kind=None):
        self.id, self.key, self.kind, self.path = id, key, kind, str(path)
        self.role, self.rename, self.stored_kind = role, list(rename), stored_kind
        self._git_dir = None

    @classmethod
    def vanilla(cls, repo):
        return cls(VANILLA, None, VANILLA, str(repo))

    def __repr__(self):
        return f"Source({self.id!r}, {self.kind}, {self.path!r})"

    @property
    def git_dir(self):
        if self.kind == VANILLA:
            return self.path
        if self.kind == FOLDER:
            return str(shared_repo())
        if self._git_dir is None:
            self._git_dir = git_dir_of(self.path) or ""
        return self._git_dir

    @property
    def cache_dir(self):
        return Path(self.path).parent / "cache" if self.kind == VANILLA else source_cache_dir()

    @property
    def cache_prefix(self):
        return "" if self.kind == VANILLA else f"{self.key}-"

    @property
    def exists(self):
        return Path(self.path).exists()

    @property
    def metadata_id(self):
        return None if self.kind == VANILLA else _metadata_id(read_metadata(self.path))

    def raw_versions(self):
        """[(commit, tag)] oldest first."""
        if self.kind == VANILLA:
            return [(h, _tag(msg)) for h, msg in reversed(tracker.get_commits(self.path))]
        if self.kind == GIT:
            return metadata_versions(self.git_dir, "HEAD") if self.git_dir else []
        return folder_versions(self.key)

    def versions(self):
        if self.kind == VANILLA:
            return [(h, t, t) for h, t in self.raw_versions()]
        patches = key_info(self.key)["patches"]
        return [(h, t, (patches.get(t) or {}).get("patch")) for h, t in self.raw_versions()]

    def tree_files(self, commit):
        return tracker.tree_files(self.git_dir, commit)

    def read_blobs(self, ids):
        return tracker.read_blobs(self.git_dir, ids)


def _tag(msg):
    parts = (msg or "").split()
    return parts[0] if parts else ""


def _ref_safe(tag):
    tag = re.sub(r"[^A-Za-z0-9_.\-+]", "_", tag).strip(".")
    tag = re.sub(r"\.{2,}", ".", tag)
    return tag[:-5] + "_lock" if tag.endswith(".lock") else (tag or "version")


def _unique(tag, taken):
    if tag not in taken:
        return tag
    n = 2
    while f"{tag}.{n}" in taken:
        n += 1
    return f"{tag}.{n}"


def _cat_specs(git_dir, specs):
    """Contents of `rev:path` specs, None where git has none, in order."""
    if not specs:
        return []
    try:
        r = subprocess.run(["git", "-c", "safe.directory=*", f"--git-dir={git_dir}", "cat-file", "--batch"],
                           input="".join(f"{s}\n" for s in specs).encode(), capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return [None] * len(specs)
    out, pos, res = r.stdout, 0, []
    for _ in specs:
        eol = out.find(b"\n", pos)
        if eol == -1:
            res.append(None)
            continue
        header = out[pos:eol].split()
        pos = eol + 1
        if len(header) != 3:
            res.append(None)
            continue
        size = int(header[2])
        res.append(out[pos:pos + size])
        pos += size + 1
    return res


def _version_of(raw):
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return None
    v = data.get("version") if isinstance(data, dict) else None
    return str(v).strip() if isinstance(v, (str, int, float)) and str(v).strip() else None


def metadata_versions(git_dir, rev):
    """[(commit, tag)] oldest first along the first-parent history of `rev`: every
    commit where `.metadata/metadata.json`'s version changes, tagged with that
    version, then `rev` itself, tagged with its short hash when it is not one of them."""
    head = _git(git_dir, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").strip()
    if not head:
        return []
    log = _git(git_dir, "log", "--first-parent", "--format=%H", head, "--", ".metadata/metadata.json").split()
    log.reverse()
    out, taken, prev = [], set(), None
    for h, raw in zip(log, _cat_specs(git_dir, [f"{h}:.metadata/metadata.json" for h in log])):
        v = _version_of(raw)
        if v and v != prev:
            tag = _unique(_ref_safe(v), taken)
            taken.add(tag)
            out.append((h, tag))
            prev = v
    if not out or out[-1][0] != head:
        out.append((head, _unique(head[:7], taken)))
    return out


def folder_versions(key):
    """[(commit, tag)] oldest first for a folder source's snapshots in the shared tracker."""
    repo = shared_repo()
    if not repo.is_dir():
        return []
    head = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/sources/{key}/head").strip()
    if not head:
        return []
    tags = {}
    prefix = f"refs/sources/{key}/tags/"
    for line in _git(repo, "for-each-ref", "--format=%(objectname) %(refname)", prefix).splitlines():
        obj, _sp, ref = line.partition(" ")
        if ref.startswith(prefix):
            tags.setdefault(obj, ref[len(prefix):])
    chain = _git(repo, "rev-list", "--first-parent", head).split()
    return [(h, tags[h]) for h in reversed(chain) if h in tags]


class ModSources:
    """A mod's chosen sources. Sources whose folder is gone are listed in `missing`
    and left out of `foundations` and `adopted`."""

    def __init__(self, mod_id, choices, reg, dependencies):
        self.mod_id, self.choices, self.dependencies = mod_id, choices, dependencies
        self.foundations, self.adopted, self.missing = [], [], []
        for role, field in ROLE_FIELD.items():
            for e in choices[field]:
                entry = reg.get(e["key"]) or {}
                path = entry.get("path") or e["path"]
                sid = entry.get("id") or source_id_of(path)
                kind = e.get("kind") or (detect_kind(path) if Path(path).is_dir() else FOLDER)
                src = Source(sid, e["key"], kind, path, role, e.get("rename") or (), e.get("kind"))
                if Path(path).is_dir():
                    (self.foundations if role == FOUNDATION else self.adopted).append(src)
                else:
                    self.missing.append(src)

    @property
    def all(self):
        return self.foundations + self.adopted + self.missing

    def by_id(self, sid):
        return next((s for s in self.all if s.id == sid), None)

    def warnings(self):
        return [f"Warning: source {s.id} is stored at {s.path}, which no longer exists; it is left out "
                f"of this run. Relocate it with `pdx-audit --relocate-source {s.id} <folder>` or remove "
                f"it with `pdx-audit --remove-source {s.id}`." for s in self.missing]


def load_sources(mod_root):
    """The mod's ModSources, read fresh from the stored files."""
    mod_id = _mod_id(mod_root)
    deps = declared_dependencies(mod_root)
    return ModSources(mod_id, load_choices(mod_id, deps), registry(), deps)


def _find_entry(choices, reg, sid):
    """(field, index) of the chosen source with id `sid`, or None."""
    for field in ("foundations", "adopted"):
        for i, e in enumerate(choices[field]):
            entry = reg.get(e["key"]) or {}
            if (entry.get("id") or source_id_of(entry.get("path") or e["path"])) == sid:
                return field, i
    return None


def _require(mod_root, sid):
    mod_id = _mod_id(mod_root)
    deps = declared_dependencies(mod_root)
    choices, reg = load_choices(mod_id, deps), registry()
    at = _find_entry(choices, reg, sid)
    if at is None:
        known = sorted(s.id for s in ModSources(mod_id, choices, reg, deps).all)
        raise SourceError(f"{sid} is not a chosen source of {mod_id}"
                          + (f"; chosen: {', '.join(known)}" if known else "; it has none"))
    return mod_id, choices, reg, at


# --- patches ----------------------------------------------------------------------------

def patch_order(vanilla_repo):
    """Tracked vanilla version tags, oldest first."""
    return [_tag(msg) for _h, msg in reversed(tracker.get_commits(vanilla_repo))] if vanilla_repo else []


def record_versions(source, vanilla_repo, stale=False, adding=False):
    """Give each version of `source` that pdx-audit has not recorded yet a patch:
    the newest tracked patch, never below the patch of the nearest earlier version
    that has one. History older than the newest recorded version, and on `adding`
    every version but the newest, is recorded without a patch. Returns warnings."""
    if source.kind == VANILLA:
        return []
    versions = source.raw_versions()
    info = key_info(source.key)
    patches = info["patches"]
    adding = adding or not patches          # nothing recorded yet: everything before now is history
    order = patch_order(vanilla_repo)
    pos = {t: i for i, t in enumerate(order)}
    newest_recorded = max((i for i, (_c, t) in enumerate(versions) if t in patches), default=-1)
    warnings, changed = [], False
    for i, (_commit, tag) in enumerate(versions):
        if tag in patches:
            continue
        changed = True
        if i < newest_recorded or (adding and i < len(versions) - 1):
            patches[tag] = {"patch": None, "how": "history"}
            continue
        prev = next((patches[t]["patch"] for _c, t in reversed(versions[:i])
                     if (patches.get(t) or {}).get("patch")), None)
        if not order or (prev is not None and prev not in pos):
            patches[tag] = {"patch": None, "how": "unpatched"}
            why = (f"the previous version's patch {prev} is not in the vanilla tracker" if order
                   else "the vanilla tracker has no snapshots")
            warnings.append(f"Warning: {source.id} {tag} was left without a patch: {why}. Assign one with "
                            f"`pdx-audit --patch {source.id} {tag} <patch>`.")
            continue
        default = order[-1]
        if prev is not None and pos[prev] > pos[default]:
            default = prev
        entry = {"patch": default, "how": "default"}
        if stale:
            entry["lagged"] = True
            warnings.append(f"Warning: {source.id} {tag} was given patch {default} while the vanilla "
                            f"tracker is out of date. Snapshot the game, then assign the right patch with "
                            f"`pdx-audit --patch {source.id} {tag} <patch>`.")
        patches[tag] = entry
    if changed:
        save_key_info(source.key, info)
    return warnings


def assign_patch(mod_root, sid, versions, patch, vanilla_repo):
    """Assign `patch` to one version or a run of consecutive versions ('a..b').
    Patches never decrease along a source's history; an assignment that would break
    this is refused, naming the conflicting version. Returns messages."""
    _mod, choices, reg, (field, _i) = _require(mod_root, sid)
    if field == "adopted":
        raise SourceError(f"{sid} is an adopted source; patches place a foundation's versions in the stack, "
                          f"while an adopted source is compared with all of its versions")
    src = load_sources(mod_root).by_id(sid)
    tags = [t for _c, t in src.raw_versions()]
    first, _dots, last = versions.partition("..")
    last = last or first
    for t in (first, last):
        if t not in tags:
            raise SourceError(f"{sid} has no version {t}; its versions: {', '.join(tags) or 'none'}")
    i, j = tags.index(first), tags.index(last)
    if i > j:
        raise SourceError(f"{first} comes after {last} in {sid}'s history; name the older version first")
    order = patch_order(vanilla_repo)
    pos = {t: n for n, t in enumerate(order)}
    if patch not in pos:
        raise SourceError(f"{patch} is not a tracked vanilla version; tracked: {', '.join(order) or 'none'}")
    info = key_info(src.key)
    patches = info["patches"]
    proposed = {t: (patches.get(t) or {}).get("patch") for t in tags}
    run = set(tags[i:j + 1])
    for t in run:
        proposed[t] = patch
    seen = None
    for t in tags:
        p = proposed[t]
        if p is None or p not in pos:
            continue
        if seen is not None and pos[p] < pos[seen[1]]:
            other = seen[0] if t in run else t
            raise SourceError(f"patches never decrease along {sid}'s history: {other} has patch "
                              f"{proposed[other]}, so {patch} cannot be assigned to "
                              f"{first if first == last else f'{first}..{last}'}")
        seen = (t, p)
    for t in tags[i:j + 1]:
        patches[t] = {"patch": patch, "how": "chosen"}
    save_key_info(src.key, info)
    n = j - i + 1
    return [f"Assigned patch {patch} to {n} version{'s' if n != 1 else ''} of {sid}."]


# --- the shared tracker ------------------------------------------------------------------

def _process_start(pid):
    """A token identifying the process with `pid` while it lives, or None when no
    such process exists."""
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return None
        return stat.rsplit(")", 1)[1].split()[19]
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        ctypes.windll.kernel32.CloseHandle(handle)
        return ""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    return ""


@contextmanager
def tracker_lock(timeout=60.0, poll=0.2):
    """Hold <data>/sources/tracker.lock while writing the shared tracker. A second
    writer waits, then fails naming the holder. A lock whose process is gone is
    stale and is removed before retrying."""
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    me = {"pid": os.getpid(), "start": _process_start(os.getpid())}
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = _read_json(path, {})
            pid = holder.get("pid")
            if isinstance(pid, int):
                start = _process_start(pid)
                alive = start is not None and (holder.get("start") in (None, "") or start == holder["start"])
            else:
                # a lock another writer has created but not yet filled in
                stamp = _stamp(path)
                alive = stamp is not None and time.time_ns() - stamp[0] < 5_000_000_000
            if not alive:
                try:
                    remove_file(path, path.parent, r"tracker\.lock")
                except (RefusedRemoval, OSError):
                    pass
                continue
            if time.monotonic() >= deadline:
                raise SourceError(f"the source tracker is being written by process {pid}; try again "
                                  f"when it finishes (lock: {path})")
            time.sleep(poll)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(me, fh)
        break
    try:
        yield
    finally:
        try:
            remove_file(path, path.parent, r"tracker\.lock")
        except (RefusedRemoval, OSError):
            pass


def _ensure_shared_repo():
    repo = shared_repo()
    if not repo.is_dir():
        repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--bare", "--quiet", str(repo)], check=True, capture_output=True)
    return repo


def snapshot_files(root):
    """The files a folder snapshot stores: script, GUI and localization files under
    the module roots, and the metadata file."""
    root = Path(root)
    files = {p for top in SCAN_TOPDIRS if (root / top).is_dir()
             for p in (root / top).rglob("*") if p.suffix in SNAPSHOT_EXTS and p.is_file()}
    meta = root / ".metadata" / "metadata.json"
    if meta.is_file():
        files.add(meta)
    return sorted(files)


def folder_digest(root):
    """A digest of a folder's snapshot files that ignores carriage returns."""
    root, h = Path(root), hashlib.sha1()
    for fp in snapshot_files(root):
        try:
            data = fp.read_bytes().replace(b"\r", b"")
        except OSError:
            continue
        h.update(fp.relative_to(root).as_posix().encode("utf-8") + b"\0")
        h.update(hashlib.sha1(data).digest())
    return h.hexdigest()


def snapshot_source(mod_root, sid, vanilla_repo, stale=False, timeout=60.0):
    """Record a folder source's current files as a new version in the shared tracker.
    On the first snapshot of a folder that ships a .git, that repository's
    first-parent history is imported first. Returns messages."""
    src = load_sources(mod_root).by_id(sid)
    if src is None:
        _require(mod_root, sid)
    if src.kind != FOLDER:
        raise SourceError(f"{sid} is a git source; its versions are its commits, so there is nothing to snapshot")
    if not Path(src.path).is_dir():
        raise SourceError(f"{sid}'s folder {src.path} no longer exists")
    messages, first = [], False
    with tracker_lock(timeout):
        repo = _ensure_shared_repo()
        head_ref = f"refs/sources/{src.key}/head"
        parent = _git(repo, "rev-parse", "--verify", "--quiet", head_ref).strip()
        taken = {t for _c, t in folder_versions(src.key)}
        if not parent:
            first = True
            upstream = git_dir_of(src.path)
            import_ref = f"refs/sources/{src.key}/import"
            if upstream and _git_ok(repo, "fetch", "--no-tags", "--quiet", upstream, f"+HEAD:{import_ref}"):
                imported = metadata_versions(repo, import_ref)
                for commit, tag in imported:
                    _git_ok(repo, "update-ref", f"refs/sources/{src.key}/tags/{tag}", commit)
                    taken.add(tag)
                parent = imported[-1][0] if imported else ""
                if parent:
                    _git_ok(repo, "update-ref", head_ref, parent)
                    messages.append(f"Imported {len(imported)} versions from {sid}'s git history.")
        files = snapshot_files(src.path)
        tree = tracker.snapshot_tree(repo, src.path, files, SNAPSHOT_INDEX_NAME, SNAPSHOT_INDEX_RE)
        if parent and (_git(repo, "rev-parse", f"{parent}^{{tree}}").strip() == tree
                       or matches_folder(repo, parent, src.path)):
            tag = next((t for c, t in folder_versions(src.key) if c == parent), None)
            messages.append(f"No changes from {sid}'s newest snapshot{f' ({tag})' if tag else ''}, "
                            f"line endings aside; nothing committed.")
        else:
            version = _version_of((Path(src.path) / ".metadata" / "metadata.json").read_bytes()
                                  if (Path(src.path) / ".metadata" / "metadata.json").is_file() else None)
            tag = _unique(_ref_safe(version or "snapshot"), taken)
            commit = tracker.commit_tree(repo, tree, parent or None, tag)
            _git_ok(repo, "update-ref", head_ref, commit)
            _git_ok(repo, "update-ref", f"refs/sources/{src.key}/tags/{tag}", commit)
            messages.append(f"Snapshotted {sid} as {tag} ({len(files)} files).")
    if tag:
        info = key_info(src.key)
        info["snapshots"][tag] = {"manifest": workshop_manifest(src.path), "digest": folder_digest(src.path)}
        save_key_info(src.key, info)
    return messages + record_versions(src, vanilla_repo, stale, adding=first)


# --- adding and changing choices ------------------------------------------------------------

def add_source(mod_root, path, role, kind=None, replace=False, vanilla_repo=None, stale=False):
    """Choose a folder as a foundation or an adopted source of the mod. A folder
    already in the registry keeps its storage key. Returns messages."""
    if role not in ROLE_FIELD:
        raise SourceError(f"a source is added as {' or '.join(ROLE_FIELD)}, not {role}")
    if kind not in (None, GIT, FOLDER):
        raise SourceError(f"a source's kind is git or folder, not {kind}")
    mod_id = _mod_id(mod_root)
    canon = canonical_path(path)
    if not Path(canon).is_dir():
        raise SourceError(f"{canon} is not a folder")
    if path_key(canon) == path_key(mod_root):
        raise SourceError("a mod cannot be its own source")
    deps = declared_dependencies(mod_root)
    choices, reg = load_choices(mod_id, deps), registry()
    messages = []
    key = _key_for_path(reg, canon)
    sid = reg[key]["id"] if key else source_id_of(canon)
    at = _find_entry(choices, reg, sid)
    if at is not None:
        field, i = at
        held = choices[field][i]
        gone = not Path((reg.get(held["key"]) or {}).get("path") or held["path"]).is_dir()
        if not replace:
            hint = (f" Its stored folder {held['path']} no longer exists; if this folder is it, keep its "
                    f"snapshots and patches with `pdx-audit --relocate-source {sid} \"{canon}\"`." if gone else "")
            raise SourceError(f"{sid} is already a chosen source of {mod_id}; pass --replace to replace "
                              f"it.{hint}")
        del choices[field][i]
        messages.append(f"Replaced the chosen source {sid}.")
    if key is None:
        for k, e in reg.items():
            if e.get("id") == sid and not Path(e["path"]).is_dir():
                messages.append(f"Note: a source with id {sid} was stored at {e['path']}, which no longer "
                                f"exists. If this folder is that source, moved, remove this one and keep its "
                                f"snapshots and patches with `pdx-audit --relocate-source {sid} \"{canon}\"`.")
        key = new_key(sid)
        reg[key] = {"id": sid, "path": canon}
        _write_json(registry_path(), reg)
    entry = {"key": key, "path": canon}
    if kind:
        entry["kind"] = kind
    if role == ADOPTED:
        entry["rename"] = []
    choices[ROLE_FIELD[role]].append(entry)
    save_choices(mod_id, choices)
    src = load_sources(mod_root).by_id(sid)
    messages.insert(0, f"Added {sid} ({src.kind}, {canon}) as {'a foundation' if role == FOUNDATION else 'an adopted source'} "
                       f"of {mod_id}.")
    if vanilla_repo and src.kind == GIT:
        messages += record_versions(src, vanilla_repo, stale, adding=not key_info(key)["patches"])
    if src.kind == FOLDER and not folder_versions(key):
        messages.append(f"Take its first snapshot with `pdx-audit --snapshot-source {sid}`.")
    return messages


def remove_source(mod_root, sid):
    mod_id, choices, _reg, (field, i) = _require(mod_root, sid)
    del choices[field][i]
    save_choices(mod_id, choices)
    return [f"Removed {sid} from {mod_id}'s sources."]


def relocate_source(mod_root, sid, path):
    """Point a chosen source, or a stored source whose folder is gone, at the folder
    it moved to, keeping its storage key and so its snapshots and patches."""
    mod_id = _mod_id(mod_root)
    canon = canonical_path(path)
    if not Path(canon).is_dir():
        raise SourceError(f"{canon} is not a folder")
    held = _metadata_id(read_metadata(canon))
    if held and held != sid:
        raise SourceError(f"{canon} holds {held}, not {sid}; add it as a source of its own instead")
    deps = declared_dependencies(mod_root)
    choices, reg = load_choices(mod_id, deps), registry()
    other = _key_for_path(reg, canon)
    at = _find_entry(choices, reg, sid)
    if at is not None:
        key = choices[at[0]][at[1]]["key"]
    else:
        gone = [k for k, e in reg.items() if e.get("id") == sid and not Path(e["path"]).is_dir()]
        if len(gone) != 1:
            raise SourceError(f"{sid} is not a chosen source of {mod_id}, and no single stored source "
                              f"with that id has lost its folder")
        key = gone[0]
    if other and other != key:
        raise SourceError(f"{canon} is already stored as the source {reg[other]['id']}")
    reg[key] = {"id": sid, "path": canon}
    _write_json(registry_path(), reg)
    stale_dupes = [(f, i) for f in ("foundations", "adopted") for i, e in enumerate(choices[f])
                   if e["key"] != key and (reg.get(e["key"]) or {}).get("id") == sid]
    for f, i in reversed(stale_dupes):
        del choices[f][i]
    if at is not None:
        choices[at[0]][at[1]]["path"] = canon
        save_choices(mod_id, choices)
    return [f"Relocated {sid} to {canon}; its snapshots and patches are kept."]


def move_source(mod_root, sid, position):
    """Move a foundation to `position` (1 = loads first) in the stored order."""
    mod_id, choices, _reg, (field, i) = _require(mod_root, sid)
    if field != "foundations":
        raise SourceError(f"{sid} is an adopted source; only foundations have a load order")
    try:
        position = int(position)
    except (TypeError, ValueError):
        raise SourceError(f"a position is a number, not {position}")
    n = len(choices["foundations"])
    if not 1 <= position <= n:
        raise SourceError(f"a position is between 1 and {n}")
    entry = choices["foundations"].pop(i)
    choices["foundations"].insert(position - 1, entry)
    save_choices(mod_id, choices)
    return [f"{sid} now loads {_ordinal(position)} of {n} foundations."]


def set_foundation_order(mod_root, ids):
    """Store the foundations in the order of `ids`, which names each exactly once."""
    mod_id = _mod_id(mod_root)
    deps = declared_dependencies(mod_root)
    choices, reg = load_choices(mod_id, deps), registry()
    by_id = {(reg.get(e["key"]) or {}).get("id") or source_id_of(e["path"]): e for e in choices["foundations"]}
    if sorted(ids) != sorted(by_id):
        raise SourceError("the new order must name every foundation once")
    choices["foundations"] = [by_id[i] for i in ids]
    save_choices(mod_id, choices)
    return ["Foundation order: " + ", ".join(ids) + "."]


def _ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def set_kind(mod_root, sid, kind):
    if kind not in (GIT, FOLDER, "auto"):
        raise SourceError(f"a kind is git, folder or auto, not {kind}")
    mod_id, choices, _reg, (field, i) = _require(mod_root, sid)
    if kind == "auto":
        choices[field][i].pop("kind", None)
    else:
        choices[field][i]["kind"] = kind
    save_choices(mod_id, choices)
    now = load_sources(mod_root).by_id(sid)
    return [f"{sid} is a {now.kind} source" + (" (detected)." if kind == "auto" else ".")]


def add_rename(mod_root, sid, frm, to):
    """A rename rule for an adopted source: `frm` on the source side reads as `to`."""
    mod_id, choices, _reg, (field, i) = _require(mod_root, sid)
    if field != "adopted":
        raise SourceError(f"{sid} is a foundation; rename rules apply to adopted sources")
    if not frm or not to or frm == to:
        raise SourceError("a rename rule needs two different names")
    old = next((r["to"] for r in choices[field][i].get("rename") or [] if r["from"] == frm), None)
    rules = [r for r in choices[field][i].get("rename") or [] if r["from"] != frm]
    rules.append({"from": frm, "to": to})
    choices[field][i]["rename"] = rules
    save_choices(mod_id, choices)
    return [f"{sid}: {frm} reads as {to}" + (f", replacing the rule {frm} → {old}." if old and old != to else ".")]


def remove_rename(mod_root, sid, frm):
    mod_id, choices, _reg, (field, i) = _require(mod_root, sid)
    rules = choices[field][i].get("rename") or []
    kept = [r for r in rules if r["from"] != frm]
    if len(kept) == len(rules):
        raise SourceError(f"{sid} has no rename rule from {frm}")
    choices[field][i]["rename"] = kept
    save_choices(mod_id, choices)
    return [f"{sid}: removed the rename rule from {frm}."]


def ignore_suggestion(mod_root, sid):
    """Stop suggesting sources with metadata id `sid` until the mod's declared
    dependencies change."""
    mod_id = _mod_id(mod_root)
    deps = declared_dependencies(mod_root)
    choices = load_choices(mod_id, deps)
    if sid not in choices["ignored_suggestions"]:
        choices["ignored_suggestions"].append(sid)
    choices["dependencies_when_ignored"] = deps
    save_choices(mod_id, choices)
    messages = [f"{sid} is no longer suggested for {mod_id} until its declared dependencies change."]
    scanned = {e.get("id") for e in (_read_json(scan_path(), {}).get("dirs") or {}).values() if isinstance(e, dict)}
    if sid not in deps and sid not in scanned:
        messages.append(f"Note: {sid} is not a dependency {mod_id} declares, nor a folder the last scan found; "
                        f"check the id with `pdx-audit --sources`.")
    return messages


# --- suggestions --------------------------------------------------------------------------

def _stamp(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def parse_vdf(text):
    """A Valve KeyValues text as nested dicts."""
    tokens = re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])', text)
    stack, key, root = [{}], None, None
    root = stack[0]
    for quoted, brace in tokens:
        if brace == "{":
            child = {}
            if key is not None:
                stack[-1][key] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif key is None:
            key = quoted.replace("\\\\", "\\")
        else:
            stack[-1][key] = quoted.replace("\\\\", "\\")
            key = None
    return root


def _game_root():
    return Path(os.environ.get("PDX_GAME_ROOT") or cfg("game_root") or str(DEFAULT_VANILLA_ROOT))


def steam_layout(game_root=None):
    """{"appid", "libraries", "vdf"} for the Steam install holding the game, or None
    when the game folder is not inside a Steam library."""
    root = Path(canonical_path(game_root or _game_root()))
    parts = root.parts
    lowered = [p.lower() for p in parts]
    if "steamapps" not in lowered:
        return None
    i = len(lowered) - 1 - lowered[::-1].index("steamapps")
    library = Path(*parts[:i])
    install = parts[i + 2] if len(parts) > i + 2 and lowered[i + 1] == "common" else None
    appid = None
    for acf in sorted((library / "steamapps").glob("appmanifest_*.acf")):
        try:
            state = parse_vdf(acf.read_text(encoding="utf-8", errors="replace")).get("AppState") or {}
        except OSError:
            continue
        if install and state.get("installdir") == install:
            appid = state.get("appid") or acf.stem.split("_", 1)[1]
            break
    if not appid:
        return None
    vdf = library / "steamapps" / "libraryfolders.vdf"
    libraries = [library]
    if vdf.is_file():
        try:
            data = parse_vdf(vdf.read_text(encoding="utf-8", errors="replace")).get("libraryfolders") or {}
        except OSError:
            data = {}
        for entry in data.values():
            if isinstance(entry, dict) and entry.get("path"):
                lib = Path(canonical_path(entry["path"]))
                if (lib / "steamapps" / "workshop" / "content" / appid).is_dir() and \
                        path_key(lib) not in {path_key(l) for l in libraries}:
                    libraries.append(lib)
    return {"appid": appid, "libraries": libraries, "vdf": vdf if vdf.is_file() else None}


def user_game_dir(mod_root):
    """The game's user folder (the one holding playsets.json and the local mod
    folder) above the mod, or None."""
    here = Path(canonical_path(mod_root))
    for p in [here, *here.parents]:
        if (p / "playsets.json").is_file():
            return p
    for p in [here, *here.parents]:
        if p.name == "mod":
            return p.parent
    return None


def _playset_paths(user_dir):
    data = _read_json(user_dir / "playsets.json", {}) if user_dir else {}
    out = []
    for ps in data.get("playsets") or []:
        for m in (ps.get("orderedListMods") or []) if isinstance(ps, dict) else []:
            if isinstance(m, dict) and isinstance(m.get("path"), str):
                out.append(canonical_path(m["path"]))
    return sorted(set(out))


def _candidates(mod_root, game_root=None):
    """(local candidate folders, workshop candidate folders, stamps)."""
    user_dir = user_game_dir(mod_root)
    layout = steam_layout(game_root)
    stamps, local, workshop = {}, set(), set()
    if user_dir:
        mods = user_dir / "mod"
        stamps[str(mods)] = _stamp(mods)
        if mods.is_dir():
            local.update(canonical_path(p) for p in mods.iterdir() if p.is_dir())
        stamps[str(user_dir / "playsets.json")] = _stamp(user_dir / "playsets.json")
        for p in _playset_paths(user_dir):
            if in_workshop(p):
                workshop.add(p)
            elif Path(p).is_dir():
                local.add(p)
    if layout:
        if layout["vdf"]:
            stamps[str(layout["vdf"])] = _stamp(layout["vdf"])
        for lib in layout["libraries"]:
            acf = lib / "steamapps" / "workshop" / f"appworkshop_{layout['appid']}.acf"
            stamps[str(acf)] = _stamp(acf)
            content = lib / "steamapps" / "workshop" / "content" / layout["appid"]
            if content.is_dir():
                workshop.update(canonical_path(p) for p in content.iterdir() if p.is_dir())
    for p in local:
        meta = Path(p) / ".metadata" / "metadata.json"
        stamps[str(meta)] = _stamp(meta)
    return sorted(local), sorted(workshop - local), stamps, (str(user_dir), str(game_root or _game_root()))


def refresh_scan(mod_root, game_root=None):
    """Scan every candidate folder's metadata and store the result in scan.json."""
    local, workshop, stamps, context = _candidates(mod_root, game_root)
    dirs = {}
    for p in local + workshop:
        data = read_metadata(p)
        if not data and not in_workshop(p):
            continue
        dirs[p] = {"id": _metadata_id(data) or Path(p).name, "git": (Path(p) / ".git").exists(),
                   "workshop": in_workshop(p)}
    scan = {"context": list(context), "stamps": stamps, "dirs": dirs}
    _write_json(scan_path(), scan)
    return scan


def cached_scan(mod_root, game_root=None):
    """The stored scan while every stamp in it still matches, else None. Costs one
    stat per stamp; no candidate's metadata is read."""
    scan = _read_json(scan_path(), {})
    if not scan.get("stamps") or not isinstance(scan.get("dirs"), dict):
        return None
    user_dir = user_game_dir(mod_root)
    if scan.get("context") != [str(user_dir), str(game_root or _game_root())]:
        return None
    for path, stamp in scan["stamps"].items():
        if _stamp(path) != stamp:
            return None
    return scan


def suggestions(mod_root, scan, sources=None):
    """Folders to offer as sources, each {"id", "path", "reason", "git", "workshop"}:
    a folder whose id matches a declared dependency (reason 'dependency'), and while
    a declared dependency is not chosen, each local git repository whose id matches
    no dependency, which can stand in for one (reason 'git')."""
    sources = sources or load_sources(mod_root)
    deps = sources.dependencies
    ignored = set(sources.choices["ignored_suggestions"])
    chosen_ids = {s.id for s in sources.all}
    chosen_paths = {path_key(s.path) for s in sources.all} | {path_key(mod_root)}
    out = []
    dirs = sorted((scan or {}).get("dirs", {}).items())
    for path, e in dirs:
        if path_key(path) in chosen_paths or e["id"] in ignored:
            continue
        if e["id"] in deps and e["id"] not in chosen_ids:
            out.append(dict(e, path=path, reason="dependency"))
    if any(d not in chosen_ids and d not in ignored for d in deps):
        for path, e in dirs:
            if (e.get("git") and not e.get("workshop") and e["id"] not in deps and e["id"] not in ignored
                    and path_key(path) not in chosen_paths):
                out.append(dict(e, path=path, reason="git"))
    return out


def suggestion_note(mod_root, game_root=None):
    """The stderr note a run prints about suggested foundations, or None. A run never
    scans: without a current scan it only says to refresh it."""
    try:
        sources = load_sources(mod_root)
    except SourceError:
        return None
    ignored = set(sources.choices["ignored_suggestions"])
    chosen = {s.id for s in sources.all}
    open_deps = [d for d in sources.dependencies if d not in chosen and d not in ignored]
    if not open_deps:
        return None
    scan = cached_scan(mod_root, game_root)
    if scan is None:
        return ("Note: source suggestions are out of date; run `pdx-audit --sources` to see "
                "which declared dependencies are installed.")
    found = [s for s in suggestions(mod_root, scan, sources) if s["reason"] == "dependency"]
    if not found:
        return None
    lines = [f"Note: {sources.mod_id} declares dependencies installed on this machine that are not chosen as "
             f"foundations:"]
    lines += [f"  {s['id']}: {s['path']}" for s in found]
    lines.append("Add one with `pdx-audit --add-source <folder> --as foundation`, or stop suggesting it with "
                 "`pdx-audit --ignore-suggestion <id>`.")
    return "\n".join(lines)


# --- freshness ------------------------------------------------------------------------

def workshop_manifest(path):
    """Steam's manifest id for a workshop folder's installed item, or None."""
    p = Path(canonical_path(path))
    lowered = [x.lower() for x in p.parts]
    for i in range(len(lowered) - 4):
        if lowered[i:i + 3] == ["steamapps", "workshop", "content"] and len(lowered) == i + 5:
            appid, item = p.parts[i + 3], p.parts[i + 4]
            acf = Path(*p.parts[:i + 2]) / f"appworkshop_{appid}.acf"
            try:
                data = parse_vdf(acf.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                return None
            items = (data.get("AppWorkshop") or {}).get("WorkshopItemsInstalled") or {}
            entry = items.get(item)
            return entry.get("manifest") if isinstance(entry, dict) else None
    return None


def freshness(source, full=False):
    """Messages saying a source changed after its newest recorded version. `full`
    also compares a folder's files (ignoring carriage returns) or a git source's
    working tree, which reads every file."""
    if source.kind == VANILLA or not Path(source.path).is_dir():
        return []
    if source.kind == FOLDER:
        versions = folder_versions(source.key)
        if not versions:
            return [f"{source.id} has no snapshot yet; take one with `pdx-audit --snapshot-source {source.id}`."]
        tag = versions[-1][1]
        snap = key_info(source.key)["snapshots"].get(tag) or {}
        out = []
        manifest = workshop_manifest(source.path)
        if manifest and snap.get("manifest") and manifest != snap["manifest"]:
            out.append(f"Steam updated {source.id} after its snapshot {tag}; take a new one with "
                       f"`pdx-audit --snapshot-source {source.id}`.")
        elif full and snap.get("digest") and folder_digest(source.path) != snap["digest"]:
            out.append(f"{source.id}'s files changed after its snapshot {tag}; take a new one with "
                       f"`pdx-audit --snapshot-source {source.id}`.")
        return out
    versions = source.raw_versions()
    patches = key_info(source.key)["patches"]
    out = []
    if versions and versions[-1][1] not in patches:
        out.append(f"{source.id}'s HEAD moved past its newest recorded version.")
    if full and source.git_dir and not matches_folder(source.git_dir, "HEAD", source.path):
        out.append(f"{source.id}'s working tree has changes not committed to HEAD.")
    return out


def _snapshot_path(path):
    parts = path.split("/")
    return (path == ".metadata/metadata.json"
            or (len(parts) > 1 and parts[0] in SCAN_TOPDIRS and path.endswith(SNAPSHOT_EXTS)))


def matches_folder(git_dir, commit, root):
    """True when a commit holds exactly a folder's snapshot files with the same
    contents, ignoring carriage returns. Only reads the repository and the files;
    no git command runs against the folder, so git never refreshes its index."""
    root = Path(root)
    tracked = {p: b for p, b in tracker.tree_files(git_dir, commit) if _snapshot_path(p)}
    files = {fp.relative_to(root).as_posix(): fp for fp in snapshot_files(root)}
    if set(tracked) != set(files):
        return False
    blobs = tracker.read_blobs(git_dir, list(tracked.values()))
    for rel, fp in files.items():
        try:
            data = fp.read_bytes()
        except OSError:
            return False
        if data.replace(b"\r", b"") != blobs.get(tracked[rel], b"").replace(b"\r", b""):
            return False
    return True


# --- orphaned sources --------------------------------------------------------------------

def orphaned_sources():
    """Registry keys no mod's sources.json references."""
    reg = registry()
    used = set()
    root = data_root()
    if root.is_dir():
        for f in root.glob("*/sources.json"):
            data = _read_json(f, {})
            for field in ("foundations", "adopted"):
                used.update(e.get("key") for e in data.get(field) or [] if isinstance(e, dict))
    return sorted(k for k in reg if k not in used)


def orphan_sources_note():
    orphans = orphaned_sources()
    if not orphans:
        return None
    reg = registry()
    shown = ", ".join(f"{reg[k]['id']} ({k})" for k in orphans)
    return (f"Note: {len(orphans)} stored source(s) are chosen by no mod: {shown}\n"
            f"Remove them with: pdx-audit --remove-orphaned-sources")


def remove_orphaned_sources(force=False, timeout=60.0):
    """List orphaned sources, confirm (unless force), then delete each one's refs,
    patch file and cache files, and its registry entry. Git's own garbage collection
    then drops unreferenced objects once git's default expiry passes. Returns a
    process exit code."""
    orphans = orphaned_sources()
    if not orphans:
        print("No orphaned sources.")
        return 0
    reg = registry()
    print("Stored sources chosen by no mod:")
    for k in orphans:
        print(f"  {reg[k]['id']} ({k}): {reg[k]['path']}")
    if not force:
        if not sys.stdin.isatty():
            print("Refusing to remove sources without an interactive confirmation. Run this in a terminal, "
                  "or add --force.", file=sys.stderr)
            return 1
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            print("Aborted. No sources were removed.")
            return 0
    with tracker_lock(timeout):
        still = set(orphaned_sources())
        reg = registry()
        repo = shared_repo()
        removed = 0
        for k in orphans:
            if k not in still:
                print(f"  kept {k}: a mod chose it again")
                continue
            if repo.is_dir():
                for ref in _git(repo, "for-each-ref", "--format=%(refname)", f"refs/sources/{k}/").split():
                    _git_ok(repo, "update-ref", "-d", ref)
            for fp in [key_info_path(k), *sorted(source_cache_dir().glob(f"{k}-*.json"))]:
                if fp.exists():
                    try:
                        remove_file(fp, fp.parent, re.escape(k) + (r"\.json" if fp.parent == sources_dir()
                                                                   else "-" + tracker.CACHE_FILE_RE))
                    except (RefusedRemoval, OSError) as e:
                        print(f"  kept {fp}: {e}", file=sys.stderr)
            reg.pop(k, None)
            removed += 1
            print(f"  removed {k}")
        _write_json(registry_path(), reg)
        if repo.is_dir():
            _git_ok(repo, "gc", "--quiet")
    print(f"Removed {removed} source(s).")
    return 0


# --- the --sources listing ------------------------------------------------------------------

def _patch_label(entry):
    if not entry or not entry.get("patch"):
        return "no patch"
    label = entry["patch"]
    if entry.get("how") == "default":
        label += ", defaulted" + (" while the tracker lagged" if entry.get("lagged") else "")
    return label


def sources_view(mod_root, vanilla_repo=None, game_root=None, refresh=True, full=False):
    """What the sources panel and `--sources` show: {mod_id, foundations, adopted,
    missing, suggestions, orphans}. Each source is {id, key, kind, detected, path,
    rename, versions: [{tag, patch, how, lagged}], unpatched, freshness}."""
    sources = load_sources(mod_root)
    scan = refresh_scan(mod_root, game_root) if refresh else cached_scan(mod_root, game_root)

    def describe(s):
        info = key_info(s.key)["patches"] if s.key else {}
        versions = [{"tag": t, "patch": (info.get(t) or {}).get("patch"), "how": (info.get(t) or {}).get("how"),
                     "lagged": bool((info.get(t) or {}).get("lagged"))}
                    for _c, t in (s.raw_versions() if Path(s.path).is_dir() else [])]
        return {"id": s.id, "key": s.key, "kind": s.kind, "stored_kind": s.stored_kind, "path": s.path,
                "rename": list(s.rename), "versions": versions,
                "unpatched": sum(1 for v in versions if not v["patch"]),
                "freshness": freshness(s, full) if Path(s.path).is_dir() else []}

    reg = registry()
    return {"mod_id": sources.mod_id, "dependencies": sources.dependencies,
            "foundations": [describe(s) for s in sources.foundations],
            "adopted": [describe(s) for s in sources.adopted],
            "missing": [{"id": s.id, "path": s.path, "role": s.role} for s in sources.missing],
            "suggestions": suggestions(mod_root, scan, sources) if scan else [],
            "scan_current": scan is not None,
            "orphans": [{"key": k, "id": reg[k]["id"], "path": reg[k]["path"]} for k in orphaned_sources()]}


def render_sources(view):
    """The --sources listing as Markdown."""
    lines = [f"# Sources for {view['mod_id']}", ""]

    def source_lines(n, s, foundation):
        out = [f"{n}. **{s['id']}** ({s['kind']}{', set by hand' if s['stored_kind'] else ''}) `{s['path']}`"]
        if s["rename"]:
            out.append("   - renames: " + ", ".join(f"`{r['from']}` → `{r['to']}`" for r in s["rename"]))
        if s["versions"]:
            shown = ", ".join(f"{v['tag']} ({_patch_label(v)})" if foundation else v["tag"] for v in s["versions"])
            out.append(f"   - versions, oldest first: {shown}")
        else:
            out.append("   - no versions yet")
        if foundation and s["unpatched"]:
            out.append(f"   - {s['unpatched']} versions have no patch and are left out of runs; assign one with "
                       f"`pdx-audit --patch {s['id']} <version>[..<version>] <patch>`")
        out += [f"   - ⚠ {m}" for m in s["freshness"]]
        return out

    lines.append("## Foundations, in load order")
    lines.append("")
    if view["foundations"]:
        for n, s in enumerate(view["foundations"], 1):
            lines += source_lines(n, s, True)
    else:
        lines.append("None chosen.")
    lines += ["", "## Adopted sources", ""]
    if view["adopted"]:
        for n, s in enumerate(view["adopted"], 1):
            lines += source_lines(n, s, False)
    else:
        lines.append("None chosen.")
    if view["missing"]:
        lines += ["", "## Sources whose folder is gone", ""]
        for s in view["missing"]:
            lines.append(f"- ⚠ **{s['id']}** ({s['role']}) was at `{s['path']}`; relocate it with "
                         f"`pdx-audit --relocate-source {s['id']} <folder>` or remove it with "
                         f"`pdx-audit --remove-source {s['id']}`")
    lines += ["", "## Suggestions", ""]
    if view["suggestions"]:
        for s in view["suggestions"]:
            why = ("declared dependency" if s["reason"] == "dependency"
                   else "local git repository that can stand in for a declared dependency")
            lines.append(f"- **{s['id']}** `{s['path']}`: {why}")
        lines += ["", "Add one with `pdx-audit --add-source <folder> --as foundation`, or stop suggesting it "
                      "with `pdx-audit --ignore-suggestion <id>`."]
    else:
        lines.append("None.")
    if view["orphans"]:
        lines += ["", "## Orphaned sources", ""]
        lines += [f"- {o['id']} ({o['key']}) `{o['path']}`" for o in view["orphans"]]
        lines += ["", "Remove them with `pdx-audit --remove-orphaned-sources`."]
    return "\n".join(lines) + "\n"
