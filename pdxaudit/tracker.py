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
from pdx_utilities.paths import (find_mod_root_or_exit, vanilla_root,
                                  DEFAULT_VANILLA_ROOT)
from pdx_utilities.constants import SCAN_TOPDIRS as MODULE_ROOTS  # noqa: F401

from . import session
from .config import cfg
from .safety import remove_file, RefusedRemoval

# Every cache file name pdx-audit writes under <vanilla-tracker>/cache/.
CACHE_FILE_RE = r"(?:blocks|gui|vocab|dupes)-v\d+-[0-9a-f]{40}(?:-[0-9a-f]{12})?\.json"

# The one temporary file --snapshot creates, inside the tracker repo itself.
SNAPSHOT_INDEX_NAME = "pdx-audit-snapshot.index"
SNAPSHOT_INDEX_RE = r"pdx-audit-snapshot\.index"

def find_mod_root(override: str | None = None) -> Path:
    return find_mod_root_or_exit(override=override)

def find_vanilla_repo(mod_root: Path, override: str | None = None) -> Path:
    if override:
        p = Path(override).resolve()
        if p.exists():
            return p
        print(f"Vanilla repo not found at {p}", file=sys.stderr)
        sys.exit(1)

    for src in (os.environ.get("PDX_VANILLA_REPO"), cfg("vanilla_repo")):
        if src:
            p = Path(src).resolve()
            if p.exists():
                return p

    candidate = mod_root.parent / "vanilla-tracker" / "repo.git"
    if candidate.exists():
        return candidate

    print("Error: vanilla-tracker repo not found. Searched:\n"
          "  $PDX_VANILLA_REPO\n"
          "  config file (vanilla_repo)\n"
          f"  {candidate}\n"
          "Use --vanilla-repo <path> to specify.", file=sys.stderr)
    sys.exit(1)

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

def prune_cache(vanilla_repo):
    """Delete cache files for commits no longer in the tracker."""
    cache_dir = Path(vanilla_repo).parent / "cache"
    if not cache_dir.is_dir():
        return
    live = set(git(vanilla_repo, "rev-list", "--all").split())
    if not live:
        return
    for fp in cache_dir.glob("*.json"):
        m = _CACHE_HASH_RE.search(fp.name)
        if m and m.group(1) not in live:
            try:
                remove_file(fp, cache_dir, CACHE_FILE_RE)
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

STALE_SENTINEL_DIRS = ("main_menu/localization/english", "in_game/gui",
                       "in_game/common", "main_menu/common",
                       "loading_screen/common/defines")

def warn_if_tracker_stale(vanilla_repo, newest_hash, sample_size=40):
    """Warn if game files differ from the newest tracked commit. Returns the
    warning, or None."""
    game_root = Path(os.environ.get("PDX_GAME_ROOT")
                     or cfg("game_root") or str(DEFAULT_GAME_ROOT))
    if not game_root.is_dir():
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
                data = (game_root / path).read_bytes()
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
        return Path(vanilla_repo_arg).resolve()
    for src in (os.environ.get("PDX_VANILLA_REPO"), cfg("vanilla_repo")):
        if src:
            return Path(src).resolve()
    mod_root = find_mod_root(mod_root_arg)
    return mod_root.parent / "vanilla-tracker" / "repo.git"

def _version_key(tag: str):
    nums = tuple(int(n) for n in re.findall(r"\d+", tag))
    suffix = re.sub(r"[\d.]+", "", tag)
    return (nums, 0 if suffix else 1, suffix)

def do_snapshot(repo: Path, tag: str, patch_name: str,
                game_root_arg: str | None = None) -> None:
    """Snapshot a vanilla install's .txt/.yml/.gui files into the tracker."""
    game_root = Path(game_root_arg or os.environ.get("PDX_GAME_ROOT")
                     or cfg("game_root") or str(DEFAULT_GAME_ROOT))
    if not game_root.is_dir():
        print(f"Error: game directory not found: {game_root}\n"
              "Set $PDX_GAME_ROOT or pass --game-root pointing at the "
              "game's 'game' directory.",
              file=sys.stderr)
        sys.exit(1)

    if not repo.exists():
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

    print(f"Snapshotting {game_root} as {tag}...")
    files = sorted({f for ext in ("*.txt", "*.yml", "*.gui")
                    for f in game_root.rglob(ext) if f.is_file()})
    n_files = len(files)

    # Files are hashed straight from the game folder into the tracker; git never
    # writes to the game folder. The index is the only temporary item.
    index = repo / SNAPSHOT_INDEX_NAME
    env = dict(os.environ, GIT_DIR=str(repo), GIT_INDEX_FILE=str(index))

    def g(*args, stdin=None, check=True):
        return subprocess.run(["git", *args], env=env, check=check, input=stdin,
                              capture_output=True, text=True)

    if index.exists():
        remove_file(index, repo, SNAPSHOT_INDEX_RE)   # left behind by a crashed run
    try:
        hashes = g("hash-object", "-w", "--no-filters", "--stdin-paths",
                   stdin="".join(f"{f}\n" for f in files)).stdout.split()
        if len(hashes) != n_files:
            print("Error: git did not hash every game file; nothing committed.",
                  file=sys.stderr)
            sys.exit(1)
        entries = "".join(f"100644 {h}\t{f.relative_to(game_root).as_posix()}\0"
                          for f, h in zip(files, hashes))
        g("update-index", "--add", "-z", "--index-info", stdin=entries)
        tree = g("write-tree").stdout.strip()

        has_head = g("rev-parse", "--verify", "--quiet", "HEAD", check=False).returncode == 0
        if has_head and g("rev-parse", "HEAD^{tree}").stdout.strip() == tree:
            print("No changes from the previous snapshot; nothing committed.")
            return

        msg = f"{tag} {patch_name}".strip()
        commit_args = ["commit-tree", tree]
        if has_head:
            commit_args += ["-p", "HEAD"]
        commit_args += ["-m", msg]
        commit = g(*commit_args).stdout.strip()
        g("update-ref", "refs/heads/master", commit)
        g("tag", tag)
    finally:
        if index.exists():
            remove_file(index, repo, SNAPSHOT_INDEX_RE)

    print(f"Done: {msg} ({n_files} files).")
    tags = subprocess.run(["git", "--git-dir", str(repo), "tag", "-l",
                           "--sort=-v:refname"], capture_output=True, text=True)
    recent = " ".join(tags.stdout.split()[:5])
    print(f"Tracked versions (newest first): {recent}")
