"""Per-user storage for findings records: keyed by mod id and commit, read from
the nearest first-parent ancestor, written only on change, never inside the mod,
and orphaned records removed only one validated file at a time."""
import json
import re
import subprocess
import types

import pytest

from pdxaudit import store as st


@pytest.fixture
def data_home(tmp_path, monkeypatch):
    home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(home))
    monkeypatch.setattr(st.sys, "platform", "linux")
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@e"),
                 ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@e")):
        monkeypatch.setenv(k, v)
    return home


def _mod(tmp_path, mod_id="testmod"):
    mod = tmp_path / "mod"
    (mod / ".metadata").mkdir(parents=True)
    (mod / ".metadata/metadata.json").write_text(
        json.dumps({"name": "Test Mod", "id": mod_id}), encoding="utf-8")
    return mod


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _repo(mod):
    _git(mod, "init", "-q", "-b", "main")
    return _commit(mod, "first")


def _commit(mod, msg):
    (mod / (re.sub(r"\W+", "_", msg) + ".txt")).write_text(msg)
    _git(mod, "add", "-A")
    _git(mod, "commit", "-q", "-m", msg)
    return _git(mod, "rev-parse", "HEAD")


def _files(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if ".git" not in p.relative_to(root).parts)


# --- location and mod id ----------------------------------------------------

def test_data_root_is_per_user_data_folder(data_home):
    assert st.data_root() == data_home / "pdx-audit"


def test_mod_id_read_from_metadata(tmp_path):
    assert st.read_mod_id(_mod(tmp_path, "meiou_and_taxes")) == ("meiou_and_taxes", None)


@pytest.mark.parametrize("bad", ["", "..", "a/b", "a\\b", "c:x", ".hidden"])
def test_unsafe_mod_id_refused(tmp_path, bad):
    mod_id, err = st.read_mod_id(_mod(tmp_path, bad))
    assert mod_id is None and err


def test_missing_metadata_refused(tmp_path):
    mod_id, err = st.read_mod_id(tmp_path)
    assert mod_id is None and err


# --- non-git mods -----------------------------------------------------------

def test_non_git_mod_uses_record_json(data_home, tmp_path):
    mod = _mod(tmp_path)
    before = _files(mod)
    s = st.Store(mod, "testmod")
    assert s.head is None
    s.state["dismissed"]["abc"] = {"finding": "x"}
    assert s.save()
    assert (data_home / "pdx-audit/testmod/record.json").is_file()
    assert _files(mod) == before                 # nothing written into the mod
    again = st.Store(mod, "testmod")
    assert "abc" in again.state["dismissed"]


def test_unchanged_state_is_not_written(data_home, tmp_path):
    mod = _mod(tmp_path)
    s = st.Store(mod, "testmod")
    assert not s.save()
    assert not (data_home / "pdx-audit/testmod").exists()


def test_save_leaves_no_temp_files(data_home, tmp_path):
    mod = _mod(tmp_path)
    s = st.Store(mod, "testmod")
    s.state["open"]["k"] = {"finding": "x"}
    s.save()
    names = [p.name for p in (data_home / "pdx-audit/testmod").iterdir()]
    assert names == ["record.json"]


# --- git mods: commit keyed, first-parent inheritance ------------------------

def test_record_keyed_by_head_commit(data_home, tmp_path):
    mod = _mod(tmp_path)
    a = _repo(mod)
    s = st.Store(mod, "testmod")
    assert s.head == a
    s.state["dismissed"]["x"] = {"finding": "k"}
    s.save()
    assert (data_home / f"pdx-audit/testmod/commits/{a}.json").is_file()


def test_new_commit_inherits_parent_record(data_home, tmp_path):
    mod = _mod(tmp_path)
    _repo(mod)
    s = st.Store(mod, "testmod")
    s.state["dismissed"]["x"] = {"finding": "k"}
    s.save()
    _commit(mod, "second")
    child = st.Store(mod, "testmod")
    assert "x" in child.state["dismissed"]
    assert not child.save()                      # inherited, unchanged: no new file


