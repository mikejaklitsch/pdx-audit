"""The single removal helper: the only code allowed to delete, one validated
file at a time. Every refusal path must leave the target in place."""
import os

import pytest

from pdxaudit.safety import remove_file, RefusedRemoval

PATTERN = r"[0-9a-f]{40}\.json"
NAME = "a" * 40 + ".json"


def test_removes_matching_regular_file(tmp_path):
    target = tmp_path / NAME
    target.write_text("{}")
    remove_file(target, tmp_path, PATTERN)
    assert not target.exists()


def test_refuses_name_that_does_not_match(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("keep me")
    with pytest.raises(RefusedRemoval):
        remove_file(target, tmp_path, PATTERN)
    assert target.exists()


def test_refuses_file_outside_expected_folder(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    target = sub / NAME
    target.write_text("{}")
    with pytest.raises(RefusedRemoval):
        remove_file(target, tmp_path, PATTERN)
    assert target.exists()


def test_refuses_parent_traversal(tmp_path):
    expected = tmp_path / "commits"
    expected.mkdir()
    target = tmp_path / NAME
    target.write_text("{}")
    with pytest.raises(RefusedRemoval):
        remove_file(expected / ".." / NAME, expected, PATTERN)
    assert target.exists()


def test_refuses_symlink(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("keep me")
    link = tmp_path / NAME
    os.symlink(real, link)
    with pytest.raises(RefusedRemoval):
        remove_file(link, tmp_path, PATTERN)
    assert link.is_symlink() and real.exists()


def test_refuses_directory(tmp_path):
    d = tmp_path / NAME
    d.mkdir()
    (d / "inner.txt").write_text("keep me")
    with pytest.raises(RefusedRemoval):
        remove_file(d, tmp_path, PATTERN)
    assert (d / "inner.txt").exists()


def test_refuses_missing_file(tmp_path):
    with pytest.raises(RefusedRemoval):
        remove_file(tmp_path / NAME, tmp_path, PATTERN)


def test_refuses_filesystem_root_as_expected_folder(tmp_path):
    target = tmp_path / NAME
    target.write_text("{}")
    with pytest.raises(RefusedRemoval):
        remove_file(target, "/", PATTERN)
    assert target.exists()
