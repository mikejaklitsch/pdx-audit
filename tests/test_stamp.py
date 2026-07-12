"""Fork-point pins and the stamp write path (add + refresh), forcing the
interactive confirmation. No vanilla tracker needed: fork detection is stubbed."""
import types

import pdxaudit.gui as gui


def test_parse_fork_pin_top_of_file_only():
    assert gui.parse_fork_pin("# pdx-audit fork-point: 1.3.8\nrest\n") == "1.3.8"
    assert gui.parse_fork_pin("line\n" * 30 + "# pdx-audit fork-point: 9.9\n") is None
    assert gui.parse_fork_pin("no pin here\n") is None


def test_stamp_add_prepends_and_preserves_bom(tmp_path):
    p = tmp_path / "f.gui"
    p.write_bytes(b"\xef\xbb\xbftypes X {}\n")
    gui._stamp_add(p, "1.3.10")
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert gui.parse_fork_pin(raw.decode("utf-8-sig")) == "1.3.10"
    assert raw.decode("utf-8-sig").splitlines()[1] == "types X {}"


def test_stamp_update_rewrites_in_place(tmp_path):
    p = tmp_path / "f.gui"
    p.write_bytes(b"\xef\xbb\xbf# pdx-audit fork-point: 1.3.10\nbody\n")
    gui._stamp_update(p, "1.2.2")
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    assert gui.parse_fork_pin(text) == "1.2.2"
    assert text.count("pdx-audit fork-point") == 1     # not duplicated
    assert "body" in text


def _mk_mod(tmp_path):
    d = tmp_path / "mod"
    (d / ".metadata").mkdir(parents=True)
    (d / "in_game" / "gui").mkdir(parents=True)
    (d / "in_game/gui/a.gui").write_bytes(b"\xef\xbb\xbftypes A {}\n")  # unpinned
    (d / "in_game/gui/b.gui").write_bytes(
        b"\xef\xbb\xbf# pdx-audit fork-point: 1.3.10\ntypes B {}\n")     # stale pin
    return d


def _stub_forks(monkeypatch):
    def fake(vanilla_repo, commits, modules, mdefs, mod_file_texts):
        file_base = {
            "in_game/gui/a.gui": ("t", "h", "1.3.10 Pavia", False),   # unpinned -> add
            "in_game/gui/b.gui": ("t", "h", "1.3.10 Pavia", True),    # pinned, stale
        }
        pin_stale = {"in_game/gui/b.gui": ("1.3.10", "1.2.2")}
        return {}, file_base, {}, pin_stale
    monkeypatch.setattr(gui, "build_fork_baselines", fake)
    monkeypatch.setattr(gui, "get_commits", lambda repo: [("h", "1.3.10 Pavia")])
    # force an interactive "yes"
    monkeypatch.setattr(gui.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(gui, "input", lambda *a, **k: "y", raising=False)


def test_stamp_add_writes_unpinned_only(tmp_path, monkeypatch):
    d = _mk_mod(tmp_path)
    _stub_forks(monkeypatch)
    gui.run_stamp_fork_points(d, "repo", [("h", "1.3.10 Pavia")], refresh=False)
    a = (d / "in_game/gui/a.gui").read_text(encoding="utf-8-sig")
    b = (d / "in_game/gui/b.gui").read_text(encoding="utf-8-sig")
    assert gui.parse_fork_pin(a) == "1.3.10"     # added
    assert gui.parse_fork_pin(b) == "1.3.10"     # stale pin left untouched without --refresh


def test_stamp_refresh_updates_stale_pin(tmp_path, monkeypatch):
    d = _mk_mod(tmp_path)
    _stub_forks(monkeypatch)
    gui.run_stamp_fork_points(d, "repo", [("h", "1.3.10 Pavia")], refresh=True)
    b = (d / "in_game/gui/b.gui").read_text(encoding="utf-8-sig")
    assert gui.parse_fork_pin(b) == "1.2.2"      # refreshed 1.3.10 -> 1.2.2


def test_stamp_refuses_without_tty(tmp_path, monkeypatch):
    d = _mk_mod(tmp_path)
    _stub_forks(monkeypatch)
    monkeypatch.setattr(gui.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    import pytest
    with pytest.raises(SystemExit):
        gui.run_stamp_fork_points(d, "repo", [("h", "1.3.10 Pavia")], refresh=False)
    # nothing written
    assert gui.parse_fork_pin((d / "in_game/gui/a.gui").read_text(encoding="utf-8-sig")) is None
