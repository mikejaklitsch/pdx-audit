"""Optional JSON config for stable per-machine settings.

Precedence for any setting: CLI flag > environment variable > config file >
built-in default. The config file is the first of these that exists:

    $PDX_AUDIT_CONFIG            (explicit path, if set)
    ~/.config/pdx-audit.json
    <repo>/config.json           (next to the tool)

Recognized keys:
    game_root      path to the game's install "game" directory
    vanilla_repo   path to the vanilla-tracker bare git repo
    skip_dirs      directories excluded from every scan
    skip_files     filename globs excluded from every scan

Unknown keys are ignored. See config.sample.json for an example.
"""
import json
import os
import fnmatch
import sys
from pathlib import Path

_CACHE = None


def _candidate_paths():
    paths = []
    env = os.environ.get("PDX_AUDIT_CONFIG")
    if env:
        paths.append(Path(env))
    paths.append(Path.home() / ".config" / "pdx-audit.json")
    paths.append(Path(__file__).resolve().parent.parent / "config.json")
    return paths


def load_config():
    """The parsed config dict of the first file found, or {} if there is none. A file
    that cannot be read as a JSON object is reported on stderr and its settings are
    ignored; later candidates are not read. Cached for the process."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    _CACHE = {}
    for p in _candidate_paths():
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"Warning: could not read the config file {p}: {e}. Its settings are ignored.", file=sys.stderr)
            break
        if isinstance(data, dict):
            _CACHE = data
        else:
            print(f"Warning: the config file {p} is not a JSON object. Its settings are ignored.", file=sys.stderr)
        break
    return _CACHE


def cfg(key, default=None):
    """Config value for `key`, or `default` if unset."""
    val = load_config().get(key)
    return val if val not in (None, "") else default


def should_skip(rel):
    """True if a mod-relative path is excluded by the config skip lists.

    "skip_dirs" entries match a whole directory: an entry matches if it is a
    path component of `rel` or a prefix of it (so "backup" skips any backup/
    dir, and "in_game/gui/wip" skips just that subtree). "skip_files" entries
    are filename globs matched against both the basename and the full path
    (so "*.bak" or "in_game/**/tmp_*.txt")."""
    rel = str(rel).replace("\\", "/")
    parts = rel.split("/")
    for d in cfg("skip_dirs", []) or []:
        d = str(d).strip("/").replace("\\", "/")
        if d and (d in parts or rel == d or rel.startswith(d + "/")):
            return True
    name = parts[-1]
    for pat in cfg("skip_files", []) or []:
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat):
            return True
    return False