def test_branches_do_not_share_later_records_and_merge_keeps_first_parent(data_home, tmp_path):
    mod = _mod(tmp_path)
    a = _repo(mod)
    s = st.Store(mod, "testmod")
    s.state["dismissed"]["on_a"] = {}
    s.save()

    _commit(mod, "b on main")
    sb = st.Store(mod, "testmod")
    sb.state["dismissed"]["on_b"] = {}
    sb.save()

    _git(mod, "checkout", "-q", "-b", "feat", a)
    _commit(mod, "c on feat")
    sc = st.Store(mod, "testmod")
    assert set(sc.state["dismissed"]) == {"on_a"}      # B's decision not visible
    sc.state["dismissed"]["on_c"] = {}
    sc.save()

    _git(mod, "checkout", "-q", "main")
    _git(mod, "merge", "-q", "--no-ff", "feat", "-m", "merge feat")
    sm = st.Store(mod, "testmod")
    assert set(sm.state["dismissed"]) == {"on_a", "on_b"}   # first parent only


def test_worktree_style_second_clone_shares_common_history(data_home, tmp_path):
    mod = _mod(tmp_path)
    _repo(mod)
    s = st.Store(mod, "testmod")
    s.state["dismissed"]["shared"] = {}
    s.save()
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(mod), str(clone)], check=True)
    (clone / ".metadata").mkdir(exist_ok=True)
    (clone / ".metadata/metadata.json").write_text(json.dumps({"id": "testmod"}))
    assert "shared" in st.Store(clone, "testmod").state["dismissed"]


# --- orphaned records -------------------------------------------------------

def _orphan_setup(data_home, tmp_path):
    mod = _mod(tmp_path)
    _repo(mod)
    s = st.Store(mod, "testmod")
    s.state["dismissed"]["x"] = {}
    s.save()
    commits = data_home / "pdx-audit/testmod/commits"
    orphan = commits / ("f" * 40 + ".json")
    orphan.write_text("{}")
    stray = commits / "notes.json"
    stray.write_text("keep")
    return mod, s, orphan, stray


def test_orphans_listed_and_head_is_not_orphaned(data_home, tmp_path):
    mod, s, orphan, _stray = _orphan_setup(data_home, tmp_path)
    assert st.Store(mod, "testmod").orphans() == ["f" * 40]


def test_detached_head_record_is_not_orphaned(data_home, tmp_path):
    mod = _mod(tmp_path)
    _repo(mod)
    _commit(mod, "second")
    _git(mod, "checkout", "-q", "--detach", "HEAD")
    _commit(mod, "detached work")
    s = st.Store(mod, "testmod")
    s.state["dismissed"]["d"] = {}
    s.save()
    assert st.Store(mod, "testmod").orphans() == []


def test_remove_orphans_with_force_removes_only_orphans(data_home, tmp_path, capsys):
    mod, s, orphan, stray = _orphan_setup(data_home, tmp_path)
    head_file = data_home / f"pdx-audit/testmod/commits/{s.head}.json"
    code = st.remove_orphaned_records(st.Store(mod, "testmod"), force=True)
    assert code == 0
    assert not orphan.exists()
    assert head_file.exists() and stray.exists()
    out = capsys.readouterr().out
    assert str(orphan) in out and "Removed 1" in out


def test_remove_orphans_refuses_without_terminal(data_home, tmp_path, monkeypatch, capsys):
    mod, _s, orphan, _stray = _orphan_setup(data_home, tmp_path)
    monkeypatch.setattr(st.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    code = st.remove_orphaned_records(st.Store(mod, "testmod"), force=False)
    assert code != 0
    assert orphan.exists()
    assert "--force" in capsys.readouterr().err


def test_remove_orphans_needs_yes(data_home, tmp_path, monkeypatch):
    mod, _s, orphan, _stray = _orphan_setup(data_home, tmp_path)
    monkeypatch.setattr(st.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda *_a: "n")
    st.remove_orphaned_records(st.Store(mod, "testmod"), force=False)
    assert orphan.exists()
    monkeypatch.setattr("builtins.input", lambda *_a: "y")
    st.remove_orphaned_records(st.Store(mod, "testmod"), force=False)
    assert not orphan.exists()


def test_orphan_note_names_mod_and_commits(data_home, tmp_path):
    mod, _s, _orphan, _stray = _orphan_setup(data_home, tmp_path)
    note = st.orphan_note(st.Store(mod, "testmod"))
    assert "testmod" in note and "ffffffff" in note
    assert "pdx-audit --remove-orphaned-records" in note
