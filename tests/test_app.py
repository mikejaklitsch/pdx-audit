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

from pdxaudit import ledger, results  # noqa: E402
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
    from pdxaudit import app as appmod
    from pdxaudit.app import MainWindow
    ini = str(tmp_path / "settings.ini")
    monkeypatch.setattr(appmod, "_settings", lambda: appmod.QSettings(ini, appmod.QSettings.Format.IniFormat))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = MainWindow(world.mod, world.repo, {}, autorun=False)
    _settle(win)
    yield win
    _settle(win)
    win.close()
    app.processEvents()


def _change(finding, **kw):
    return dict({"kind": "vanilla_added", "mark": "stale", "fid": ledger.finding_id(finding),
                 "since": finding.since, "first": None, "last": None, "cols": None, "anchor": None,
                 "inside": False, "vanilla": None}, **kw)


def _findings(since="1.1.0"):
    key = {"target": "override:in_game/common/building_types/some_building", "path": [],
           "yours": None, "vanilla": "upkeep = 5"}
    added = Finding("override_vanilla_added_high", "some_building",
                    "in_game/common/building_types/m.txt:2", "", None, key, since, "1.0.0")
    block = {"type": "REPLACE", "file": "in_game/common/building_types/m.txt", "line": 1,
             "vanilla_file": "in_game/common/building_types/b.txt",
             "lines": ["REPLACE:some_building = {", "\tcost = 100", "}"],
             "changes": [_change(added, anchor=2, vanilla="upkeep = 5")]}
    return [
        added._replace(data=block),
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
    _settle(window)
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


def test_each_duplicate_definition_shows_its_block_open_unless_expanding_is_off(window):
    window.show_results(_payload(_findings()))
    dupe = next(r for r in window.listed_records() if r["name"] == "dup_thing")
    window.select_record(dupe)
    view = window.block_view
    heads = ["definition · in_game/common/buildings/dup1.txt:1",
             "definition · in_game/common/buildings/dup2.txt:1"]
    both = [heads[0], "dup_thing = {", "\tcost = 1", "}", heads[1], "dup_thing = {", "\tcost = 2", "}"]
    assert window.expand_sources.isChecked() and view.visible_texts() == both
    view.toggle(1)
    assert "\tcost = 2" not in view.visible_texts()
    window.expand_sources.setChecked(False)
    assert window.block_view.visible_texts() == heads
    window.expand_sources.setChecked(True)
    assert window.block_view.visible_texts() == both


def test_a_gui_finding_shows_its_definition_with_indentation(window):
    gui = Finding("gui_vanilla_changed_high", "foo", "in_game/gui/aaa_mod.gui:2", "foo > size", None,
                  {"target": "gui:in_game/template/foo", "path": ["foo", "size"],
                   "yours": "size = { 10 10 }", "vanilla": "size = { 20 20 }"}, "1.1.0", "1.0.0")
    gui = gui._replace(data={"type": None, "file": "in_game/gui/aaa_mod.gui", "line": 1,
                             "vanilla_file": "in_game/gui/vanilla.gui",
                             "lines": ["template foo = {", "\tsize = { 10 10 }", "}"],
                             "changes": [_change(gui, kind="vanilla_changed", first=2, last=2, cols=[1, 17],
                                                 vanilla="size = { 20 20 }")]})
    window.show_results(_payload(_findings() + [gui]))
    window.select_record(next(r for r in window.listed_records() if r["name"] == "foo"))
    _settle(window)
    view = window.block_view
    assert view.visible_texts() == ["template foo = {", "\tsize = { 10 10 }", "\tsize = { 20 20 }", "}"]
    assert [(r["sign"], r.get("emph")) for r in view.rows if r["sign"]] == [("-", [(10, 15)]), ("+", [(10, 15)])]
    assert len(view.selected_runs()) == 1               # one outline around both of the finding's rows
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
    assert window.background_jobs == 3      # the record, the mod's files, and the first finding's block view
    release.set()
    _settle(window)
    assert not window.changed_label.isVisibleTo(window)


def test_block_lines_are_syntax_coloured(window):
    window.show_results(_payload(_findings()))
    window.select_record(next(r for r in window.listed_records() if r["name"] == "some_building"))
    _settle(window)
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


def test_the_run_menu_lists_the_mods_gui_names_as_blocks(window):
    assert {"foo", "bar"} <= set(window.block_choices())


def test_an_empty_list_says_when_chips_hide_every_finding(window):
    window.show_results(_payload(_findings()))
    for tag in window.chips:
        window.set_audit_visible(tag, False)
    assert "hidden by the audit chips" in window.empty.text()
    for tag in window.chips:
        window.set_audit_visible(tag, True)
    assert window.empty.text() == "Select a finding to see it here."


def test_each_sources_action_writes_what_its_command_line_option_writes(window, world, tmp_path, monkeypatch):
    import io, sys
    from contextlib import redirect_stderr, redirect_stdout
    from pdxaudit.cli import main
    from test_cli_sources import _stored
    from test_sources import folder_source, git_source

    found = git_source(tmp_path / "found", [("1.0", {}), ("1.1", {})])
    other = git_source(tmp_path / "other", [("3.0", {})], mod_id="other")
    fold = folder_source(tmp_path / "fold")

    def cli(*argv):
        monkeypatch.setattr(sys, "argv", ["pdx-audit", "--mod-root", str(world.mod), "--vanilla-repo", world.repo,
                                          "--color", "never", *argv])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                main()
            except SystemExit as e:
                assert not e.code, argv

    steps = [
        (["--add-source", str(found), "--as", "foundation"], lambda: window.add_source_folder(found, "foundation")),
        (["--add-source", str(fold), "--as", "foundation"], lambda: window.add_source_folder(fold, "foundation")),
        (["--add-source", str(other), "--as", "adopted"], lambda: window.add_source_folder(other, "adopted")),
        (["--move-source", "fold", "1"], lambda: window.move_foundations(["fold", "found"])),
        (["--set-kind", "found", "folder"], lambda: window.set_source_kind("found", "folder")),
        (["--set-kind", "found", "auto"], lambda: window.set_source_kind("found", "auto")),
        (["--rename", "other", "old_", "new_"], lambda: window.add_rename("other", "old_", "new_")),
        (["--unrename", "other", "old_"], lambda: window.remove_rename("other", "old_")),
        (["--patch", "found", "1.0..1.1", "1.0.0"], lambda: window.assign_patch("found", "1.0..1.1", "1.0.0")),
        (["--ignore-suggestion", "dep"], lambda: window.ignore_suggestion("dep")),
        (["--remove-source", "other"], lambda: window.remove_source("other")),
    ]
    by_cli, by_app = tmp_path / "by-cli", tmp_path / "by-app"
    for argv, action in steps:
        monkeypatch.setenv("XDG_DATA_HOME", str(by_cli))
        cli(*argv)
        monkeypatch.setenv("XDG_DATA_HOME", str(by_app))
        assert action() is not False, argv
        _settle(window)
        assert _stored(by_cli / "pdx-audit") == _stored(by_app / "pdx-audit"), argv


def test_a_change_made_on_the_command_line_shows_in_the_open_window(window, world, tmp_path):
    from pdxaudit import sources
    from test_sources import git_source
    assert window.foundation_tree.topLevelItemCount() == 0 and not window.base_combo.isVisibleTo(window.run_popup)
    sources.add_source(world.mod, git_source(tmp_path / "found", [("1.0", {})]), "foundation",
                       vanilla_repo=world.repo)
    window.refresh_store_views()                 # what activating the window does
    _settle(window)
    assert window.foundation_tree.ids() == ["found"]
    assert window.base_combo.isVisibleTo(window.run_popup)
    window.set_run_choice(base="vanilla")
    assert "--vanilla-only" in results.run_argv(window.run_options())
    window.select_source("found")
    assert [window.version_tree.topLevelItem(i).text(1) for i in range(window.version_tree.topLevelItemCount())] \
        == ["1.1.0"]


def test_a_block_with_vanillas_text_shows_side_by_side_until_switched_off(window):
    finding, = [f for f in _findings() if f.name == "some_building"]
    block = dict(finding.data, vanilla_lines=["some_building = {", "\tcost = 100", "\tupkeep = 5", "}"],
                 vanilla_tag="1.1.0", base_lines=["some_building = {", "\tcost = 100", "}"], base_tag="1.0.0",
                 changes=[dict(finding.data["changes"][0], vfirst=3, vlast=3)])
    window.show_results(_payload([finding._replace(data=block)]))
    window.select_record(window.listed_records()[0])
    _settle(window)
    view = window.block_view
    assert window.side_by_side.isChecked()
    assert view.visible_texts() == [("some_building = {", "REPLACE:some_building = {"),
                                    ("\tcost = 100", "\tcost = 100"), ("\tupkeep = 5", None), ("}", "}")]
    assert window.diff_labels["add"].isVisibleTo(window) and not window.diff_labels["del"].isVisibleTo(window)
    assert "added since 1.0.0" in window.diff_labels["add"].text()
    window.flatten.setChecked(True)
    _settle(window)
    assert view.visible_texts()[1:3] == [("cost = 100", "cost = 100"), ("upkeep = 5", None)]
    window.flatten.setChecked(False)
    assert window.legend_marks() == ["stale"]
    window.side_by_side.setChecked(False)
    _settle(window)
    assert view.visible_texts() == ["REPLACE:some_building = {", "\tcost = 100", "\tupkeep = 5", "}"]


def test_a_block_is_built_off_the_ui_thread_once_and_wraps_to_the_view(window):
    finding, dupe = _findings()
    long_line = dict(finding.data, lines=["REPLACE:some_building = {", "\tcost = 100 # " + "x" * 400, "}"])
    window.show_results(_payload([finding._replace(data=long_line), dupe]))
    rec = next(r for r in window.listed_records() if r["name"] == "some_building")
    window.select_record(rec)
    assert window.background_jobs >= 1 and not window.block_view.rows   # built on a worker thread
    _settle(window)
    assert window.block_view.rows
    window.select_record(next(r for r in window.listed_records() if r["name"] == "dup_thing"))
    window.select_record(rec)
    assert window.background_jobs == 0 and window.block_view.rows              # kept once built
    tallest = lambda: max(h for _y, h, row in window.block_view.items if "text" in row)
    assert tallest() == window.block_view.LINE
    window.wrap_lines.setChecked(True)
    assert tallest() > window.block_view.LINE


def test_a_foundations_change_carries_a_layer_mark(window):
    finding, = [f for f in _findings() if f.name == "some_building"]
    block = dict(finding.data, changes=[dict(finding.data["changes"][0], layer="found")])
    window.show_results(_payload([finding._replace(data=block)]))
    window.select_record(window.listed_records()[0])
    _settle(window)
    assert any(r.get("layer") == "found" for r in window.block_view.rows)
    assert window.layer_legend.isVisibleTo(window)


def test_the_window_opens_before_the_mods_overrides_are_read(world, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    from pdxaudit import app as appmod
    from pdxaudit.app import MainWindow
    ini = str(tmp_path / "settings.ini")
    monkeypatch.setattr(appmod, "_settings", lambda: appmod.QSettings(ini, appmod.QSettings.Format.IniFormat))
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
