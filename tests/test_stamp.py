"""--stamp-fork-points saves detected fork points into the per-user findings
record (reviewed_against). It never touches the mod's files and needs no
confirmation. Fork detection is stubbed; no vanilla tracker needed."""
import types

import pytest

import pdxaudit.gui as gui
from pdxaudit import store as st


@pytest.fixture
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg"


def _mk_mod(tmp_path):
    d = tmp_path / "mod"
    (d / ".metadata").mkdir(parents=True)
    (d / ".metadata/metadata.json").write_text('{"id": "testmod"}')
    (d / "in_game" / "gui").mkdir(parents=True)
    (d / "in_game/gui/a.gui").write_bytes(b"\xef\xbb\xbftemplate A = { x = 1 }\n")
    (d / "in_game/gui/b.gui").write_bytes(b"\xef\xbb\xbftemplate B = { x = 1 }\n")
    return d


def _stub_forks(monkeypatch, tag="1.3.10"):
    def fake(vanilla_repo, commits, modules, mdefs, mod_file_texts, bases=None):
        msg = f"{tag} Pavia"
        def_base = {("in_game", "template", "A"): ("in_game/gui/v.gui", "t", "h", msg, False)}
        file_base = {"in_game/gui/b.gui": ("t", "h", msg, False)}
        return def_base, file_base
    monkeypatch.setattr(gui, "build_fork_baselines", fake)


def _snapshot(d):
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in d.rglob("*") if p.is_file()}


def test_stamp_writes_record_not_mod(tmp_path, monkeypatch, data_home):
    d = _mk_mod(tmp_path)
    before = _snapshot(d)
    _stub_forks(monkeypatch)
    store = st.Store(d, "testmod")
    gui.run_stamp_fork_points(d, "repo", [("h", "1.3.10 Pavia")], store, refresh=False)
    ra = st.Store(d, "testmod").state["reviewed_against"]
    assert ra == {"gui:in_game/template/A": "1.3.10", "guifile:in_game/gui/b.gui": "1.3.10"}
    assert _snapshot(d) == before


def test_stamp_keeps_existing_entries_without_refresh(tmp_path, monkeypatch, data_home):
    d = _mk_mod(tmp_path)
    store = st.Store(d, "testmod")
    store.state["reviewed_against"]["guifile:in_game/gui/b.gui"] = "1.2.2"
    store.save()
    _stub_forks(monkeypatch)
    gui.run_stamp_fork_points(d, "repo", [("h", "1.3.10 Pavia")], st.Store(d, "testmod"),
                              refresh=False)
    ra = st.Store(d, "testmod").state["reviewed_against"]
    assert ra["guifile:in_game/gui/b.gui"] == "1.2.2"
    assert ra["gui:in_game/template/A"] == "1.3.10"


def test_stamp_refresh_updates_existing_entries(tmp_path, monkeypatch, data_home):
    d = _mk_mod(tmp_path)
    store = st.Store(d, "testmod")
    store.state["reviewed_against"]["guifile:in_game/gui/b.gui"] = "1.2.2"
    store.save()
    _stub_forks(monkeypatch)
    gui.run_stamp_fork_points(d, "repo", [("h", "1.3.10 Pavia")], st.Store(d, "testmod"),
                              refresh=True)
    assert st.Store(d, "testmod").state["reviewed_against"]["guifile:in_game/gui/b.gui"] == "1.3.10"


def test_stamp_needs_no_terminal(tmp_path, monkeypatch, data_home):
    d = _mk_mod(tmp_path)
    _stub_forks(monkeypatch)
    monkeypatch.setattr(gui.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    gui.run_stamp_fork_points(d, "repo", [("h", "1.3.10 Pavia")], st.Store(d, "testmod"))
    assert st.Store(d, "testmod").state["reviewed_against"]


def test_pin_comments_in_mod_files_are_ignored(tmp_path, monkeypatch, data_home):
    assert not hasattr(gui, "parse_fork_pin")
    assert not hasattr(gui, "_stamp_add") and not hasattr(gui, "_stamp_update")
