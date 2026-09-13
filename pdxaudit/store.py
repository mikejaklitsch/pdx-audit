"""Per-user storage for findings records. Nothing here ever writes into a mod.

Layout under the per-user data folder:
    <data>/<mod id>/commits/<commit>.json   record as of that commit (git mods)
    <data>/<mod id>/record.json              record for a mod that is not in git
    <data>/<mod id>/results.json             the last run the --display app made

A record is read from the nearest first-parent ancestor of HEAD that has one,
and written back under HEAD only when something changed. Records whose commit is
on no branch of the repository are reported, never removed automatically."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .ledger import empty_state, short_id
from .safety import remove_file, RefusedRemoval

APP_NAME = "pdx-audit"
MOD_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
COMMIT_FILE_RE = r"[0-9a-f]{40}\.json"


def data_root():
    """The per-user data folder for pdx-audit on this operating system."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def read_mod_id(mod_root):
    """(mod id, None) from .metadata/metadata.json, or (None, reason)."""
    meta = Path(mod_root) / ".metadata" / "metadata.json"
    try:
        data = json.loads(meta.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        return None, f"could not read {meta}: {e}"
    mod_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(mod_id, str) or not mod_id:
        return None, f"{meta} has no id"
    if not MOD_ID_RE.fullmatch(mod_id) or ".." in mod_id:
        return None, (f"mod id {mod_id!r} is not safe to use as a folder name "
                      f"(letters, digits, '_', '-' and '.' only)")
    return mod_id, None


def _git(cwd, *args):
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _canonical(state):
    return json.dumps(state, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


class Store:
    def __init__(self, mod_root, mod_id):
        self.mod_root = Path(mod_root)
        self.mod_id = mod_id
        self.dir = data_root() / mod_id
        self.commits_dir = self.dir / "commits"
        self.is_git = bool(_git(self.mod_root, "rev-parse", "--show-toplevel"))
        head = _git(self.mod_root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}") \
            if self.is_git else None
        self.head = head.strip() if head and COMMIT_RE.fullmatch(head.strip()) else None
        self.readonly = False
        self.state = self._load()
        self._saved = _canonical(self.state)

    # --- reading ------------------------------------------------------------

    def _record_hashes(self):
        try:
            names = os.listdir(self.commits_dir)
        except OSError:
            return set()
        return {n[:-5] for n in names if re.fullmatch(COMMIT_FILE_RE, n)}

    def _read(self, path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"Warning: could not read pdx-audit record {path}: {e}. "
                  f"Records will not be saved this run.", file=sys.stderr)
            self.readonly = True
            return empty_state()
        state = empty_state()
        if isinstance(data, dict):
            for k in state:
                if isinstance(data.get(k), dict):
                    state[k] = data[k]
        return state

    def _load(self):
        if self.head:
            have = self._record_hashes()
            if have:
                for h in (_git(self.mod_root, "rev-list", "--first-parent", "HEAD") or "").split():
                    if h in have:
                        return self._read(self.commits_dir / f"{h}.json")
            return empty_state()
        path = self.dir / "record.json"
        return self._read(path) if path.is_file() else empty_state()

    # --- writing ------------------------------------------------------------

    @property
    def record_path(self):
        if self.head:
            return self.commits_dir / f"{self.head}.json"
        return self.dir / "record.json"

    @property
    def results_path(self):
        return self.dir / "results.json"

    def changed(self):
        return _canonical(self.state) != self._saved

    def save(self):
        """Write the record under HEAD if the state changed. Returns True when a
        file was written."""
        if not self.changed():
            return False
        if self.readonly:
            print("Warning: pdx-audit record not saved (the existing record could "
                  "not be read).", file=sys.stderr)
            return False
        target = self.record_path
        target.parent.mkdir(parents=True, exist_ok=True)
        text = _canonical(self.state)
        tmp = target.parent / f".{target.name}.tmp-{os.getpid()}"
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                try:
                    remove_file(tmp, target.parent,
                                r"\.(?:[0-9a-f]{40}|record)\.json\.tmp-\d+")
                except (RefusedRemoval, OSError):
                    pass
        self._saved = text
        return True

    # --- orphaned records ---------------------------------------------------

    def reachable(self):
        out = _git(self.mod_root, "rev-list", "--branches", "--remotes", "--tags", "HEAD")
        return set((out or "").split())

    def orphans(self):
        if not self.is_git:
            return []
        have = self._record_hashes()
        if not have:
            return []
        return sorted(have - self.reachable())


def open_store(mod_root):
    """(Store, None) or (None, reason) when the mod id is missing or unsafe."""
    mod_id, err = read_mod_id(mod_root)
    if err:
        return None, err
    return Store(mod_root, mod_id), None


def orphan_note(store):
    orphans = store.orphans()
    if not orphans:
        return None
    shown = ", ".join(short_id(h) for h in orphans)
    return (f"Note: {len(orphans)} pdx-audit record(s) for {store.mod_id} point at "
            f"commits on no branch of this repository:\n  {shown}\n"
            f"Remove them with: pdx-audit --remove-orphaned-records")


def remove_orphaned_records(store, force=False):
    """List orphaned record files, confirm (unless force), then remove each one
    through the validated single-file helper. Returns a process exit code."""
    orphans = store.orphans()
    if not orphans:
        print(f"No orphaned pdx-audit records for {store.mod_id}.")
        return 0
    print(f"Orphaned pdx-audit records for {store.mod_id} "
          f"(commit on no branch of this repository):")
    for h in orphans:
        print(f"  {store.commits_dir / (h + '.json')}")
    if not force:
        if not sys.stdin.isatty():
            print("Refusing to remove records without an interactive confirmation. "
                  "Run this in a terminal, or add --force.", file=sys.stderr)
            return 1
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            print("Aborted. No records were removed.")
            return 0
    reachable = store.reachable()
    removed = 0
    for h in orphans:
        path = store.commits_dir / f"{h}.json"
        if h in reachable:
            print(f"  kept {path}: its commit is now on a branch")
            continue
        try:
            remove_file(path, store.commits_dir, COMMIT_FILE_RE)
        except (RefusedRemoval, OSError) as e:
            print(f"  kept {path}: {e}", file=sys.stderr)
            continue
        print(f"  removed {path}")
        removed += 1
    print(f"Removed {removed} record file(s).")
    return 0
