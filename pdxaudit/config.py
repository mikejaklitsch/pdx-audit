"""Optional JSON config for stable per-machine settings.

There is one config file, `<data folder>/config.json`. It sits beside the findings
records, so it outlives a reinstall of the tool. `--set`, `--unset` and the Settings
page of the app all write that one file.

Precedence for any setting: CLI flag > environment variable > config file >
built-in default.

Recognized keys:
    vanilla_repo   path to the vanilla-tracker bare git repo, under any name
    game_root      path to the game's install "game" directory
    patch_name     the patch name a commit records
    skip_dirs      directories excluded from every scan
    skip_files     filename globs excluded from every scan
    merge_types    common/ folders the engine merges across files, beyond the
                   ones worked out from vanilla

Unknown keys are ignored. See config.sample.json for an example.
"""
import json
import os
import fnmatch
import sys
from pathlib import Path

from pdx_utilities.paths import canonical_path, DEFAULT_VANILLA_ROOT

_CACHE = None
_FILE = None

#: Every setting the config file holds. `env` and `flag` are what outrank it,
#: and the settings carrying a `label` are the ones --set and the app's
#: Settings page can write.
SETTINGS = {
    "vanilla_repo": {"label": "Tracker", "kind": "path", "env": "PDX_VANILLA_REPO",
                     "flag": "--vanilla-repo",
                     "fallback": "<mod-parent>/vanilla-tracker/repo.git",
                     "help": "the vanilla-tracker bare git repo, under any name"},
    "game_root": {"label": "Game folder", "kind": "path", "env": "PDX_GAME_ROOT",
                  "flag": "--game-root", "default": str(DEFAULT_VANILLA_ROOT),
                  "help": "the game install's \"game\" directory, committed into the tracker"},
    "patch_name": {"label": "Default patch name", "kind": "text", "env": "PDX_PATCH_NAME",
                   "flag": "--patch-name", "default": "Pavia",
                   "help": "the patch name a commit records"},
    "skip_dirs": {"kind": "list", "help": "directories excluded from every scan"},
    "skip_files": {"kind": "list", "help": "filename globs excluded from every scan"},
    "merge_types": {"kind": "list",
                    "help": "common/ folders the engine merges across files, beyond vanilla's"},
}

#: The keys --set writes and the app's Settings page edits, in the order shown.
SETTABLE = tuple(k for k, s in SETTINGS.items() if "label" in s)


class ConfigError(Exception):
    """A key or value --set cannot store."""


# Where earlier versions also read a config file. Nothing reads these now; they are
# named so that a run says where a file has been left behind, and can be dropped once
# no one has one.
def _former_paths():
    home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return [Path(home) / "pdx-audit.json", Path(__file__).resolve().parent.parent / "config.json"]


def writable_path():
    """The one config file. It sits beside the findings records, so it outlives
    reinstalling the tool, and `--set` and the app write it."""
    from .store import data_root
    return data_root() / "config.json"


def stale_config_note():
    """A note naming a config file from an earlier version that nothing reads now, or
    None. The settings in it do not apply, and saying so beats losing them quietly."""
    left = [p for p in _former_paths() if p.is_file()]
    if not left or writable_path().is_file():
        return None
    return (f"Note: pdx-audit reads one config file, {writable_path()}, which does not exist yet. "
            f"{left[0]} is left over from an earlier version and no longer applies. Move its "
            f"settings with `pdx-audit --set <key> <value>`.")


def load_config():
    """The settings in the config file, or {} when it does not exist. A file that does
    not read as a JSON object is reported on stderr and its settings are ignored.
    Cached for the process."""
    global _CACHE, _FILE
    if _CACHE is not None:
        return _CACHE
    _CACHE, _FILE = {}, None
    p = writable_path()
    if not p.is_file():
        return _CACHE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"Warning: could not read the config file {p}: {e}. Its settings are ignored.", file=sys.stderr)
        return _CACHE
    if isinstance(data, dict):
        _CACHE, _FILE = data, p
    else:
        print(f"Warning: the config file {p} is not a JSON object. Its settings are ignored.", file=sys.stderr)
    return _CACHE


def invalidate():
    """Drop the cached config, so the next read sees a file just written."""
    global _CACHE, _FILE
    _CACHE, _FILE = None, None


def config_file():
    """The config file, or None when it does not exist."""
    load_config()
    return _FILE


def cfg(key, default=None):
    """Config value for `key`, or `default` if unset."""
    val = load_config().get(key)
    return val if val not in (None, "") else default


def setting(key, flag=None):
    """(value, where it comes from) for one setting, at the precedence every run
    uses: a CLI flag, then the environment, then the config file, then the built-in
    default. `where` names the source, for a report that says why a value is in use."""
    spec = SETTINGS[key]
    if flag:
        return flag, spec.get("flag", "a command-line option")
    env_name = spec.get("env")
    env = os.environ.get(env_name) if env_name else None
    if env:
        return env, f"${env_name}"
    val = cfg(key)
    if val not in (None, ""):
        return val, str(config_file())
    return spec.get("default"), "the built-in default"


def _read_writable():
    """The data folder's config file as a dict, or {} when it is absent. Raises
    ConfigError when it exists but cannot be read, so --set never discards it."""
    p = writable_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ConfigError(f"could not read {p}: {e}. Correct or remove the file, then try again.")
    if not isinstance(data, dict):
        raise ConfigError(f"{p} is not a JSON object. Correct or remove the file, then try again.")
    return data


def _write_writable(data):
    p = writable_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".new")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, p)
    except OSError as e:
        raise ConfigError(f"could not write {p}: {e}")
    invalidate()


