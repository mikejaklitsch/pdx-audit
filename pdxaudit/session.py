"""Memo for one audit run.

During a run neither the mod's files nor the vanilla tracker change, yet several
audits walk the same folders, read the same files, resolve the same commits and
normalize the same vanilla text. Inside `run()` each of those is computed once
and shared. Outside it every call computes afresh, so a long-lived caller (the
--display app, or a test that edits a mod between calls) never sees stale data."""
import os
from contextlib import contextmanager
from pathlib import Path

from pdx_utilities.constants import SCAN_TOPDIRS

_memo = None


@contextmanager
def run():
    """Share computed results across the audits of one run."""
    global _memo
    outer, _memo = _memo, {}
    try:
        yield
    finally:
        _memo = outer


def memo(key, compute):
    """compute(), called once per key within a run and every time outside one."""
    if _memo is None:
        return compute()
    if key not in _memo:
        _memo[key] = compute()
    return _memo[key]


def cached(key):
    """True when this run already holds a value for `key`."""
    return _memo is not None and key in _memo


def mod_paths(mod_root, suffix=""):
    """Sorted paths under the mod's module roots whose name ends with `suffix`:
    what `sorted(mod_root.rglob("*" + suffix))` lists inside the module roots,
    without walking the rest of the mod (its .git folder above all)."""
    root = Path(mod_root)
    paths = memo(("paths", str(root)), lambda: sorted(
        p for top in SCAN_TOPDIRS for p in (root / top).rglob("*")))
    suffix = os.path.normcase(suffix)
    return [p for p in paths if os.path.normcase(p.name).endswith(suffix)]


def read_text(path):
    """A mod file's text with any BOM stripped."""
    return memo(("text", str(path)), lambda: Path(path).read_text(encoding="utf-8-sig"))
