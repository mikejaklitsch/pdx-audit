import os
import platform
import re
import sys
from pathlib import Path

DEFAULT_VANILLA_ROOT = Path(
    "/mnt/d/Program Files (x86)/Steam/steamapps/common/Europa Universalis V/game")

_WIN_DRIVE = re.compile(r"^([A-Za-z]):(?:[\\/](.*))?$")
_WSL_MOUNT = re.compile(r"^/mnt/([A-Za-z])(?:/(.*))?$")


def _on_wsl():
    return sys.platform.startswith("linux") and "microsoft" in platform.uname().release.lower()


def _on_windows():
    return os.name == "nt"


def native_form(path, wsl=None, windows=None):
    """A path in this system's own drive form: under WSL a Windows drive path
    (C:\\x or C:/x) becomes /mnt/c/x; on Windows /mnt/c/x becomes C:\\x."""
    s = str(path)
    wsl = _on_wsl() if wsl is None else wsl
    windows = _on_windows() if windows is None else windows
    if wsl:
        m = _WIN_DRIVE.match(s)
        if m:
            return f"/mnt/{m.group(1).lower()}/" + (m.group(2) or "").replace("\\", "/")
    elif windows:
        m = _WSL_MOUNT.match(s.replace("\\", "/"))
        if m:
            return f"{m.group(1).upper()}:\\" + (m.group(2) or "").replace("/", "\\")
    return s


def canonical_path(path):
    """One spelling for a folder: in this system's drive form (native_form),
    absolute, symlinks resolved."""
    p = Path(native_form(path)).expanduser()
    try:
        p = p.resolve()
    except OSError:
        p = p.absolute()
    return str(p)


def path_key(path):
    """A folder's canonical path in the form paths are compared in: casefolded on
    case-insensitive volumes (Windows drives and their /mnt/<drive> mounts)."""
    c = canonical_path(path)
    if _on_windows() or (_on_wsl() and _WSL_MOUNT.match(c)):
        return c.casefold().rstrip("/\\")
    return c.rstrip("/") or "/"


def find_mod_root(start=None):
    """Walk up from start (or cwd) looking for .metadata/ to find mod root."""
    p = (start or Path.cwd()).resolve()
    while p != p.parent:
        if (p / ".metadata").is_dir():
            return p
        p = p.parent
    return None


def find_mod_root_or_exit(start=None, override=None):
    """Like find_mod_root but prints an error and exits if not found.
    If override is given, use that path directly; it must be a mod folder."""
    if override:
        root = Path(override).resolve()
        if not root.is_dir():
            print(f"Error: mod folder not found: {root}", file=sys.stderr)
            sys.exit(1)
        if not (root / ".metadata").is_dir():
            print(f"Error: {root} is not a mod folder: it has no .metadata/ directory.", file=sys.stderr)
            sys.exit(1)
        return root
    root = find_mod_root(start)
    if root is None:
        print("Error: could not find mod root (.metadata/ directory). "
              "Run from inside a mod directory.", file=sys.stderr)
        sys.exit(1)
    return root


def vanilla_root():
    env = os.environ.get("PDX_GAME_ROOT")
    return Path(env) if env else DEFAULT_VANILLA_ROOT


def find_vanilla_repo(mod_root=None, override=None):
    """Locate the vanilla-tracker bare git repo.
    Precedence: override > $PDX_VANILLA_REPO > <mod-parent>/vanilla-tracker/repo.git."""
    if override:
        p = Path(override).resolve()
        if p.exists():
            return p
        return None

    env = os.environ.get("PDX_VANILLA_REPO")
    if env:
        p = Path(env).resolve()
        if p.exists():
            return p

    if mod_root:
        candidate = mod_root.parent / "vanilla-tracker" / "repo.git"
        if candidate.exists():
            return candidate

    return None
