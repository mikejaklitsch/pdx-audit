"""The tracker freshness check samples script folders too, so a patch that only
touches common/ scripts is noticed, and it tells the user how to fix it."""
import pdxaudit.config as config
from conftest import build_tracker
from pdxaudit.tracker import warn_if_tracker_stale, STALE_SENTINEL_DIRS

REL = "in_game/common/buildings/b.txt"


def _game(tmp_path, text):
    game = tmp_path / "game"
    (game / "in_game/common/buildings").mkdir(parents=True)
    (game / REL).write_text(text)
    return game


def test_sentinels_cover_script_folders():
    for d in ("in_game/common", "main_menu/common", "loading_screen/common/defines"):
        assert d in STALE_SENTINEL_DIRS


def test_script_only_patch_is_detected(tmp_path, monkeypatch, capsys):
    tr = build_tracker(tmp_path, [("1.0", {REL: "b = {\n}\n"})])
    monkeypatch.setenv("PDX_GAME_ROOT", str(_game(tmp_path, "b = {\n\tcost = 2\n}\n")))
    monkeypatch.setattr(config, "_CACHE", {})
    warn_if_tracker_stale(tr.repo, tr.hashes["1.0"])
    err = capsys.readouterr().err
    assert "OUT OF DATE" in err and "pdx-audit --snapshot" in err


def test_matching_install_is_quiet(tmp_path, monkeypatch, capsys):
    tr = build_tracker(tmp_path, [("1.0", {REL: "b = {\n}\n"})])
    monkeypatch.setenv("PDX_GAME_ROOT", str(_game(tmp_path, "b = {\n}\n")))
    monkeypatch.setattr(config, "_CACHE", {})
    warn_if_tracker_stale(tr.repo, tr.hashes["1.0"])
    assert capsys.readouterr().err == ""
