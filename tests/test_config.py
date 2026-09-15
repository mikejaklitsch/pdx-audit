"""Config loading, value precedence helper, and skip-list matching."""
import pdxaudit.config as config


def _set(monkeypatch, d):
    monkeypatch.setattr(config, "_CACHE", d)


def test_cfg_default_when_unset(monkeypatch):
    _set(monkeypatch, {})
    assert config.cfg("game_root") is None
    assert config.cfg("game_root", "fallback") == "fallback"


def test_cfg_empty_string_is_unset(monkeypatch):
    _set(monkeypatch, {"game_root": ""})
    assert config.cfg("game_root", "fallback") == "fallback"


def test_skip_dir_matches_component_and_subtree(monkeypatch):
    _set(monkeypatch, {"skip_dirs": ["backup", "in_game/gui/experimental"]})
    assert config.should_skip("in_game/gui/backup/x.gui")          # component
    assert config.should_skip("in_game/gui/experimental/y.gui")    # subtree prefix
    assert not config.should_skip("in_game/common/a.txt")


def test_skip_file_glob_basename_and_path(monkeypatch):
    _set(monkeypatch, {"skip_files": ["*.bak", "in_game/common/tmp_*.txt"]})
    assert config.should_skip("in_game/z.bak")                     # basename glob
    assert config.should_skip("in_game/common/tmp_foo.txt")        # full-path glob
    assert not config.should_skip("in_game/common/foo.txt")


def test_empty_config_skips_nothing(monkeypatch):
    _set(monkeypatch, {})
    assert not config.should_skip("in_game/gui/anything.gui")


def test_an_unreadable_config_file_is_reported_and_ignored(monkeypatch, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    monkeypatch.setenv("PDX_AUDIT_CONFIG", str(bad))
    _set(monkeypatch, None)
    assert config.load_config() == {}
    assert "could not read the config file" in capsys.readouterr().err


def test_windows_separators_normalized(monkeypatch):
    _set(monkeypatch, {"skip_dirs": ["backup"]})
    assert config.should_skip("in_game\\gui\\backup\\x.gui")
