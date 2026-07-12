"""Vanilla-tracker access: git, snapshots, archives, path resolution."""

import subprocess
import re
import shutil
import sys
import difflib
import tempfile
import hashlib
import os
from pathlib import Path

from .config import cfg

def find_mod_root(override: str | None = None) -> Path:
    if override:
        return Path(override).resolve()
    p = Path.cwd().resolve()
    while True:
        if (p / ".metadata").is_dir():
            return p
        parent = p.parent
        if parent == p:
            break
        p = parent
    print("Error: could not find mod root (.metadata/ directory). "
          "Run from inside a mod directory or use --mod-root.", file=sys.stderr)
    sys.exit(1)

def find_vanilla_repo(mod_root: Path, override: str | None = None) -> Path:
    if override:
        p = Path(override).resolve()
        if p.exists():
            return p
        print(f"Vanilla repo not found at {p}", file=sys.stderr)
        sys.exit(1)

    # precedence: env > config > convention (<mod-parent>/vanilla-tracker)
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

def git(vanilla_repo, *args):
    try:
        r = subprocess.run(
            ["git", f"--git-dir={vanilla_repo}"] + list(args),
            capture_output=True, text=True, timeout=30,
        )
        return r.stdout if r.returncode == 0 else ""
    except subprocess.TimeoutExpired:
        return ""

def get_commits(vanilla_repo):
    # Full history: one commit per game version, and --full trusts commits[-1]
    # to be the oldest tracked snapshot.
    log = git(vanilla_repo, "log", "--oneline", "--no-decorate")
    result = []
    for line in log.strip().split("\n"):
        if not line:
            continue
        parts = line.split(None, 1)
        result.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return result

def resolve_ref(vanilla_repo, ref, commits, side):
    """Validate a user-supplied --old/--new value (version tag or commit hash)
    against the tracker. Returns the version label ('1.3.10 Pavia') for display,
    or "" if the ref resolves but isn't a tracked commit. Aborts loudly with a
    'did you mean' hint when the ref matches nothing; otherwise an unresolvable
    version silently extracts an empty tree and every block looks 'changed'."""
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

DEFAULT_GAME_ROOT = Path(
    "/mnt/d/Program Files (x86)/Steam/steamapps/common/Europa Universalis V/game")

STALE_SENTINEL_DIRS = ("main_menu/localization/english", "in_game/gui")

def warn_if_tracker_stale(vanilla_repo, newest_hash, sample_size=40):
    """Hash a spread sample of live game files against the newest tracked
    commit's blobs. Mismatches mean the game patched after the last tracker
    snapshot; every audit window is then missing the real newest version.
    English localization churns every patch, so the sample is reliable.
    Warn-only heuristic; silent when the game install is not found."""
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
    if stale:
        print(f"Warning: vanilla-tracker looks OUT OF DATE: {stale}/{checked} "
              f"sampled game files differ from the newest tracked commit. "
              f"Run the tracker's update script, then re-audit.",
              file=sys.stderr)

MODULE_ROOTS = ("in_game", "main_menu", "loading_screen")

