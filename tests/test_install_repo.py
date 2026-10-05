"""A git repository of the whole game install, made with plain git at the install
folder, serves as the tracker. Its game files sit under `game/`, and its first
commit can hold no game files. The audits read it as they read a tracker that
--commit made."""
import io
import os
import subprocess
import types
from pathlib import Path
from contextlib import redirect_stdout

import pytest

from conftest import MOD, VANILLA_NEW, VANILLA_OLD, _write_tree
from pdxaudit.gui import run_gui_audit
from pdxaudit.loc import run_loc_audit
from pdxaudit.overrides import run_override_audit
from pdxaudit.tracker import (cache_dir_of, do_commit, game_prefix, get_commits,
                              git_dir_of, locate_vanilla_repo, tree_files)


def _git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    return subprocess.run(["git", "-C", str(cwd), *args], env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit_version(install, files, message):
    _write_tree(install / "game", files)
    _git(install, "add", "-A")
    _git(install, "commit", "-q", "-m", message)
    return _git(install, "rev-parse", "HEAD")


@pytest.fixture
def install(tmp_path):
    """The install folder with its own repository: a first commit with no game
    files, then two versions, with a file outside the game folder in each."""
    root = tmp_path / "Europa Universalis V"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "readme.txt").write_text("not game data\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "Initial commit")
    (root / "binaries").mkdir()
    (root / "binaries" / "license.txt").write_text("text\n")
    old = _commit_version(root, VANILLA_OLD, "1.0.0 Test")
    new = _commit_version(root, VANILLA_NEW, "1.1.0 Test")
    mod = tmp_path / "mod"
    _write_tree(mod, MOD)
    return types.SimpleNamespace(root=root, repo=str(root / ".git"), old=old, new=new, mod=mod,
                                 args=types.SimpleNamespace(diff=False, block=None, category=None,
                                                            full=True, old=None, new=None))


def _out(fn, *a):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*a)
    return buf.getvalue()


def test_the_install_folder_names_its_git_folder(install):
    assert git_dir_of(install.root) == install.root / ".git"
    repo, err = locate_vanilla_repo(install.mod, str(install.root))
    assert err is None and repo == install.root / ".git"


def test_the_game_folder_is_found_in_each_commit(install):
    assert game_prefix(install.repo, install.new) == "game"
    assert game_prefix(install.repo, "HEAD~2") is None


def test_a_commit_with_no_game_files_is_not_a_version(install):
    commits = get_commits(install.repo)
    assert [m for _h, m in commits] == ["1.1.0 Test", "1.0.0 Test"]


def test_paths_start_at_the_module_folders(install):
    paths = {p for p, _blob in tree_files(install.repo, install.new)}
    assert paths == set(VANILLA_NEW)


def test_the_audits_read_the_install_repository(install):
    out = _out(run_override_audit, install.mod, install.repo,
               install.old, "1.0.0", install.new, "1.1.0", install.args)
    assert "2 REPLACE changes to take or check" in out
    assert "upkeep = 5" in out and "legacy_mod = 1" in out
    out = _out(run_gui_audit, install.mod, install.repo,
               install.old, "1.0.0", install.new, "1.1.0", install.args)
    assert "1 changes in 1 shadowed definitions" in out
    out = _out(run_loc_audit, install.mod, install.repo,
               install.old, "1.0.0", install.new, "1.1.0", install.args)
    assert "1 changed strings" in out


def test_the_cache_stays_out_of_the_install(install):
    assert cache_dir_of(install.repo) == install.root / ".git" / "pdx-audit-cache"
    _out(run_override_audit, install.mod, install.repo,
         install.old, "1.0.0", install.new, "1.1.0", install.args)
    assert not (install.root / "cache").exists()
    assert list((install.root / ".git" / "pdx-audit-cache").glob("blocks-v*.json"))


def test_commit_refuses_the_install_repository(install, tmp_path, capsys):
    head = _git(install.root, "rev-parse", "HEAD")
    with pytest.raises(SystemExit):
        do_commit(install.root / ".git", "1.2.0", "Test", str(install.root / "game"))
    assert "Commit each new version with git" in capsys.readouterr().err
    assert _git(install.root, "rev-parse", "HEAD") == head
    assert _git(install.root, "tag", "-l") == ""


def test_a_tracker_with_core_bare_false_keeps_its_cache_and_commits(tmp_path, monkeypatch):
    # A tracker folder that git once used with a working tree carries
    # `core.bare = false`; it is still a tracker, not the .git folder of an install.
    from conftest import build_tracker
    tr = build_tracker(tmp_path, [("1.0", VANILLA_OLD)])
    subprocess.run(["git", "--git-dir", tr.repo, "config", "core.bare", "false"], check=True)
    assert cache_dir_of(tr.repo) == tmp_path / "vanilla-tracker" / "cache"
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@e"),
                 ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@e")):
        monkeypatch.setenv(k, v)
    game = tmp_path / "game"
    _write_tree(game, VANILLA_NEW)
    do_commit(Path(tr.repo), "1.1", "Test", str(game))
    assert [m for _h, m in get_commits(tr.repo)] == ["1.1 Test", "1.0 Test"]
