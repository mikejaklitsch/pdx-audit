"""The Merge page of the app: plan the merge, decide single decisions or a whole file
with Take vanilla and Keep mine, and apply a file once nothing in it is open. The
tests run the merge command in this process, not in a background process."""
import json
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
QtCore = pytest.importorskip("PySide6.QtCore")

from pdxaudit import merge_cli  # noqa: E402

M_TXT = "in_game/common/building_types/m.txt"


def _settle(win, timeout=10):
    app = QtWidgets.QApplication.instance()
    deadline = time.monotonic() + timeout
    while win.background_jobs and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def _open(mod, repo, tmp_path, monkeypatch):
    """The app's window on `mod`, with the merge command run in this process."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    from pdxaudit import app as appmod
    from pdxaudit.app import MainWindow
    ini = str(tmp_path / "settings.ini")
    monkeypatch.setattr(appmod, "_settings", lambda: appmod.QSettings(ini, appmod.QSettings.Format.IniFormat))
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = MainWindow(mod, repo, {}, autorun=False)
    _settle(win)
    calls = []

    def start(argv, on_done, text, command=None):
        calls.append((command, argv))
        code = merge_cli.main(["--mod-root", str(mod), "--vanilla-repo", repo, *argv])
        on_done(code)
    monkeypatch.setattr(win, "_start", start)
    win.merge_calls = calls
    return win


def _close(win):
    _settle(win)
    win.close()
    QtWidgets.QApplication.instance().processEvents()


@pytest.fixture
def window(world, tmp_path, monkeypatch):
    win = _open(world.mod, world.repo, tmp_path, monkeypatch)
    yield win
    _close(win)


def test_the_versions_default_to_the_last_patch(window):
    page = window.merge_page
    assert (page.old_tag(), page.new_tag()) == ("1.0.0", "1.1.0")


def test_a_plan_lists_each_file_and_a_file_with_nothing_open_is_ready(window):
    page = window.merge_page
    page.plan_all()
    assert all(command == "merge" for command, _argv in window.merge_calls)
    assert M_TXT in page.file_names()
    f = page.file(M_TXT)
    assert f["open"] == 0 and page.ready(M_TXT)
    page.select_file(M_TXT)
    assert page.apply_button.isEnabled()
    assert "| \tupkeep = 5" in _view(page)


def test_each_row_offers_the_buttons_for_its_state(window):
    page = window.merge_page
    page.plan_all()
    page.select_file(M_TXT)
    rows = page.row_buttons()
    assert rows
    for d, labels in rows:
        if d["action"] == "take":
            assert "Keep mine" in labels and "Take vanilla" not in labels
        elif d["action"] == "open":
            assert {"Take vanilla", "Keep mine"} <= set(labels)


def test_keep_mine_on_a_change_the_merge_takes_updates_the_file_without_a_command(window):
    page = window.merge_page
    page.plan_all()
    page.select_file(M_TXT)
    d = next(d for d in page.decisions() if d["kind"] == "vanilla_added")
    rows = page.row_widgets()
    calls = len(window.merge_calls)
    page.choose(d, "keep")
    # The row shows the choice at once; the file follows when the build ends.
    row = next(labels for rd, labels in page.row_buttons() if rd is d)
    assert "Keep mine" not in row and "Take vanilla" in row       # Keep mine shows ✓ and is off
    assert any("Your choice" in t for t in page.row_texts())
    _settle(window)
    assert len(window.merge_calls) == calls                       # no command ran
    assert "upkeep" not in page.file(M_TXT)["merged"]
    d = next(d for d in page.decisions() if d["kind"] == "vanilla_added")
    assert (d["action"], d["by"]) == ("keep", "choice")
    assert page.row_widgets() == rows                  # the rows stay; only their state changes
    outcome = [line.split(" | ", 1)[1] for line in _view(page).splitlines() if " | " in line]
    assert "\tupkeep = 5" not in outcome                 # the right side: the file after Apply file
    page.reset_choice(d)
    _settle(window)
    assert "upkeep = 5" in page.file(M_TXT)["merged"]


def test_a_choice_on_the_page_gives_the_file_of_a_dry_run_with_it(window, world, tmp_path):
    """The page and `merge --dry-run --choices` give one file for each choice."""
    page = window.merge_page
    page.plan_all()
    page.select_file(M_TXT)
    for d in page.decisions():
        page.choose(d, "keep" if d["action"] == "take" else "take")
    _settle(window)
    path = page._choices_file()
    plan = tmp_path / "with-choices.json"
    assert merge_cli.main(["--mod-root", str(world.mod), "--vanilla-repo", world.repo, *page._versions(),
                           "--dry-run", "--quiet", "--plan-out", str(plan), "--choices", str(path)]) == 0
    page._drop(path)
    [f] = [f for f in json.loads(plan.read_text(encoding="utf-8"))["files"] if f["file"] == M_TXT]
    mine = page.file(M_TXT)
    for key in ("merged", "decisions", "open", "removed_check", "stale_entries", "diff", "choices"):
        assert mine[key] == f[key]


def test_an_open_row_names_its_cause(window, world, monkeypatch):
    """With merge_default ask, a change that only vanilla made is an open row marked No
    Conflict, and a change that you and vanilla both made is a Merge Conflict."""
    monkeypatch.setenv("PDX_MERGE_DEFAULT", "ask")
    (world.mod / M_TXT).write_text("REPLACE:some_building = {\n\tcost = 100\n\tlegacy_mod = 2\n}\n",
                                   encoding="utf-8")
    page = window.merge_page
    page.plan_all()
    page.select_file(M_TXT)
    texts = page.row_texts()
    assert any("No Conflict" in t for t in texts) and any("Merge Conflict" in t for t in texts)
    assert "Merge Conflict" in page.file_note.text() and "No Conflict" in page.file_note.text()
    text, colour = page._item_text(page.file(M_TXT))
    assert text.endswith("2 vanilla changes · 1 Merge Conflict, 1 No Conflict")
    assert colour == page.C["broken"]


def test_a_copy_that_vanilla_changed_shows_its_change_in_the_file_list(window, monkeypatch):
    """Your file is a copy of the old vanilla file. With merge_default ask, its file
    item gives the vanilla changes and the No Conflict rows, in the No Conflict colour."""
    monkeypatch.setenv("PDX_MERGE_DEFAULT", "ask")
    page = window.merge_page
    page.plan_all()
    f = page.file(M_TXT)
    assert f["open"] == 2 and {d["cause"] for d in f["decisions"]} == {"clean"}
    text, colour = page._item_text(f)
    assert text.endswith("2 vanilla changes · 2 No Conflict") and colour == page.C["review"]


def test_take_vanilla_for_all_and_keep_mine_for_all_choose_each_row(window, world):
    (world.mod / M_TXT).write_text("REPLACE:some_building = {\n\tcost = 100\n\tlegacy_mod = 2\n}\n",
                                   encoding="utf-8")
    page = window.merge_page
    page.plan_all()
    page.select_file(M_TXT)
    assert page.file(M_TXT)["open"] == 1
    page.choose_all("keep")
    _settle(window)
    f = page.file(M_TXT)
    assert f["merged"] == f["gathered"]["ours"] and f["open"] == 0
    assert all(d["action"] == "keep" for d in page.decisions())
    assert page.apply_button.isEnabled() and page.clear_button.isEnabled()
    page.choose_all("take")
    _settle(window)
    assert page.file(M_TXT)["open"] == 0
    assert all(d["action"] == "take" for d in page.decisions())
    # A choice on a row after the file's choice stays.
    d = page.decisions()[0]
    page.choose(d, "keep")
    _settle(window)
    assert page.decisions()[0]["action"] == "keep"
    assert all(d["action"] == "take" for d in page.decisions()[1:])
    page.clear_choices()
    _settle(window)
    assert page.file(M_TXT)["open"] == 1 and not page.clear_button.isEnabled()


def test_decisions_for_you_keeps_a_row_you_change_and_says_when_none_is_left(window, world):
    """A row that the user changes stays until the filter changes. When no decision is
    left, the rows area says so, the block view stays empty, and Show all rows shows
    the rows again. The app starts with All rows."""
    (world.mod / M_TXT).write_text("REPLACE:some_building = {\n\tcost = 100\n\tlegacy_mod = 2\n}\n",
                                   encoding="utf-8")
    page = window.merge_page
    assert page.row_combo.currentData() == "all"
    page.plan_all()
    page.select_file(M_TXT)
    shown = lambda: [d for d, row in page._rows if not row.isHidden()]     # noqa: E731
    page.row_combo.setCurrentIndex(page.row_combo.findData("open"))
    page._show_file(M_TXT)
    [d] = shown()
    assert d["action"] == "open" and page.empty_note.isHidden()
    assert "1 wait for decisions" in page.summary.text()
    page.choose(d, "keep")
    _settle(window)
    [d] = shown()
    assert d["action"] == "keep"                        # the row the user changed stays
    page.row_combo.activated.emit(page.row_combo.currentIndex())   # the user picks the filter again
    _settle(window)
    assert shown() == [] and not page.empty_note.isHidden()
    assert "All decisions are ready" in page.empty_label.text()
    assert page.view_text() == "" and page.view_title.text() == ""
    assert "0 wait for decisions" in page.summary.text()       # the summary follows the choice
    page._show_plan(keep=M_TXT)                         # the file keeps all of your text and stays listed
    assert M_TXT in page.file_names()
    page._show_all_rows()
    _settle(window)
    assert page.row_combo.currentData() == "all" and len(shown()) == len(page.decisions())
    assert page.empty_note.isHidden()


def test_apply_file_writes_it_and_takes_it_off_the_list(window, world):
    page = window.merge_page
    page.plan_all()
    page.select_file(M_TXT)
    page.apply_file(M_TXT)
    text = (world.mod / M_TXT).read_text(encoding="utf-8-sig")
    assert "upkeep = 5" in text and "legacy_mod" not in text
    assert M_TXT not in page.file_names()


def test_apply_all_writes_only_the_ready_files(window, world):
    (world.mod / M_TXT).write_text("REPLACE:some_building = {\n\tcost = 100\n\tlegacy_mod = 2\n}\n",
                                   encoding="utf-8")
    page = window.merge_page
    page.plan_all()
    before = (world.mod / M_TXT).read_text(encoding="utf-8")
    page.apply_ready()
    assert (world.mod / M_TXT).read_text(encoding="utf-8") == before
    assert M_TXT in page.file_names()


def test_the_page_keeps_nothing_on_disk(window, world, tmp_path):
    import tempfile
    before = set(os.listdir(tempfile.gettempdir()))
    page = window.merge_page
    page.plan_all()
    page.select_file(M_TXT)
    d = next(d for d in page.decisions() if d["kind"] == "vanilla_added")
    page.choose(d, "keep")
    _settle(window)
    page.apply_file(M_TXT)
    left = {n for n in set(os.listdir(tempfile.gettempdir())) - before if n.startswith("pdx-audit-")}
    assert not left
    store = window.store.dir
    assert not (store / "merge-plans").exists() and not (store / "merge-choices.json").exists()


def test_search_filter_and_sort_the_file_list(window, world):
    page = window.merge_page
    page.plan_all()
    assert M_TXT in page.file_names()
    page.search.setText("no_such_file")
    assert page.file_names() == []
    page.search.setText("building_types")
    assert page.file_names() == [M_TXT]
    page.search.setText("")
    page.show_combo.setCurrentIndex(page.show_combo.findData("decide"))
    page._remember("merge_show", page.show_combo)
    assert M_TXT not in page.file_names()                # m.txt is ready, not waiting for a decision
    page.show_combo.setCurrentIndex(page.show_combo.findData("all"))
    page.sort_combo.setCurrentIndex(page.sort_combo.findData("name"))
    page._remember("merge_sort", page.sort_combo)
    assert page.file_names() == sorted(page.file_names())


def test_the_file_list_shows_names_paths_or_folders(window):
    """The list shows each file's name, and its path on hover. Full paths shows the
    paths; Folder tree puts the files in folders that a click closes."""
    page = window.merge_page
    page.plan_all()
    page.select_file(M_TXT)
    [item] = [i for i in page._file_items() if i.data(0, QtCore.Qt.ItemDataRole.UserRole) == M_TXT]
    assert item.text(0).splitlines()[0] == "m.txt" and item.toolTip(0) == M_TXT
    page.paths_box.setChecked(True)
    [item] = [i for i in page._file_items() if i.data(0, QtCore.Qt.ItemDataRole.UserRole) == M_TXT]
    assert item.text(0).splitlines()[0] == M_TXT
    names = page.file_names()
    page.tree_box.setChecked(True)
    assert sorted(page.file_names()) == sorted(names) and not page.paths_box.isEnabled()
    [item] = [i for i in page._file_items() if i.data(0, QtCore.Qt.ItemDataRole.UserRole) == M_TXT]
    assert item.text(0).splitlines()[0] == "m.txt"
    folder = item.parent()
    assert folder is not None and M_TXT.startswith(folder.data(0, QtCore.Qt.ItemDataRole.UserRole + 1))
    page._toggle_folder(folder)
    assert not folder.isExpanded() and page.current == M_TXT
    page._show_plan(keep=page.current)                  # the folder stays closed
    [item] = [i for i in page._file_items() if i.data(0, QtCore.Qt.ItemDataRole.UserRole) == M_TXT]
    assert not item.parent().isExpanded()
    page.tree_box.setChecked(False)
    page.paths_box.setChecked(False)

def test_a_blocked_button_says_why(window):
    window._set_busy(True, "Planning the merge…")
    assert not window.run_button.isEnabled()
    assert "Planning the merge" in window.run_button.toolTip()
    assert "Planning the merge" in window.merge_page.plan_button.toolTip()
    window._set_busy(False)
    assert window.run_button.isEnabled() and window.run_button.toolTip() == ""


def test_flat_code_is_laid_out_one_statement_a_line():
    from pdxaudit.merge_view import layout
    flat = 'horse_maintenance { icon = horses goods = 0.1 size { 2 2 } limit { OR { a = yes b > 1 } } }'
    assert layout(flat) == ("horse_maintenance {\n    icon = horses\n    goods = 0.1\n    size { 2 2 }\n"
                            "    limit {\n        OR {\n            a = yes\n            b > 1\n        }\n"
                            "    }\n}")
    assert layout('text = "a b { c"') == 'text = "a b { c"'


def test_the_page_explains_itself_and_rows_say_what_apply_does(window):
    page = window.merge_page
    assert "Plan merge" in page.help_text() and "Apply file" in page.help_text()
    page.plan_all()
    page.select_file(M_TXT)
    texts = page.row_texts()
    assert any("Vanilla added this. Your file does not have it." in t for t in texts)
    assert any("Default" in t for t in texts)


def _view(page):
    _settle(page.win)
    assert not page.view_pending
    return page.view_text()


def test_the_chosen_action_shows_a_check_and_cannot_be_clicked(window):
    page = window.merge_page
    page.plan_all()
    page.select_file(M_TXT)
    for d, row in zip(page.decisions(), page.row_widgets()):
        buttons = {b.text(): b for b in row.findChildren(QtWidgets.QPushButton)}
        if d["action"] == "take":
            assert not buttons["✓ Take vanilla"].isEnabled() and buttons["Keep mine"].isEnabled()


def test_a_click_on_a_row_shows_its_full_block_below(window):
    page = window.merge_page
    page.plan_all()
    page.select_file(M_TXT)
    d = next(d for d in page.decisions() if d["kind"] == "vanilla_added")
    page.select_row(page.row_widgets()[page.decisions().index(d)])
    lines = _view(page).splitlines()
    assert lines[0] == "REPLACE:some_building = { | REPLACE:some_building = {" and lines[-1] == "} | }"
    assert any(line.endswith(" | \tupkeep = 5") for line in lines)
    selected = [row for _y, _h, row in page.block_view.items if row.get("fid") == "selected"]
    assert [r["left"]["text"] for r in selected] == ["\tupkeep = 5"]
    page.select_row(None)
    assert "\tupkeep = 5" in _view(page) and "Whole file" in page.view_title.text()


def test_a_quick_choice_changes_only_what_it_chose(window):
    """A build that ends at once does not show "updating", the view title stays, and
    the view shows its rows with their colours the first time it paints them."""
    page = window.merge_page
    page.plan_all()
    page.select_file(M_TXT)
    d = next(d for d in page.decisions() if d["kind"] == "vanilla_added")
    page.select_row(page.row_widgets()[page.decisions().index(d)])
    _view(page)
    hints, painted = [], []
    real_hint, real_rows = page.view_hint.setText, page.block_view.set_rows
    page.view_hint.setText = lambda text: (hints.append(text), real_hint(text))
    page.block_view.set_rows = lambda rows, *a: (
        painted.append([c for r in a[-1][1] for c in (r.get("left"), r.get("right")) if c]), real_rows(rows, *a))
    note = page.file_note.text()
    page.choose(d, "keep")
    assert "Updating" not in page.file_note.text()
    _settle(window)
    assert page.file_note.text() == note
    assert "Finding the block…" not in hints
    assert painted and painted[-1] and all(c.get("spans") for c in painted[-1])

def test_a_click_does_not_wait_for_the_block(window, monkeypatch):
    import threading
    from pdxaudit import worker
    gate = threading.Event()
    real = worker.call
    monkeypatch.setattr(worker, "call", lambda *a: (gate.wait(5), real(*a))[1])
    page = window.merge_page
    page.plan_all()
    page.select_file(M_TXT)
    page.select_row(page.row_widgets()[0])
    assert page.view_pending and page.view_hint.text() == "Finding the block…"
    gate.set()
    assert "some_building" in _view(page)


def test_block_of_shows_the_outermost_short_block_and_marks_the_node():
    from pdxaudit.merge_view import block_of
    ours = "a = 1\nthing = {\n\tinner = {\n\t\tx = 1\n\t}\n\ty = 2\n}\nb = 2\n"
    merged = ours.replace("x = 1", "x = 5")
    rows, selected = block_of({"line": 4, "copy": "thing"}, ours, merged, False)
    assert rows[0].text == "thing = {" and rows[-1].text == "}" and (rows[0].mine, rows[-1].mine) == (2, 7)
    assert sorted((rows[i].sign, rows[i].text) for i in selected) == [("+", "\t\tx = 5"), ("-", "\t\tx = 1")]


def test_a_node_with_no_line_is_found_by_its_address():
    from pdxaudit.merge_view import block_of
    ours = "REPLACE:thing = {\n\ty = 2\n}\n"
    merged = "REPLACE:thing = {\n\ty = 2\n\tz = 3\n}\n"
    d = {"copy": "thing", "path": [{"key": "z"}], "address": {"identity": "x/thing", "path": [{"key": "z"}]}}
    rows, selected = block_of(d, ours, merged, False)
    assert [(rows[i].sign, rows[i].theirs, rows[i].text) for i in selected] == [("+", 3, "\tz = 3")]


def test_a_node_that_no_file_holds_shows_at_its_place_in_a_long_block():
    """Vanilla added a node that the merge does not take yet, in a block too long to
    show whole. The view shows the lines around the node's place, and vanilla's text
    there as the selected row, not the start of the block."""
    from pdxaudit.merge_rows import SELECTED, block_of, option_rows
    ours = "REPLACE:thing = {\n" + "".join(f"\tv{i} = 1\n" for i in range(200)) + "}\n"
    d = {"kind": "vanilla_added", "action": "open", "copy": "thing", "at": 120, "theirs": "z = 3",
         "path": [{"key": "z"}], "address": {"identity": "x/thing", "path": [{"key": "z"}]}}
    rows, _selected = block_of(d, ours, ours, False)
    assert rows[0].mine <= 120 <= rows[-1].mine and rows[0].mine > 1
    f = {"gathered": {"ours": ours}, "decisions": [d]}
    sides = option_rows(f, rows, d)
    picked = [r for r in sides if r["fid"] == SELECTED]
    assert [r["right"]["text"] for r in picked] == ["\tz = 3"]

def test_a_new_block_shows_the_block_that_holds_it_at_its_place():
    """Vanilla added a whole block, and the merge takes it. The view shows the block of
    your file that holds its place, with the new block selected. A node of the same
    name elsewhere in the file does not move the view there."""
    from pdxaudit.merge_rows import SELECTED, block_of, option_rows
    ours = ("REPLACE:thing = {\n\tfirst = {\n\t\tz = { a = 1 }\n\t}\n\tsecond = {\n\t\tx = 1\n\t}\n}\n")
    merged = ours.replace("\t\tx = 1\n", "\t\tx = 1\n\t\tz = {\n\t\t\ta = 2\n\t\t}\n")
    new = "\n\t\tz = {\n\t\t\ta = 2\n\t\t}"
    at = ours.index("\t\tx = 1\n") + len("\t\tx = 1")
    d = {"kind": "vanilla_added", "action": "take", "copy": "thing", "at": 7, "span": [at, at], "vanilla": new,
         "path": [{"key": "z"}], "address": {"identity": "x/thing", "path": [{"key": "z"}]}}
    rows, _selected = block_of(d, ours, merged, False)
    assert [r.mine for r in rows if r.mine] == list(range(1, 9))          # the whole REPLACE block
    f = {"gathered": {"ours": ours}, "decisions": [d]}
    picked = [r["right"]["text"] for r in option_rows(f, rows, d) if r["fid"] == SELECTED]
    assert picked == ["\t\tz = {", "\t\t\ta = 2", "\t\t}"]

def test_options_show_your_line_then_vanillas_and_the_outcome_beside_them():
    from pdxaudit.merge_rows import diff_rows, option_rows
    ours = "a = {\n\tx = 1\n\tkeep = 1\n}\n"
    merged = "a = {\n\tx = 5\n\tkeep = 1\n\ty = 2\n}\n"
    take = {"kind": "vanilla_changed", "action": "take", "line": 2, "theirs": "x = 5"}
    add = {"kind": "vanilla_added", "action": "take", "at": 4, "theirs": "y = 2"}
    f = {"gathered": {"ours": ours}, "merged": merged, "decisions": [take, add]}
    rows = option_rows(f, diff_rows(ours, merged), take)
    got = [((r["right"] or {}).get("text"), (r["right"] or {}).get("state"),
            (r["left"] or {}).get("text"), (r["left"] or {}).get("state")) for r in rows]
    assert got == [("a = {", "same", "a = {", "same"),
                   ("\tx = 1", "del", None, None),             # your option
                   ("\tx = 5", "add", "\tx = 5", "add"),        # vanilla's option, and the outcome
                   ("\tkeep = 1", "same", "\tkeep = 1", "same"),
                   ("\ty = 2", "add", "\ty = 2", "add"),        # a node your file does not hold
                   ("}", "same", "}", "same")]
    assert [r["fid"] for r in rows] == [None, "selected", "selected", None, None, None]
    assert rows[1]["right"]["emph"] and rows[2]["right"]["emph"]   # the words that differ

def test_rows_follow_the_order_of_the_file(window):
    page = window.merge_page
    page.plan_all()
    page.select_file(M_TXT)
    places = [d.get("line") or d.get("at") for d in page.decisions()]
    assert all(places) and places == sorted(places)
    added = next(d for d in page.decisions() if d["kind"] == "vanilla_added")
    assert "line" not in added and "near line" in page.where(added)   # a node your file does not hold


def test_a_file_shows_its_rows_a_page_at_a_time(window):
    page = window.merge_page
    page.PAGE = 1
    page.plan_all()
    page.select_file(M_TXT)
    total = len(page.decisions())
    assert total > 1 and len(page.row_widgets()) == 1
    assert page.more_label.text() == f"1 of {total} rows. Scroll down to see more."
    page._add_more()
    assert len(page.row_widgets()) == 2


def test_a_new_line_elsewhere_is_not_part_of_the_selected_node():
    from pdxaudit.merge_view import block_of
    ours = "thing = {\n\ta = 1\n\tb = 2\n}\n"
    merged = "thing = {\n\tnew = 1\n\ta = 1\n}\n"
    rows, selected = block_of({"line": 3, "copy": "thing"}, ours, merged, False)
    assert [(rows[i].sign, rows[i].text) for i in selected] == [("-", "\tb = 2")]


def test_the_merge_worker_keeps_the_file_and_loads_it_again_when_it_lost_it(window):
    from pdxaudit import worker
    page = window.merge_page
    page.plan_all()
    page.select_file(M_TXT)
    _view(page)
    key = page._key(page.file(M_TXT))
    assert worker.call("merge", "pdxaudit.merge_rows:view", key, None)[0] == "file"
    worker.call("merge", "pdxaudit.merge_rows:forget", key[0])
    assert worker.call("merge", "pdxaudit.merge_rows:view", key, None) == "missing"
    page.select_row(None)
    assert "upkeep = 5" in _view(page)              # the page loaded the file again


def test_a_new_file_shows_only_its_own_rows(window, world):
    page = window.merge_page
    page.PAGE = 1
    page.plan_all()
    names = page.file_names()
    assert len(names) > 1
    first = next(n for n in names if len(page.file(n)["decisions"]) > 1)
    page.select_file(first)
    other = next(n for n in names if n != first)
    page.select_file(other)
    box = page.rows.widget()
    cards = [w for w in box.findChildren(QtWidgets.QFrame, "card")]
    assert cards == page.row_widgets()
    assert all(d in page.file(other)["decisions"] for d, _labels in page.row_buttons())
    layout = [page.row_box.itemAt(i).widget() for i in range(page.row_box.count())]
    assert layout.index(page.more_label) == len(cards)       # the more label comes after the rows


def test_vanillas_new_node_has_the_indent_of_its_place():
    from pdxaudit.merge_rows import diff_rows, option_rows
    ours = "a = {\n\tb = {\n\t\tx = 1\n\t}\n}\n"
    merged = "a = {\n\tb = {\n\t\tx = 1\n\t}\n\tc = 2\n}\n"
    f = {"gathered": {"ours": ours}, "merged": merged, "decisions": [{"kind": "vanilla_added", "at": 5, "theirs": "c = 2"}]}
    rows = option_rows(f, diff_rows(ours, merged))
    assert [((r["right"] or {}).get("text"), (r["left"] or {}).get("text")) for r in rows][4] == ("\tc = 2", "\tc = 2")


def test_a_value_inside_a_one_line_block_shows_vanillas_whole_line():
    from pdxaudit.merge_rows import diff_rows, option_rows
    ours = "x = {\n\tai_weight = { add = 10 }\n}\n"
    merged = "x = {\n\tai_weight = { add = 20 }\n}\n"
    at = ours.index("add = 10")
    d = {"kind": "vanilla_changed", "line": 2, "ours": "add = 10", "theirs": "add = 20",
         "span": [at, at + len("add = 10")], "vanilla": "add = 20"}
    f = {"gathered": {"ours": ours}, "merged": merged, "decisions": [d]}
    rows = option_rows(f, diff_rows(ours, merged), d)
    got = [((r["right"] or {}).get("text"), (r["left"] or {}).get("text")) for r in rows]
    assert got[1:3] == [("\tai_weight = { add = 10 }", None), ("\tai_weight = { add = 20 }", "\tai_weight = { add = 20 }")]


def test_vanillas_option_is_its_real_text_not_the_canonical_text():
    from pdxaudit.merge_rows import diff_rows, option_rows
    ours = "b = {\n\tx = 1\n}\n"
    merged = "b = {\n\tx = 1\n\tor = {\n\t\towner ?= { is_ai = no }\n\t}\n}\n"
    add = {"kind": "vanilla_added", "at": 3, "theirs": "OR { owner ?= { is_ai = no } }",
           "span": [len("b = {\n\tx = 1\n")] * 2, "vanilla": "\tor = {\n\t\towner ?= { is_ai = no }\n\t}\n"}
    f = {"gathered": {"ours": ours}, "merged": merged, "decisions": [add]}
    left = [r["right"]["text"] for r in option_rows(f, diff_rows(ours, merged)) if r["right"]]
    assert left == ["b = {", "\tx = 1", "\tor = {", "\t\towner ?= { is_ai = no }", "\t}", "}"]


def test_a_file_opens_with_its_first_row_selected(window):
    page = window.merge_page
    window.show()
    page.plan_all()
    page.select_file(M_TXT)
    assert page._selected is page.row_widgets()[0]
    assert page._selected.property("selected")
    _view(page)
    assert "Whole file" not in page.view_title.text()


def test_the_plan_holds_the_real_text_of_each_change(window):
    page = window.merge_page
    page.plan_all()
    d = next(d for d in page.file(M_TXT)["decisions"] if d["kind"] == "vanilla_added")
    assert d["vanilla"].strip() == "upkeep = 5" and d["span"][0] == d["span"][1]


def test_your_own_text_decides_each_row_of_its_block_until_you_remove_it(window):
    page = window.merge_page
    page.plan_all()
    page.select_file(M_TXT)
    rows = [d for d in page.decisions() if d.get("span") is not None]
    block = page.own_block(rows[0])
    assert page.write_own(rows[0], block["result"], block) == ""
    _settle(window)
    f = page.file(M_TXT)
    inside = [d for d in f["decisions"] if d.get("own") == block["span"]]
    assert inside and all(d["action"] == "take" for d in inside)
    covered = [labels for d, labels in page.row_buttons() if d.get("own")]
    assert covered and all("Take vanilla" not in x and "Keep mine" not in x and "Write my own" in x for x in covered)
    page.remove_own(inside[0])
    _settle(window)
    assert not any(d.get("own") for d in page.file(M_TXT)["decisions"])


def test_your_own_text_is_saved_and_a_new_session_reads_it_back(window):
    page = window.merge_page
    page.plan_all()
    page.select_file(M_TXT)
    d = next(d for d in page.decisions() if d["kind"] == "vanilla_added")
    block = page.own_block(d)
    assert block["result"].startswith("REPLACE:some_building = {") and "\tupkeep = 5" in block["result"]
    assert page.write_own(d, block["result"].replace("upkeep = 5", "upkeep = 9"), block) == ""
    _settle(window)
    assert "\tupkeep = 9" in page.file(M_TXT)["merged"]
    assert page.write_own(d, "REPLACE:some_building = {", block) == "your text leaves 1 block open"
    key = merge_cli.choice_key(d)
    i = next(k for k, x in enumerate(page.decisions()) if x.get("address") and merge_cli.choice_key(x) == key)
    assert any("✓ My own text" in b.text() for b in page.row_widgets()[i].findChildren(QtWidgets.QPushButton))
    saved = window.store.dir / merge_cli.SAVED_CHOICES
    assert saved.is_file()
    page.choices, page._versions_of_choices = {}, None        # as in a new session
    page.plan_all()
    assert "\tupkeep = 9" in page.file(M_TXT)["merged"]
    page.clear_all()
    _settle(window)
    assert not saved.exists() and "\tupkeep = 9" not in page.file(M_TXT)["merged"]




def test_a_choice_does_not_move_the_list(tmp_path, monkeypatch):
    """Keep mine, Take vanilla and the buttons for all keep the rows where they are."""
    from conftest import build_tracker, _write_tree
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    rel = "in_game/common/x/many.txt"
    block = "{p}B{i} = {{\n\tv = {v}\n\tw = {w}\n}}\n"
    old, new, mine = ("".join(block.format(p=p, i=i, v=v, w=w) for i in range(12))
                      for p, v, w in (("", 1, 1), ("", 2, 1), ("REPLACE:", 1, 3)))
    tr = build_tracker(tmp_path, [("1.0.0", {rel: old}), ("1.1.0", {rel: new})])
    mod = tmp_path / "mod"
    _write_tree(mod, {".metadata/metadata.json": '{"id": "t"}', rel: mine})
    win = _open(mod, tr.repo, tmp_path, monkeypatch)
    app = QtWidgets.QApplication.instance()

    def pump():
        for _ in range(2):
            for _ in range(20):
                app.processEvents()
            _settle(win)
    try:
        win.resize(1200, 700)
        win.show()
        page = win.merge_page
        win.pages.setCurrentWidget(page)
        page.plan_all()
        page.select_file(rel)
        pump()
        bar = page.rows.verticalScrollBar()
        for row in page.row_widgets()[3:10:3]:
            bar.setValue(row.y())
            pump()
            at = bar.value()
            button = next(b for b in row.findChildren(QtWidgets.QPushButton) if b.text() == "Keep mine")
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            assert bar.value() == at
            pump()
            assert bar.value() == at
        for act in ("keep", "take"):
            QTest.mouseClick(page.file_buttons[act], Qt.MouseButton.LeftButton)
            pump()
            assert bar.value() == at
    finally:
        _close(win)
