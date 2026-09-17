"""Vanilla-tracker access: git, snapshots, archives, path resolution."""

import subprocess
import re
import sys
import difflib
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pdx_utilities.git import git, git_archive
from pdx_utilities.paths import (canonical_path, find_mod_root_or_exit, vanilla_root,
                                  DEFAULT_VANILLA_ROOT)
from pdx_utilities.constants import SCAN_TOPDIRS as MODULE_ROOTS  # noqa: F401

from . import session
from .config import cfg, config_file, setting, SETTINGS
from .safety import remove_file, RefusedRemoval

# Every cache file name pdx-audit writes under a source's cache folder. A source
# other than vanilla prefixes its storage key.
CACHE_FILE_RE = r"(?:blocks|gui|vocab|dupes)-v\d+-[0-9a-f]{40}(?:-[0-9a-f]{12})?\.json"

# The one temporary file --snapshot creates, inside the tracker repo itself.
SNAPSHOT_INDEX_NAME = "pdx-audit-snapshot.index"
SNAPSHOT_INDEX_RE = r"pdx-audit-snapshot\.index"

def find_mod_root(override: str | None = None) -> Path:
    return find_mod_root_or_exit(override=override)

def locate_vanilla_repo(mod_root: Path, override: str | None = None):
    """(the tracker repo, None), or (None, why it was not found). The tracker can sit
    anywhere under any name: `--vanilla-repo`, then $PDX_VANILLA_REPO, then the config
    file's `vanilla_repo`, then `<mod-parent>/vanilla-tracker/repo.git`."""
    if override:
        p = Path(canonical_path(override))
        if p.exists():
            return p, None
        return None, f"Vanilla repo not found at {p}"

    for name, src in (("$PDX_VANILLA_REPO", os.environ.get("PDX_VANILLA_REPO")),
                      (f"The config file's vanilla_repo ({config_file()})", cfg("vanilla_repo"))):
        if src:
            p = Path(canonical_path(src))
            if p.exists():
                return p, None
            return None, (f"Error: {name} points at {p}, which does not exist. Correct it with "
                          f"`pdx-audit --set vanilla_repo <path>`, or pass --vanilla-repo <path>.")

    candidate = mod_root.parent / "vanilla-tracker" / "repo.git" if mod_root else None
    if candidate and candidate.exists():
        return candidate, None

    return None, ("Error: vanilla-tracker repo not found. Searched:\n"
                  "  $PDX_VANILLA_REPO\n"
                  f"  the config file (vanilla_repo){f' at {config_file()}' if config_file() else ''}\n"
                  f"  {candidate}\n"
                  "Point pdx-audit at a tracker under any name with "
                  "`pdx-audit --set vanilla_repo <path>` (or --vanilla-repo <path> for one run), "
                  "or create one with `pdx-audit --snapshot <version>`.")


def find_vanilla_repo(mod_root: Path, override: str | None = None) -> Path:
    """The tracker repo, or an error and exit. `--display` uses locate_vanilla_repo
    instead, so the app can open on its Settings page and be pointed at one."""
    repo, err = locate_vanilla_repo(mod_root, override)
    if repo is None:
        print(err, file=sys.stderr)
        sys.exit(1)
    return repo

class Message(str):
    """A version's message carrying its tag, for a tag that is not the message's
    first word (a foundation point's `<source id> <version>`)."""

    def __new__(cls, text, tag=None):
        obj = super().__new__(cls, text)
        obj.tag = tag
        return obj

def tag_of(msg):
    """A version's tag: the one its Message carries, or its message's first word."""
    tag = getattr(msg, "tag", None)
    if tag:
        return tag
    parts = (msg or "").split()
    return parts[0] if parts else ""

def get_commits(vanilla_repo):
    log = git(vanilla_repo, "log", "--oneline", "--no-decorate")
    result = []
    for line in log.strip().split("\n"):
        if not line:
            continue
        parts = line.split(None, 1)
        result.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return result

def full_hash(vanilla_repo, commit):
    """The full hash of a tracker commit, or '' when it does not resolve."""
    return session.memo(("rev-parse", str(vanilla_repo), commit),
                        lambda: git(vanilla_repo, "rev-parse", commit).strip())

