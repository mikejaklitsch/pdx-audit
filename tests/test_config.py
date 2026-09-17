"""Config loading, value precedence, the settings --set writes, and skip-list
matching."""
import json
import pathlib
import types

import pytest

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


# --- settings: where a value comes from, and writing the per-user file ------------


@pytest.fixture
def cfg_files(tmp_path, monkeypatch):
    """The four config files, all under tmp_path, so nothing on this machine is read
    or written. Returns the home file, the per-user data file and the repo file."""
    monkeypatch.delenv("PDX_AUDIT_CONFIG", raising=False)
    for key in ("PDX_VANILLA_REPO", "PDX_GAME_ROOT", "PDX_PATCH_NAME"):
        monkeypatch.delenv(key, raising=False)
    home, data, repo = (tmp_path / n for n in ("home.json", "data.json", "repo.json"))
    monkeypatch.setattr(config, "_home_config", lambda: home)
    monkeypatch.setattr(config, "writable_path", lambda: data)
    monkeypatch.setattr(config, "_repo_config", lambda: repo)
    config.invalidate()
    yield types.SimpleNamespace(home=home, data=data, repo=repo)
    config.invalidate()


def _tracker(path):
    """A path that passes the tracker check: a bare repo's HEAD and objects/."""
    (path / "objects").mkdir(parents=True)
    (path / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
    return path


def test_setting_precedence_flag_env_file_default(cfg_files, monkeypatch):
    cfg_files.data.parent.mkdir(parents=True, exist_ok=True)
    cfg_files.data.write_text('{"patch_name": "FromFile"}', encoding="utf-8")
    config.invalidate()
    assert config.setting("patch_name") == ("FromFile", str(cfg_files.data))
    monkeypatch.setenv("PDX_PATCH_NAME", "FromEnv")
    assert config.setting("patch_name") == ("FromEnv", "$PDX_PATCH_NAME")
    assert config.setting("patch_name", "FromFlag") == ("FromFlag", "--patch-name")
    cfg_files.data.write_text("{}", encoding="utf-8")
    monkeypatch.delenv("PDX_PATCH_NAME")
    config.invalidate()
    assert config.setting("patch_name") == ("Pavia", "the built-in default")


def test_set_writes_the_per_user_file_and_takes_effect(cfg_files, tmp_path):
    repo = _tracker(tmp_path / "my-tracker.git")
    messages = config.set_value("vanilla_repo", str(repo))
    assert f"Set vanilla_repo to {repo}" in messages[0]
    assert json.loads(cfg_files.data.read_text(encoding="utf-8"))["vanilla_repo"] == str(repo)
    assert config.cfg("vanilla_repo") == str(repo)      # the write invalidated the cache


def test_a_tracker_that_does_not_exist_yet_is_kept_with_a_note(cfg_files, tmp_path):
    messages = config.set_value("vanilla_repo", str(tmp_path / "new.git"))
    assert config.cfg("vanilla_repo") == str(tmp_path / "new.git")
    assert any("does not exist yet" in m for m in messages)


def test_a_tracker_that_is_not_a_repo_is_refused(cfg_files, tmp_path):
    folder = tmp_path / "not-a-repo"
    (folder / "sub").mkdir(parents=True)
    with pytest.raises(config.ConfigError, match="not a bare git repository"):
        config.set_value("vanilla_repo", str(folder))
    assert not cfg_files.data.exists()


def test_a_game_folder_must_exist(cfg_files, tmp_path):
    with pytest.raises(config.ConfigError, match="not a folder"):
        config.set_value("game_root", str(tmp_path / "nope"))
    game = tmp_path / "game"
    (game / "in_game").mkdir(parents=True)
    assert config.set_value("game_root", str(game))


def test_an_unsettable_key_is_refused(cfg_files):
    with pytest.raises(config.ConfigError, match="not a setting"):
        config.set_value("skip_dirs", "backup")


def test_creating_the_per_user_file_copies_the_repo_file_it_shadows(cfg_files, tmp_path):
    cfg_files.repo.write_text('{"game_root": "/g", "skip_dirs": ["backup"]}', encoding="utf-8")
    config.invalidate()
    messages = config.set_value("patch_name", "Cortes")
    stored = json.loads(cfg_files.data.read_text(encoding="utf-8"))
    assert stored == {"game_root": "/g", "skip_dirs": ["backup"], "patch_name": "Cortes"}
    assert any("copied into it" in m for m in messages)


def test_a_file_read_earlier_is_reported_as_still_winning(cfg_files, tmp_path):
    cfg_files.home.write_text('{"patch_name": "Home"}', encoding="utf-8")
    config.invalidate()
    messages = config.set_value("patch_name", "Cortes")
    assert any("is read instead of this file" in m for m in messages)
    assert config.cfg("patch_name") == "Home"


def test_an_environment_variable_is_reported_as_still_winning(cfg_files, monkeypatch, tmp_path):
    monkeypatch.setenv("PDX_VANILLA_REPO", str(tmp_path / "env.git"))
    messages = config.set_value("vanilla_repo", str(_tracker(tmp_path / "t.git")))
    assert any("$PDX_VANILLA_REPO" in m and "outranks" in m for m in messages)


def test_unset_falls_back_to_the_setting_below(cfg_files):
    config.set_value("patch_name", "Cortes")
    assert config.cfg("patch_name") == "Cortes"
    messages = config.unset_value("patch_name")
    assert "Pavia (from the built-in default)" in messages[0]
    assert config.cfg("patch_name") is None
    assert config.unset_value("patch_name") == [f"patch_name is not set in {cfg_files.data}."]


def test_config_view_marks_the_file_in_effect_and_the_one_written(cfg_files, tmp_path):
    cfg_files.repo.write_text('{"patch_name": "Repo"}', encoding="utf-8")
    config.invalidate()
    view = config.config_view()
    assert view["file"] == str(cfg_files.repo)
    assert view["writable"] == str(cfg_files.data)
    marks = {c["path"]: (c["exists"], c["in_effect"], c["writable"]) for c in view["candidates"]}
    assert marks[str(cfg_files.repo)] == (True, True, False)
    assert marks[str(cfg_files.data)] == (False, False, True)
    patch = next(s for s in view["settings"] if s["key"] == "patch_name")
    assert (patch["value"], patch["origin"], patch["settable"]) == ("Repo", str(cfg_files.repo), True)
    assert next(s for s in view["settings"] if s["key"] == "skip_dirs")["settable"] is False


def test_render_config_names_every_setting_and_file(cfg_files):
    text = config.render_config(config.config_view())
    for key in config.SETTINGS:
        assert f"`{key}`" in text
    assert str(cfg_files.data) in text


def test_a_config_file_that_cannot_be_read_is_reported_not_raised(cfg_files, capsys):
    cfg_files.data.write_text("not json", encoding="utf-8")
    config.invalidate()
    view = config.config_view()                       # the diagnosis must not crash
    assert "could not read" in view["unreadable"]
    assert "could not read" in config.render_config(view)
    assert next(s for s in view["settings"] if s["key"] == "patch_name")["value"] == "Pavia"


def test_set_refuses_to_write_over_a_file_it_cannot_read(cfg_files):
    cfg_files.data.write_text("not json", encoding="utf-8")
    config.invalidate()
    with pytest.raises(config.ConfigError, match="could not read"):
        config.set_value("patch_name", "Cortes")
    assert cfg_files.data.read_text(encoding="utf-8") == "not json"


def test_a_write_that_fails_is_a_config_error(cfg_files, monkeypatch, tmp_path):
    def refuse(*_a, **_k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(pathlib.Path, "write_text", refuse)
    with pytest.raises(config.ConfigError, match="could not write"):
        config.set_value("patch_name", "Cortes")


def test_an_empty_value_is_refused_rather_than_stored_as_the_cwd(cfg_files):
    for key in config.SETTABLE:
        with pytest.raises(config.ConfigError, match="needs a value"):
            config.set_value(key, "")
    assert not cfg_files.data.exists()


def test_the_shadowed_file_is_copied_in_even_when_a_file_above_exists(cfg_files):
    # Following the advice to remove the file above must not lose the file below.
    cfg_files.home.write_text('{"patch_name": "Home"}', encoding="utf-8")
    cfg_files.repo.write_text('{"skip_dirs": ["backup"], "game_root": "/g"}', encoding="utf-8")
    config.invalidate()
    messages = config.set_value("patch_name", "Cortes")
    assert any("is read instead of this file" in m for m in messages)
    stored = json.loads(cfg_files.data.read_text(encoding="utf-8"))
    assert stored == {"skip_dirs": ["backup"], "game_root": "/g", "patch_name": "Cortes"}


def test_an_unset_setting_reports_the_fallback_runs_use(cfg_files):
    view = config.config_view()
    tracker = next(s for s in view["settings"] if s["key"] == "vanilla_repo")
    assert tracker["value"] is None                   # nothing an editor should offer
    assert tracker["shown"] == "<mod-parent>/vanilla-tracker/repo.git"
    game = next(s for s in view["settings"] if s["key"] == "game_root")
    assert game["shown"] == str(config.DEFAULT_VANILLA_ROOT)
    assert "<mod-parent>/vanilla-tracker/repo.git" in config.render_config(view)


def test_unset_points_at_the_file_that_actually_holds_the_setting(cfg_files):
    cfg_files.home.write_text('{"patch_name": "Home"}', encoding="utf-8")
    config.invalidate()
    message, = config.unset_value("patch_name")
    assert str(cfg_files.home) in message and "change it there" in message
