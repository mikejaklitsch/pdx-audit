"""The desktop app window, driven offscreen: findings are listed by folder or
file, the audit chips filter the list, a finding shows its block with vanilla's
changes marked in, duplicates offer no dismiss, the Run menu offers the mod's
own categories and blocks, and slow reads never hold up the window. Skipped when
PySide6 is not installed."""
import json
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from pdxaudit import results  # noqa: E402
from pdxaudit.report import Finding  # noqa: E402


def _settle(win, timeout=10):
    """Let the work the window runs off the UI thread finish and land."""
    app = QtWidgets.QApplication.instance()
    deadline = time.monotonic() + timeout
    while win.background_jobs and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert not win.background_jobs


@pytest.fixture
def window(world, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    from pdxaudit.app import MainWindow
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = MainWindow(world.mod, world.repo, {}, autorun=False)
    _settle(win)
    yield win
    _settle(win)
    win.close()
    app.processEvents()


BLOCK = {"type": "REPLACE", "vanilla_file": "in_game/common/building_types/b.txt", "diff": "",
         "n_add": 1, "n_rem": 0, "missing": ["upkeep = 5"], "kept": [], "overlap": [], "lines": [],
         "patch": [{"t": "REPLACE:some_building = {", "c": "context"},
                   {"t": "\tcost = 100", "c": "context"},
                   {"t": "\tupkeep = 5", "c": "add"},
                   {"t": "}", "c": "context"}],
         "absent": [], "removed_note": []}


def _findings(since="1.1.0"):
    key = {"target": "override:in_game/common/building_types/some_building", "path": [],
           "slot": "upkeep", "op": "=", "old": None, "new": "5", "mod": None,
           "ops": [None, "=", None]}
    return [
        Finding("override_replace_new_line", "some_building",
                "in_game/common/building_types/m.txt:1", "upkeep = 5", BLOCK, key, since, "1.0.0"),
        Finding("dupes_multiple_sources", "dup_thing", "in_game/common/buildings/dup1.txt:1",
                "definition at in_game/common/buildings/dup1.txt:1; "
                "definition at in_game/common/buildings/dup2.txt:1",
                {"sources": [
                    {"how": "definition", "file": "in_game/common/buildings/dup1.txt", "line": 1},
                    {"how": "definition", "file": "in_game/common/buildings/dup2.txt", "line": 1}]},
                {"target": "dupes:common/buildings/dup_thing"}, "1.1.0"),
    ]


def _payload(findings):
    return results.build_payload(findings, mod_name="testmod", old_msg="1.0.0 Test",
                                 new_msg="1.1.0 Test", new_tag="1.1.0",
                                 selected=["overrides", "deps", "gui", "loc", "dupes"])


def test_lists_findings_and_shows_the_block(window):
    window.show_results(_payload(_findings()))
    records = {r["name"]: r for r in window.listed_records()}
    assert set(records) == {"some_building", "dup_thing"}

    window.select_record(records["some_building"])
    text = window.detail_text()
    assert "(missing)" in text
    assert "vanilla changed it in 1.1.0" in text and "your copy matches 1.0.0" in text
    signs = [(r["sign"], r["text"].strip()) for r in window.block_view.rows if r.get("sign")]
    assert signs == [("+", "upkeep = 5")]
    assert not any(r.get("note") for r in window.block_view.rows)
    assert window.legend_marks() == ["stale"]
    assert window.dismiss_button.isEnabled()

    window.select_record(records["dup_thing"])
    assert not window.dismiss_button.isEnabled()
    assert "cannot be dismissed" in window.detail_text()


def test_each_duplicate_definition_opens_to_show_its_block(window):
    window.show_results(_payload(_findings()))
    dupe = next(r for r in window.listed_records() if r["name"] == "dup_thing")
    window.select_record(dupe)
    view = window.block_view
    assert view.visible_texts() == ["definition · in_game/common/buildings/dup1.txt:1",
                                    "definition · in_game/common/buildings/dup2.txt:1"]
    view.toggle(1)
    assert view.visible_texts() == ["definition · in_game/common/buildings/dup1.txt:1",
                                    "definition · in_game/common/buildings/dup2.txt:1",
                                    "dup_thing = {", "\tcost = 2", "}"]
    view.toggle(1)
    assert "\tcost = 2" not in view.visible_texts()


def test_a_gui_finding_shows_its_definition_with_indentation(window):
    gui = Finding("gui_shadow_stale", "foo", "in_game/gui/aaa_mod.gui:1", "1 missing, 1 removed lines kept",
                  {"patch": [{"t": "template foo = {", "c": "context"},
                             {"t": "\tsize = { 10 10 }", "c": "changed", "e": [[10, 15]]},
                             {"t": "\tsize = { 20 20 }", "c": "new", "e": [[10, 15]]},
                             {"t": "}", "c": "context"}],
                   "vanilla_file": "in_game/gui/vanilla.gui"},
                  {"target": "gui:in_game/template/foo", "gap": "x"}, "1.1.0", "1.0.0")
    window.show_results(_payload(_findings() + [gui]))
    window.select_record(next(r for r in window.listed_records() if r["name"] == "foo"))
    view = window.block_view
    assert view.visible_texts() == ["template foo = {", "\tsize = { 10 10 }", "\tsize = { 20 20 }", "}"]
    assert [(r["sign"], r.get("emph")) for r in view.rows if r["sign"]] == [("-", [(10, 15)]), ("+", [(10, 15)])]
    assert not any(r["note"] for r in view.rows)
    assert window.legend_marks() == ["stale"]


def test_a_note_says_when_the_mod_changed_since_the_run(window, world):
    payload = _payload(_findings())
    payload["files"] = results.mod_fingerprint(world.mod)
    window.show_results(payload)
    window.check_for_changes()
    _settle(window)
    assert not window.changed_label.isVisibleTo(window)
    (world.mod / "in_game/common/buildings/dup2.txt").write_text("dup_thing = {\n\tcost = 22\n}\n")
    window.check_for_changes()
    _settle(window)
    assert window.changed_label.isVisibleTo(window)
    assert "1 file" in window.changed_label.text()


def test_a_finished_run_shows_its_findings_before_the_slow_reads_finish(window, monkeypatch):
    release = threading.Event()

    def held(fn):
        def wait_then_call(*args):
            release.wait(10)
            return fn(*args)
        return wait_then_call
    monkeypatch.setattr(results, "store_views", held(results.store_views))
    monkeypatch.setattr(results, "changed_files", held(results.changed_files))
    window.store.results_path.parent.mkdir(parents=True, exist_ok=True)
    window.store.results_path.write_text(json.dumps(_payload(_findings())), encoding="utf-8")

    window._run_done(0)   # returns while the record and the mod's files are still being read
    assert {r["name"] for r in window.listed_records()} == {"some_building", "dup_thing"}
    assert window.background_jobs == 2
    release.set()
    _settle(window)
    assert not window.changed_label.isVisibleTo(window)


def test_block_lines_are_syntax_coloured(window):
    window.show_results(_payload(_findings()))
    window.select_record(next(r for r in window.listed_records() if r["name"] == "some_building"))
    cost = next(r for r in window.block_view.rows if "cost" in r["text"])
    colours = {colour for _text, colour, _bold, _italic in cost["spans"] if colour}
    assert "#b5cea8" in colours          # the number, in the theme's numeric colour


def test_audit_chips_hide_and_show_findings(window):
    window.show_results(_payload(_findings()))
    window.set_audit_visible("dupes", False)
    assert {r["name"] for r in window.listed_records()} == {"some_building"}
    window.set_audit_visible("dupes", True)
    assert "dup_thing" in {r["name"] for r in window.listed_records()}


def test_folders_and_files_views(window):
    window.show_results(_payload(_findings()))
    assert "in_game" in window.tree_titles()
    window.set_list_mode("files")
    assert "in_game/common/building_types/m.txt" in window.tree_titles()
    assert {r["name"] for r in window.listed_records()} == {"some_building", "dup_thing"}


def test_earlier_findings_get_their_own_section(window):
    window.show_results(_payload(_findings(since="1.0.0")))
    assert "Still open from earlier patches" in window.section_titles()


def test_run_menu_offers_the_mods_categories_and_blocks(window):
    assert "common/building_types" in window.category_choices()
    window.set_run_choice(category="common/building_types")
    assert window.block_choices() == ["some_building"]
    window.set_run_choice(category="common/building_types", block="some_building")
    argv = results.run_argv(window.run_options())
    assert argv[:2] == ["--overrides", "--dupes"] and "some_building" in argv


def test_the_window_opens_before_the_mods_overrides_are_read(world, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    from pdxaudit.app import MainWindow
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    release = threading.Event()
    scan = results.override_targets
    monkeypatch.setattr(results, "override_targets", lambda mod_root: release.wait(10) and scan(mod_root))

    win = MainWindow(world.mod, world.repo,
                     {"category": "common/building_types", "block": "some_building"}, autorun=False)
    assert win.category_choices() == []          # built while the mod's scripts are still being read
    release.set()
    _settle(win)
    options = win.run_options()
    assert options["category"] == "common/building_types" and options["block"] == "some_building"
    win.close()
    app.processEvents()