def tree_files(vanilla_repo, commit):
    """[(path, blob id)] for the regular files at `commit`, in tree order, which
    is the order `git archive` writes them in."""
    def list_tree():
        files = []
        for rec in git(vanilla_repo, "ls-tree", "-r", "-z", commit, timeout=60).split("\0"):
            meta, _tab, path = rec.partition("\t")
            parts = meta.split()
            if len(parts) == 3 and parts[1] == "blob" and parts[0] in ("100644", "100755"):
                files.append((path, parts[2]))
        return files
    return session.memo(("ls-tree", str(vanilla_repo), commit), list_tree)

def read_blobs(vanilla_repo, blob_ids, timeout=180, workers=8):
    """{blob id: bytes} for tracker blobs. Git inflates one blob at a time per
    process, so a large batch is split across several `git cat-file` processes
    read in parallel. Ids git cannot find are left out."""
    ids = list(dict.fromkeys(blob_ids))
    n = max(1, min(workers, os.cpu_count() or 1, len(ids) // 64))
    blobs = {}
    with ThreadPoolExecutor(n) as pool:
        for part in pool.map(lambda chunk: _cat_file(vanilla_repo, chunk, timeout),
                             [ids[i::n] for i in range(n)]):
            blobs.update(part)
    return blobs

def _cat_file(vanilla_repo, ids, timeout):
    if not ids:
        return {}
    try:
        r = subprocess.run(["git", f"--git-dir={vanilla_repo}", "cat-file", "--batch"],
                           input="".join(f"{b}\n" for b in ids).encode(),
                           capture_output=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}
    out, pos, blobs = r.stdout, 0, {}
    for _ in ids:
        eol = out.find(b"\n", pos)
        if eol == -1:
            break
        header = out[pos:eol].split()   # "<id> <type> <size>" or "<id> missing"
        pos = eol + 1
        if len(header) != 3:
            continue
        size = int(header[2])
        blobs[header[0].decode()] = out[pos:pos + size]
        pos += size + 1
    return blobs

_CACHE_HASH_RE = re.compile(r"-([0-9a-f]{40})[-.]")

def cache_location(source):
    """(git directory, cache folder, file name prefix) for a source's parsed-index
    cache. A path is the vanilla tracker, whose cache sits beside it."""
    if hasattr(source, "cache_dir"):
        return source.git_dir, Path(source.cache_dir), source.cache_prefix
    return source, Path(source).parent / "cache", ""

def cache_path(source, name):
    """The cache file `name` (a CACHE_FILE_RE name) of a source."""
    _git_dir, folder, prefix = cache_location(source)
    return folder / f"{prefix}{name}"

def prune_cache(source):
    """Delete a source's cache files for commits no longer in its repository."""
    git_dir, cache_dir, prefix = cache_location(source)
    if not cache_dir.is_dir() or not git_dir:
        return
    live = set(git(git_dir, "rev-list", "--all").split())
    if not live:
        return
    pattern = re.escape(prefix) + CACHE_FILE_RE
    for fp in cache_dir.glob(f"{prefix}*.json"):
        if not re.fullmatch(pattern, fp.name):
            continue
        m = _CACHE_HASH_RE.search(fp.name[len(prefix):])
        if m and m.group(1) not in live:
            try:
                remove_file(fp, cache_dir, pattern)
            except (RefusedRemoval, OSError):
                pass

def resolve_ref(vanilla_repo, ref, commits, side):
    """Validate a user-supplied --old/--new value against the tracker."""
    resolved = git(vanilla_repo, "rev-parse", "--verify", "--quiet",
                   f"{ref}^{{commit}}").strip()
    if resolved:
        for h, msg in commits:
            if resolved.startswith(h):
                return msg
        return ""
    versions = [msg.split()[0] for _, msg in commits if msg]
    hits = [v for v in versions if v.startswith(ref)]
    if not hits:
        hits = difflib.get_close_matches(ref, versions, n=3, cutoff=0.4)
    hint = (f"  did you mean: {', '.join(hits)}?" if hits
            else "  run pdx-audit --list-commits to see tracked versions")
    print(f"Error: --{side} '{ref}' does not match any tracked commit or tag.\n"
          f"{hint}", file=sys.stderr)
    sys.exit(1)

DEFAULT_GAME_ROOT = DEFAULT_VANILLA_ROOT

def game_root(override: str | None = None) -> Path:
    """The game install to read, at the usual precedence: `--game-root`, then
    $PDX_GAME_ROOT, then the config file's `game_root`, then the Steam default."""
    value, _origin = setting("game_root", override)
    return Path(canonical_path(value or str(DEFAULT_GAME_ROOT)))

def patch_name(override: str | None = None) -> str:
    """The patch name a snapshot records: `--patch-name`, then $PDX_PATCH_NAME, then
    the config file's `patch_name`, then the built-in default."""
    value, _origin = setting("patch_name", override)
    return value or SETTINGS["patch_name"]["default"]

STALE_SENTINEL_DIRS = ("main_menu/localization/english", "in_game/gui",
                       "in_game/common", "main_menu/common",
                       "loading_screen/common/defines")

def warn_if_tracker_stale(vanilla_repo, newest_hash, sample_size=40):
    """Warn if game files differ from the newest tracked commit. Returns the
    warning, or None."""
    root = game_root()
    if not root.is_dir():
        return
    checked = stale = 0
    for sd in STALE_SENTINEL_DIRS:
        out = git(vanilla_repo, "ls-tree", "-r", newest_hash, "--", sd)
        entries = []
        for line in out.strip().split("\n"):
            if "\t" not in line:
                continue
            meta, path = line.split("\t", 1)
            entries.append((path, meta.split()[2]))
        step = max(1, len(entries) // sample_size)
        for path, sha in entries[::step][:sample_size]:
            try:
                data = (root / path).read_bytes()
            except OSError:
                continue
            checked += 1
            h = hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()
            if h != sha:
                stale += 1
    if not stale:
        return None
    msg = (f"Warning: vanilla-tracker looks OUT OF DATE: {stale}/{checked} "
           f"sampled game files differ from the newest tracked commit. "
           f"Record the new game version with `pdx-audit --snapshot <version>`, "
           f"then re-audit.")
    print(msg, file=sys.stderr)
    return msg

def _git_archive(vanilla_repo, commit, paths=None, timeout=60):
    """Wrapper around shared git_archive with ignore_zeros note."""
    return git_archive(vanilla_repo, commit, paths, timeout)

def resolve_tracker_path(mod_root_arg, vanilla_repo_arg) -> Path:
    """Where the tracker repo lives (or should live)."""
    if vanilla_repo_arg:
        return Path(canonical_path(vanilla_repo_arg))
    for src in (os.environ.get("PDX_VANILLA_REPO"), cfg("vanilla_repo")):
        if src:
            return Path(canonical_path(src))
    mod_root = find_mod_root(mod_root_arg)
    return mod_root.parent / "vanilla-tracker" / "repo.git"

class SnapshotError(Exception):
    """git did not store every file of a snapshot."""

def snapshot_tree(repo, root, files, index_name=SNAPSHOT_INDEX_NAME, index_re=SNAPSHOT_INDEX_RE):
    """The tree id of `files` (paths under `root`) stored in `repo`. Files are hashed
    straight from their folder into git with their bytes exact; git never writes to
    that folder. The index is the only temporary item, and it is removed after."""
    repo, root = Path(repo), Path(root)
    index = repo / index_name
    env = dict(os.environ, GIT_DIR=str(repo), GIT_INDEX_FILE=str(index))

    def g(*args, stdin=None):
        return subprocess.run(["git", *args], env=env, check=True, input=stdin,
                              capture_output=True, text=True)

    if index.exists():
        remove_file(index, repo, index_re)   # left behind by a crashed run
    try:
        if files:
            hashes = g("hash-object", "-w", "--no-filters", "--stdin-paths",
                       stdin="".join(f"{f}\n" for f in files)).stdout.split()
            if len(hashes) != len(files):
                raise SnapshotError(f"git hashed {len(hashes)} of {len(files)} files")
            entries = "".join(f"100644 {h}\t{Path(f).relative_to(root).as_posix()}\0"
                              for f, h in zip(files, hashes))
            g("update-index", "--add", "-z", "--index-info", stdin=entries)
        return g("write-tree").stdout.strip()
    finally:
        if index.exists():
            remove_file(index, repo, index_re)

def commit_tree(repo, tree, parent, message):
    """A new commit of `tree` on `parent` (or none) in `repo`; no ref moves."""
    args = ["git", "-c", "user.name=pdx-audit", "-c", "user.email=pdx-audit@localhost",
            "--git-dir", str(repo), "commit-tree", tree]
    if parent:
        args += ["-p", parent]
    args += ["-m", message]
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()

def _version_key(tag: str):
    nums = tuple(int(n) for n in re.findall(r"\d+", tag))
    suffix = re.sub(r"[\d.]+", "", tag)
    return (nums, 0 if suffix else 1, suffix)

def do_snapshot(repo: Path, tag: str, patch: str,
                game_root_arg: str | None = None) -> None:
    """Snapshot a vanilla install's .txt/.yml/.gui files into the tracker."""
    root = game_root(game_root_arg)
    if not root.is_dir():
        print(f"Error: game directory not found: {root}\n"
              "Point pdx-audit at the game's 'game' directory with "
              "`pdx-audit --set game_root <path>`, or pass --game-root for one run.",
              file=sys.stderr)
        sys.exit(1)

    if not (repo / "HEAD").is_file():
        if repo.exists() and any(repo.iterdir()):
            print(f"Error: {repo} is not a tracker repository and is not empty; nothing was "
                  f"written. Point pdx-audit at a tracker, or at a new path, with "
                  f"`pdx-audit --set vanilla_repo <path>`.", file=sys.stderr)
            sys.exit(1)
        repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--bare", "--quiet", str(repo)], check=True)
        subprocess.run(["git", "--git-dir", str(repo), "symbolic-ref",
                        "HEAD", "refs/heads/master"], check=True)
        print(f"Created tracker repo: {repo}")

    tag_exists = subprocess.run(
        ["git", "--git-dir", str(repo), "rev-parse", "--verify", "--quiet",
         f"refs/tags/{tag}"], capture_output=True).returncode == 0
    if tag_exists:
        print(f"Error: tag '{tag}' already exists in the tracker.", file=sys.stderr)
        sys.exit(1)

    existing = subprocess.run(
        ["git", "--git-dir", str(repo), "tag", "-l"],
        capture_output=True, text=True).stdout.split()
    if existing:
        newest = max(existing, key=_version_key)
        if _version_key(tag) < _version_key(newest):
            print(f"Error: '{tag}' is older than the newest tracked version "
                  f"('{newest}'), and snapshots must be recorded oldest "
                  "first. To back-populate history, snapshot the old versions "
                  "in order into a new tracker (pass --vanilla-repo with a new "
                  "path), then snapshot the current version last.",
                  file=sys.stderr)
            sys.exit(1)

    print(f"Snapshotting {root} as {tag}...")
    files = sorted({f for ext in ("*.txt", "*.yml", "*.gui")
                    for f in root.rglob(ext) if f.is_file()})
    n_files = len(files)
    try:
        tree = snapshot_tree(repo, root, files)
    except SnapshotError:
        print("Error: git did not hash every game file; nothing committed.",
              file=sys.stderr)
        sys.exit(1)

    head = git(repo, "rev-parse", "--verify", "--quiet", "HEAD").strip()
    if head and git(repo, "rev-parse", "HEAD^{tree}").strip() == tree:
        print("No changes from the previous snapshot; nothing committed.")
        return
    msg = f"{tag} {patch}".strip()
    commit = commit_tree(repo, tree, head or None, msg)
    subprocess.run(["git", "--git-dir", str(repo), "update-ref", "refs/heads/master", commit],
                   check=True, capture_output=True)
    subprocess.run(["git", "--git-dir", str(repo), "tag", tag], check=True, capture_output=True)

    print(f"Done: {msg} ({n_files} files).")
    tags = subprocess.run(["git", "--git-dir", str(repo), "tag", "-l",
                           "--sort=-v:refname"], capture_output=True, text=True)
    recent = " ".join(tags.stdout.split()[:5])
    print(f"Tracked versions (newest first): {recent}")