def _outranking(key):
    """A message naming what still wins over the setting just written, or None."""
    env_name = SETTINGS[key].get("env")
    if env_name and os.environ.get(env_name):
        return (f"Note: ${env_name} is set to {os.environ[env_name]} and outranks the config file, "
                f"so runs keep using it until it is unset.")
    return None


def _check_tracker(path):
    """A tracker path to store, and a note about it. A path that does not exist yet
    is kept, since `--commit` creates the repo there."""
    p = Path(canonical_path(path))
    if p.is_file():
        raise ConfigError(f"{p} is a file, not a folder. The tracker is a bare git repository, "
                          f"such as /path/to/my-tracker.git.")
    if not p.exists():
        return str(p), (f"Note: {p} does not exist yet. `pdx-audit --commit <version>` creates the "
                        f"tracker there and records the installed game as its first version.")
    if not (p / "objects").is_dir() or not (p / "HEAD").is_file():
        if any(p.iterdir()):
            raise ConfigError(f"{p} is not a bare git repository: it has no HEAD and no objects/ "
                              f"directory. Point --set vanilla_repo at a tracker repo, or at a new "
                              f"path for `--commit` to create.")
        return str(p), f"Note: {p} is empty. `pdx-audit --commit <version>` creates the tracker there."
    return str(p), None


def _check_game_root(path):
    p = Path(canonical_path(path))
    if not p.is_dir():
        raise ConfigError(f"{p} is not a folder. Point --set game_root at the game install's "
                          f"\"game\" directory.")
    if not (p / "in_game").is_dir():
        return str(p), (f"Note: {p} has no in_game/ directory, so it may not be the game's \"game\" "
                        f"directory. A commit reads the .txt, .yml and .gui files under it.")
    return str(p), None


def _check_patch_name(value):
    text = " ".join(str(value).split())
    if not text:
        raise ConfigError("the patch name cannot be empty. It is the name a commit records, "
                          "such as Pavia.")
    return text, None


_CHECKS = {"vanilla_repo": _check_tracker, "game_root": _check_game_root,
           "patch_name": _check_patch_name}


def set_value(key, value):
    """Store one setting in the data folder's config file. Returns the messages to
    show: what was stored, and anything that still outranks it. Raises ConfigError
    for a key that is not settable or a value that cannot be right."""
    if key not in SETTABLE:
        raise ConfigError(f"{key} is not a setting --set can store. The settings are: "
                          f"{', '.join(SETTABLE)}.")
    if not str(value).strip():
        raise ConfigError(f"{key} needs a value. `pdx-audit --unset {key}` removes the setting "
                          f"instead.")
    stored, note = _CHECKS[key](value)
    data = _read_writable()
    if data.get(key) == stored:
        messages = [f"{key} is already {stored} in {writable_path()}."]
    else:
        data[key] = stored
        _write_writable(data)
        messages = [f"Set {key} to {stored} in {writable_path()}."]
    for m in (note, _outranking(key)):
        if m:
            messages.append(m)
    return messages


def unset_value(key):
    """Remove one setting from the data folder's config file, so the setting below it
    applies again. Returns the messages to show."""
    if key not in SETTABLE:
        raise ConfigError(f"{key} is not a setting --unset can remove. The settings are: "
                          f"{', '.join(SETTABLE)}.")
    data = _read_writable()
    if key not in data:
        value, origin = setting(key)
        if value not in (None, "") and origin != "the built-in default":
            return [f"{key} is not set in {writable_path()}. Runs read it from {origin}, which "
                    f"pdx-audit does not write; change it there."]
        return [f"{key} is not set in {writable_path()}."]
    del data[key]
    _write_writable(data)
    value, origin = setting(key)
    where = f"{value} (from {origin})" if value else f"nothing (from {origin})"
    return [f"Removed {key} from {writable_path()}. Runs now use {where}."]


def config_view():
    """What --config prints and the Settings page of the app shows: the config file,
    and each setting's value with where it comes from."""
    in_effect = config_file()
    try:
        stored, unreadable = _read_writable(), None
    except ConfigError as e:
        stored, unreadable = {}, str(e)
    settings = []
    for key in SETTINGS:
        value, origin = setting(key)
        # `shown` is for reading; `value` is the real one, so a fallback that names a
        # path rather than being one never lands in an editable box.
        shown = value
        if shown in (None, "") and "fallback" in SETTINGS[key]:
            shown = SETTINGS[key]["fallback"]
        settings.append({"key": key, "label": SETTINGS[key].get("label", key),
                         "help": SETTINGS[key]["help"], "kind": SETTINGS[key]["kind"],
                         "settable": key in SETTABLE, "value": value, "shown": shown,
                         "origin": origin, "stored": stored.get(key)})
    return {"file": str(in_effect) if in_effect else None, "writable": str(writable_path()),
            "settings": settings, "unreadable": unreadable, "stale": stale_config_note()}


def render_config(view):
    """`--config` output: Markdown, like every other report the CLI prints."""
    out = ["## Config", ""]
    if view["unreadable"]:
        out += [f"Warning: {view['unreadable']}", ""]
    for s in view["settings"]:
        value = s["shown"]
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value) if value else "(none)"
        out.append(f"- `{s['key']}`: {value if value not in (None, '') else '(unset)'}")
        out.append(f"    from {s['origin']} · {s['help']}")
    out += ["", f"Config file: {view['writable']}"
                + ("" if view["file"] else "  (not present)"), ""]
    if view["stale"]:
        out += [view["stale"], ""]
    out += [f"To change one: `pdx-audit --set vanilla_repo /path/to/my-tracker.git`, "
                f"or `--unset <key>`. Settable: {', '.join(SETTABLE)}.", ""]
    return "\n".join(out)


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
