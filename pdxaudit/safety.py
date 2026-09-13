"""The only place pdx-audit deletes anything.

remove_file deletes exactly one regular file, and only after checking its name,
its folder, and that it is not a symlink or directory. Nothing in pdx-audit
removes folders, and tests/test_no_recursive_removal.py fails the build if any
other code deletes files directly."""
import os
import re
from pathlib import Path


class RefusedRemoval(Exception):
    """A deletion was refused because the target failed validation."""


def remove_file(path, expected_dir, name_pattern):
    """Delete `path` if and only if its name fully matches `name_pattern`, it
    sits directly inside `expected_dir` (after resolving both), and it is a
    regular file. Raises RefusedRemoval otherwise, leaving the target alone."""
    p = Path(path)
    if not re.fullmatch(name_pattern, p.name):
        raise RefusedRemoval(f"refusing to remove {p}: name does not match {name_pattern}")
    expected = Path(expected_dir).resolve()
    if expected == Path(expected.anchor):
        raise RefusedRemoval(f"refusing to remove {p}: expected folder is a filesystem root")
    if p.is_symlink():
        raise RefusedRemoval(f"refusing to remove {p}: it is a symlink")
    if p.resolve().parent != expected:
        raise RefusedRemoval(f"refusing to remove {p}: it is not directly inside {expected}")
    if not p.is_file():
        raise RefusedRemoval(f"refusing to remove {p}: not an existing regular file")
    os.unlink(p)