def _git_archive(vanilla_repo, commit, paths=None, timeout=60):
    """Tar bytes for `paths` (whole tree if None) at `commit`. When the batched
    call fails (e.g. one path does not exist at that commit), falls back to one
    archive per path and concatenates them; the result must be parsed with
    ignore_zeros=True or every archive after the first is silently lost."""
    cmd = ["git", f"--git-dir={vanilla_repo}", "archive", "--format=tar", commit]
    if paths:
        cmd += ["--"] + list(paths)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return b""
    if r.returncode == 0:
        return r.stdout
    if not paths:
        return b""
    out = b""
    for p in paths:
        cmd2 = ["git", f"--git-dir={vanilla_repo}",
                "archive", "--format=tar", commit, "--", p]
        try:
            r2 = subprocess.run(cmd2, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            continue
        if r2.returncode == 0:
            out += r2.stdout
    return out

def resolve_tracker_path(mod_root_arg, vanilla_repo_arg) -> Path:
    """Where the tracker repo lives (or should live). Unlike
    find_vanilla_repo this does not require it to exist yet."""
    if vanilla_repo_arg:
        return Path(vanilla_repo_arg).resolve()
    for src in (os.environ.get("PDX_VANILLA_REPO"), cfg("vanilla_repo")):
        if src:
            return Path(src).resolve()
    mod_root = find_mod_root(mod_root_arg)
    return mod_root.parent / "vanilla-tracker" / "repo.git"

def _version_key(tag: str):
    """Sort key for version-ish tags: numeric fields compare numerically,
    and a suffix like '-beta' sorts below the plain release."""
    nums = tuple(int(n) for n in re.findall(r"\d+", tag))
    suffix = re.sub(r"[\d.]+", "", tag)
    return (nums, 0 if suffix else 1, suffix)

def do_snapshot(repo: Path, tag: str, patch_name: str,
                game_root_arg: str | None = None) -> None:
    """Snapshot a vanilla install's .txt/.yml/.gui files into the tracker
    as one commit tagged with the game version. Creates the bare repo on
    first use, so a new machine needs no manual git setup."""
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

    # Commits must be chronological (oldest patch first): the audits diff
    # the newest two commits, so appending an OLDER version after a newer
    # one inverts every audit window. Refuse rather than corrupt.
    existing = subprocess.run(
        ["git", "--git-dir", str(repo), "tag", "-l"],
        capture_output=True, text=True).stdout.split()
    if existing:
        newest = max(existing, key=_version_key)
        if _version_key(tag) < _version_key(newest):
            print(f"Error: '{tag}' is older than the newest tracked version "
                  f"('{newest}'), and snapshots must be recorded oldest "
                  "first. To back-populate history, snapshot old versions "
                  "in order BEFORE the current one (delete the repo.git "
                  "directory and start over if needed; snapshots are cheap, "
                  "derived data).", file=sys.stderr)
            sys.exit(1)

    print(f"Snapshotting {game_root} as {tag}...")
    with tempfile.TemporaryDirectory(prefix="vanilla-tracker-") as tmp:
        work = Path(tmp) / "work"
        work.mkdir()
        n_files = 0
        for ext in ("*.txt", "*.yml", "*.gui"):
            for f in game_root.rglob(ext):
                rel = f.relative_to(game_root)
                dest = work / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, dest)
                n_files += 1

        env = dict(os.environ,
                   GIT_DIR=str(repo),
                   GIT_WORK_TREE=str(work),
                   GIT_INDEX_FILE=str(Path(tmp) / "index"))

        def g(*args, check=True):
            return subprocess.run(["git", *args], env=env, check=check,
                                  capture_output=True, text=True)

        has_head = subprocess.run(
            ["git", "--git-dir", str(repo), "rev-parse", "--verify",
             "--quiet", "HEAD"], capture_output=True).returncode == 0

        if has_head:
            g("read-tree", "HEAD")
        g("add", "-A")
        if has_head and g("diff", "--cached", "--quiet", check=False).returncode == 0:
            print("No changes from the previous snapshot; nothing committed.")
            return

        tree = g("write-tree").stdout.strip()
        msg = f"{tag} {patch_name}".strip()
        commit_args = ["commit-tree", tree]
        if has_head:
            commit_args += ["-p", "HEAD"]
        commit_args += ["-m", msg]
        commit = g(*commit_args).stdout.strip()
        g("update-ref", "refs/heads/master", commit)
        g("tag", tag)

    print(f"Done: {msg} ({n_files} files).")
    tags = subprocess.run(["git", "--git-dir", str(repo), "tag", "-l",
                           "--sort=-v:refname"], capture_output=True, text=True)
    recent = " ".join(tags.stdout.split()[:5])
    print(f"Tracked versions (newest first): {recent}")
