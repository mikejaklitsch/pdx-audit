"""--commit records a game install into the tracker without a temporary work
tree: files are hashed straight from the game folder into git, and the only
temporary item is an index file inside the tracker repo, removed afterwards."""
import subprocess

import pytest

from pdxaudit.tracker import do_commit


@pytest.fixture
def git_identity(monkeypatch):
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@e"),
                 ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@e")):
        monkeypatch.setenv(k, v)


def _game(tmp_path):
    g = tmp_path / "game"
    (g / "in_game/common/buildings").mkdir(parents=True)
    (g / "in_game/gui").mkdir(parents=True)
    (g / "in_game/gfx").mkdir(parents=True)
    (g / "in_game/common/buildings/a.txt").write_bytes(b"\xef\xbb\xbfa = {\r\n}\r\n")
    (g / "in_game/gui/w.gui").write_text("template x = {}\n")
    (g / "in_game/gfx/skip.dds").write_bytes(b"binary")
    return g


def _tree(repo, ref="HEAD"):
    out = subprocess.run(["git", "--git-dir", str(repo), "ls-tree", "-r",
                          "--name-only", ref], capture_output=True, text=True)
    return set(out.stdout.split())


def test_snapshot_commits_script_files_only(tmp_path, git_identity):
    repo = tmp_path / "tracker" / "repo.git"
    game = _game(tmp_path)
    do_commit(repo, "1.0.0", "Test", str(game))
    assert _tree(repo) == {"in_game/common/buildings/a.txt", "in_game/gui/w.gui"}
    tags = subprocess.run(["git", "--git-dir", str(repo), "tag", "-l"],
                          capture_output=True, text=True).stdout.split()
    assert tags == ["1.0.0"]


def test_snapshot_stores_raw_bytes(tmp_path, git_identity):
    repo = tmp_path / "tracker" / "repo.git"
    game = _game(tmp_path)
    do_commit(repo, "1.0.0", "Test", str(game))
    blob = subprocess.run(["git", "--git-dir", str(repo), "show",
                           "HEAD:in_game/common/buildings/a.txt"], capture_output=True).stdout
    assert blob == b"\xef\xbb\xbfa = {\r\n}\r\n"   # BOM and CRLF untouched


def test_snapshot_leaves_no_index_file(tmp_path, git_identity):
    repo = tmp_path / "tracker" / "repo.git"
    do_commit(repo, "1.0.0", "Test", str(_game(tmp_path)))
    assert not [p for p in repo.iterdir() if "index" in p.name.lower()
                and p.name != "index"]
    assert not (repo / "pdx-audit-snapshot.index").exists()


def test_unchanged_install_commits_nothing(tmp_path, git_identity, capsys):
    repo = tmp_path / "tracker" / "repo.git"
    game = _game(tmp_path)
    do_commit(repo, "1.0.0", "Test", str(game))
    do_commit(repo, "1.0.1", "Test", str(game))
    assert "nothing committed" in capsys.readouterr().out
    count = subprocess.run(["git", "--git-dir", str(repo), "rev-list", "--count", "HEAD"],
                           capture_output=True, text=True).stdout.strip()
    assert count == "1"


def test_removed_game_file_is_dropped_from_next_snapshot(tmp_path, git_identity):
    repo = tmp_path / "tracker" / "repo.git"
    game = _game(tmp_path)
    do_commit(repo, "1.0.0", "Test", str(game))
    (game / "in_game/gui/w.gui").write_text("")      # still present, now empty
    (game / "in_game/common/buildings/b.txt").write_text("b = {}\n")
    moved = game / "in_game/common/buildings/a.txt"
    moved.rename(game / "in_game/common/buildings/a.txt.old")
    do_commit(repo, "1.1.0", "Test", str(game))
    assert _tree(repo) == {"in_game/common/buildings/b.txt", "in_game/gui/w.gui"}
    assert _tree(repo, "1.0.0") == {"in_game/common/buildings/a.txt", "in_game/gui/w.gui"}


def test_out_of_order_snapshot_does_not_suggest_deleting(tmp_path, git_identity, capsys):
    repo = tmp_path / "tracker" / "repo.git"
    game = _game(tmp_path)
    do_commit(repo, "1.2.0", "Test", str(game))
    with pytest.raises(SystemExit):
        do_commit(repo, "1.1.0", "Test", str(game))
    err = capsys.readouterr().err
    assert "delete" not in err.lower()
    assert "--vanilla-repo" in err
