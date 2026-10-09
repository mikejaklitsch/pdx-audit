"""The Merge page of the --display app: plan the merge, decide single decisions or a
whole file with buttons, and apply a file once nothing in it is open.

The page calls `pdx-audit merge`, as the app calls the audits. The plan and the
user's choices stay in memory. Each command reads them from a temporary file that the
page deletes when the command ends, so the page keeps nothing on disk. The plan holds
the result of each choice (see merge_cli), so a choice runs no command: the page
calls merge_cli.build, the function of the dry run, in the app's merge worker process
(merge_rows, worker), and the file shows the choice at once. Apply calls `merge --apply` with the choices, so the checks
of the command line stay: the file must read as it did at the dry run, its
removed-line check must pass, and no decision in it is open.

A click on a row shows the full block that holds its change in the bottom view, with
the lines that Apply file deletes and adds. The worker makes those rows too, so no
calculation holds the window."""
import html
import json
import os
import re
import tempfile
import textwrap
import threading
import uuid
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
                               QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import merge, merge_cli, worker
from .merge_rows import (INDENT, MISSING, SELECTED, block_of, diff_rows,  # noqa: F401  (the tests use them)
                         from_body, layout)
from .safety import remove_file
from .tracker import tag_of

# The steps of a merge, side by side in the top bar. {n} and {mark} are colours.
HELP_STEPS = (
    "Set <b>Ported to</b> to the vanilla version that your files match. Set <b>Merge in</b> to the new "
    "version. Click <b>Plan merge</b>.",
    "Select a file. On each row, ✓ shows the text that <b>Apply file</b> writes. To change it, click the "
    "other button. The app saves your choices.",
    "A row with <span style=\"color:{mark};\">●</span> <b>Decision for you</b> has no default. Choose one. "
    "When no decision is left, click <b>Apply file</b>.")
HELP = ('<table width="100%" cellspacing="0" cellpadding="0"><tr>'
        + "".join(f'<td width="33%" valign="top" style="padding-right: 24px;">'
                  f'<b style="color:{{n}};">{i}</b>&nbsp;&nbsp;{step}</td>' for i, step in enumerate(HELP_STEPS, 1))
        + "</tr></table>")

# What happened to the text, in the words a row shows.
KIND_WORDS = {
    "vanilla_changed": "Vanilla changed this. You did not change it.",
    "vanilla_added": "Vanilla added this. Your file does not have it.",
    "vanilla_removed": "Vanilla deleted this. Your file still has it.",
    "both_changed": "You and vanilla both changed this.",
    "removed_changed": "You deleted this. Vanilla changed it.",
    "both_added": "You and vanilla both added this, with different text.",
    "inject_overlap": "Vanilla changed a key that you inject.",
    "not_merged": "The merge cannot put the changes of this copy into your file.",
    "own_text": "Your own text for these lines.",
}
BUTTONS = ((merge.TAKE, "Take vanilla"), (merge.KEEP, "Keep mine"))
FILE_BUTTONS = ((merge.TAKE, "Take vanilla for all"), (merge.KEEP, "Keep mine for all"))
FOLDER = Qt.ItemDataRole.UserRole + 1     # a folder item's path in the file tree
MISSING_OURS = "(not in your file)"
MISSING_THEIRS = "(vanilla deleted it)"


class RealText(str):
    """Text as a file holds it, which a code box shows as it is (not laid out)."""


def _dedent(text):
    """`text` without the indentation that all its lines share. A text that starts in
    the middle of a line loses the indentation of its last line, which closes it."""
    lines = text.split("\n")
    if len(lines) > 1 and lines[0][:1] not in ("\t", " "):
        cut = len(lines[-1]) - len(lines[-1].lstrip())
        return "\n".join([lines[0]] + [ln[cut:] if ln[:cut].isspace() else ln.lstrip() for ln in lines[1:]])
    return textwrap.dedent(text)


class MergePage(QWidget):
    # The page makes the rows of a file this many at a time, and the next ones when the
    # list scrolls near its end. A file can hold thousands of decisions.
    PAGE = 15
    # A build usually takes some milliseconds. The page says that a file updates only
    # when its build takes longer than this, so a click does not show the words for
    # one frame. Apply file checks the build itself (_apply_block).
    UPDATING_DELAY = 300
    SORTS = (("ready", "Ready files first"), ("most", "Most decisions first"),
             ("fewest", "Fewest decisions first"), ("name", "File name"))
    SHOWS = (("all", "All files"), ("ready", "Ready files"), ("decide", "Files with decisions"),
             ("hand", "Files to merge by hand"))
    ROW_SHOWS = (("all", "All rows"), ("open", "Decisions for you"))

    def __init__(self, win, colours, mono, settings):
        super().__init__(objectName="mergePage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.win, self.C, self.mono, self._settings = win, colours, mono, settings
        self.plan = None
        self.current = None
        # {mod path: {"sha": ..., "all": action or None, "nodes": {choice key: action},
        #  "own": [{"span": [start, end], "text": your text, "edges": [before, after]}]}}
        self.choices = {}
        self.building = set()        # files that merge_cli.build makes again with the user's choices
        self._token, self._token_plan = None, None   # the merge worker's name for self.plan
        self._versions_of_choices = None             # (old, new) of self.choices
        self._gathered = {}          # {choice key: gathered decision} of the file on the page
        self._row_buttons, self._rows = [], []
        self._decs, self._pending = [], []   # the file's decisions; the ones the filter shows with no row yet
        self._selected = None        # the row that the bottom view shows
        self._touched = set()        # choice keys of the rows the user changed; the row filter keeps them
        self.view_pending = False

        from .app import BlockView, Combo, OverlayScrollBar   # the Audit page's widgets; app imports this module
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        # Each part of the page is a panel: the bar on top, the file list, the file's
        # head, its rows and the block view. A gap divides each panel from the next.
        top_bar = QFrame(objectName="mergeBar")
        bv = QVBoxLayout(top_bar)
        bv.setContentsMargins(16, 12, 16, 12)
        bv.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.old_combo, self.new_combo = Combo(), Combo()
        top.addWidget(QLabel("Ported to"))
        top.addWidget(self.old_combo)
        top.addWidget(QLabel("Merge in"))
        top.addWidget(self.new_combo)
        self.plan_button = QPushButton("Plan merge")
        self.plan_button.setProperty("kind", "primary")
        self.plan_button.clicked.connect(self.plan_all)
        top.addWidget(self.plan_button)
        top.addStretch(1)
        self.summary = QLabel(objectName="hint")
        top.addWidget(self.summary)
        self.clear_saved_button = QPushButton("Clear saved choices")
        self.clear_saved_button.setToolTip("Delete your choices for these versions, in every file, "
                                           "on this page and on disk.")
        self.clear_saved_button.clicked.connect(self.clear_saved)
        top.addWidget(self.clear_saved_button)
        self.apply_ready_button = QPushButton("Apply all ready files")
        self.apply_ready_button.clicked.connect(self.apply_ready)
        top.addWidget(self.apply_ready_button)
        bv.addLayout(top)
        self.help = QLabel(HELP.format(n=colours["accent_text"], mark=colours["stale"]), objectName="hint")
        self.help.setWordWrap(True)
        self.help.setTextFormat(Qt.TextFormat.RichText)
        bv.addWidget(self.help)
        lay.addWidget(top_bar)

        split = QSplitter()
        split.setHandleWidth(8)
        split.setChildrenCollapsible(False)
        left = QFrame(objectName="listPanel")
        lv = QVBoxLayout(left)
        lv.setContentsMargins(1, 1, 1, 1)          # inside the panel's border
        lv.setSpacing(0)
        list_head = QFrame(objectName="listHead")
        lh = QVBoxLayout(list_head)
        lh.setContentsMargins(12, 10, 12, 10)
        lh.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by file name")
        self.search.textChanged.connect(lambda _t: self._show_plan(keep=self.current))
        lh.addWidget(self.search)
        pick = QHBoxLayout()
        pick.setSpacing(8)
        self.show_combo, self.sort_combo = Combo(), Combo()
        for combo, items, key in ((self.show_combo, self.SHOWS, "merge_show"),
                                  (self.sort_combo, self.SORTS, "merge_sort")):
            for value, label in items:
                combo.addItem(label, value)
            combo.setCurrentIndex(max(combo.findData(self._settings().value(key, items[0][0])), 0))
            combo.activated.connect(lambda _i, c=combo, k=key: self._remember(k, c))
            pick.addWidget(combo, 1)
        lh.addLayout(pick)
        boxes = QHBoxLayout()
        boxes.setSpacing(16)
        self.tree_box, self.paths_box = QCheckBox("Folder tree"), QCheckBox("Full paths")
        self.tree_box.setToolTip("Show the files in their folders. Click a folder to close or open it.")
        self.paths_box.setToolTip("Show the path of each file, not only its name. "
                                  "The tooltip of a file always shows its path.")
        for box, key in ((self.tree_box, "merge_tree"), (self.paths_box, "merge_paths")):
            box.setChecked(self._settings().value(key, False, type=bool))
            box.toggled.connect(lambda on, k=key: (self._settings().setValue(k, on), self._show_plan(keep=self.current)))
            boxes.addWidget(box)
        boxes.addStretch(1)
        lh.addLayout(boxes)
        lv.addWidget(list_head)
        self._closed_folders = set()     # the folders of the tree that the user closed
        self.file_list = QTreeWidget(objectName="mergeFiles")
        self.file_list.setHeaderHidden(True)
        self.file_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.file_list.setMinimumWidth(280)
        self.file_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.file_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.file_list.setExpandsOnDoubleClick(False)
        OverlayScrollBar(self.file_list)
        self.file_list.currentItemChanged.connect(
            lambda item, _prev: self.select_file(item.data(0, Qt.ItemDataRole.UserRole))
            if item and item.data(0, Qt.ItemDataRole.UserRole) else None)
        self.file_list.itemClicked.connect(lambda item, _col: self._toggle_folder(item))
        self.file_list.itemExpanded.connect(lambda item: self._closed_folders.discard(item.data(0, FOLDER)))
        self.file_list.itemCollapsed.connect(lambda item: self._closed_folders.add(item.data(0, FOLDER)))
        lv.addWidget(self.file_list, 1)
        split.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(8)
        file_head = QFrame(objectName="detailHead")
        fv = QVBoxLayout(file_head)
        fv.setContentsMargins(16, 12, 16, 12)
        fv.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.file_label = QLabel()
        self.file_label.setStyleSheet(f"font-family: '{mono}'; font-size: 14px; font-weight: 600;")
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.file_label.setTextFormat(Qt.TextFormat.PlainText)
        head.addWidget(self.file_label, 1)
        self.row_combo = Combo()
        for value, label in self.ROW_SHOWS:
            self.row_combo.addItem(label, value)
        # The app starts with All rows. A row that the user changes stays until the user
        # picks a different file or filter.
        self.row_combo.activated.connect(lambda _i: (self._touched.clear(), self._show_file(self.current)))
        head.addWidget(self.row_combo)
        self.open_file_button = QPushButton("Open in editor")
        self.open_file_button.clicked.connect(lambda: self.current and self.win.open_file(self.current, 1))
        head.addWidget(self.open_file_button)
        self.apply_button = QPushButton("Apply file")
        self.apply_button.setProperty("kind", "primary")
        self.apply_button.clicked.connect(lambda: self.current and self.apply_file(self.current))
        head.addWidget(self.apply_button)
        fv.addLayout(head)
        every = QHBoxLayout()
        every.setSpacing(6)
        self.file_buttons = {}
        for act, text in FILE_BUTTONS:
            b = QPushButton(text)
            b.setToolTip(f"{text.rsplit(' for all', 1)[0]} for each decision in this file. "
                         "A choice that you make on a row after this stays.")
            b.clicked.connect(lambda _c=False, act=act: self.current and self.choose_all(act))
            every.addWidget(b)
            self.file_buttons[act] = b
        self.clear_button = QPushButton("Clear my choices")
        self.clear_button.setToolTip("Use the default of each decision in this file again.")
        self.clear_button.clicked.connect(lambda: self.current and self.clear_choices())
        every.addWidget(self.clear_button)
        every.addStretch(1)
        fv.addLayout(every)
        self.file_note = QLabel(objectName="hint")
        self.file_note.setWordWrap(True)
        self.file_note.setTextFormat(Qt.TextFormat.PlainText)
        fv.addWidget(self.file_note)
        rv.addWidget(file_head)
        body = QSplitter(Qt.Orientation.Vertical)
        body.setHandleWidth(8)
        body.setChildrenCollapsible(False)
        self.rows = QScrollArea(objectName="rowWell")
        self.rows.setWidgetResizable(True)
        self.rows.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.rows.viewport().setAutoFillBackground(False)   # the panel's round corners show
        OverlayScrollBar(self.rows)
        bar = self.rows.verticalScrollBar()
        bar.valueChanged.connect(self._scrolled)
        bar.rangeChanged.connect(lambda _lo, _hi: self._scrolled(bar.value()))
        self._new_row_box()
        body.addWidget(self.rows)
        diff_box = QFrame(objectName="viewPanel")
        dv = QVBoxLayout(diff_box)
        dv.setContentsMargins(1, 1, 1, 8)          # the view's square corners stay inside the round ones
        dv.setSpacing(0)
        view_bar = QFrame(objectName="viewBar")
        view_head = QHBoxLayout(view_bar)
        view_head.setContentsMargins(16, 6, 8, 6)
        view_head.setSpacing(12)
        # The place of the block, a short hint when one helps, and what the signs mean.
        self.view_title = QLabel(objectName="viewTitle")
        self.view_title.setTextFormat(Qt.TextFormat.PlainText)
        self.view_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        view_head.addWidget(self.view_title, 1)
        self.view_hint = QLabel(objectName="faint")
        self.view_hint.setTextFormat(Qt.TextFormat.PlainText)
        view_head.addWidget(self.view_hint)
        self.view_legend = QLabel(objectName="faint")
        self.view_legend.setTextFormat(Qt.TextFormat.RichText)
        view_head.addWidget(self.view_legend)
        self.whole_button = QPushButton("Show whole file")
        self.whole_button.setProperty("kind", "ghost")
        self.whole_button.clicked.connect(lambda: self.select_row(None))
        view_head.addWidget(self.whole_button)
        dv.addWidget(view_bar)
        self.block_view = BlockView()
        self.block_view.set_wrap(self._settings().value("wrap_lines", False, type=bool))
        dv.addWidget(self.block_view, 1)
        body.addWidget(diff_box)
        body.setSizes([400, 400])
        rv.addWidget(body, 1)
        split.addWidget(right)
        split.setSizes([360, 900])
        split.setStretchFactor(1, 1)
        lay.addWidget(split, 1)
        self._show_file(None)
        self.set_busy(False)

    def _remember(self, key, combo):
        self._settings().setValue(key, combo.currentData())
        self._show_plan(keep=self.current)

    def _new_row_box(self):
        box = QFrame(objectName="rowBox")
        self.row_box = QVBoxLayout(box)
        self.row_box.setContentsMargins(14, 14, 14, 14)
        self.row_box.setSpacing(10)
        self.more_label = QLabel(objectName="hint")
        self.more_label.hide()
        self.row_box.addWidget(self.more_label)
        # What the rows area shows when the row filter shows no row.
        self.empty_note = QFrame()
        en = QHBoxLayout(self.empty_note)
        en.setContentsMargins(4, 4, 4, 4)
        en.setSpacing(12)
        self.empty_label = QLabel(objectName="hint")
        self.empty_label.setWordWrap(True)
        en.addWidget(self.empty_label, 1)
        show_all = QPushButton("Show all rows")
        show_all.clicked.connect(self._show_all_rows)
        en.addWidget(show_all)
        self.empty_note.hide()
        self.row_box.addWidget(self.empty_note)
        self.row_box.addStretch(1)
        self.rows.setWidget(box)

    def _show_all_rows(self):
        self.row_combo.setCurrentIndex(self.row_combo.findData("all"))
        self._touched.clear()
        self._show_file(self.current)

    def _nothing_shown(self):
        """True when the file has rows and the row filter shows none of them."""
        return bool(self._decs) and not any(self._passes(d) for d in self._decs)

    def _show_empty_note(self):
        """Say why the rows area is empty, and how to show the rows."""
        self.empty_label.setText("All decisions are ready. To change one, show all rows.")
        self.empty_note.setVisible(self._nothing_shown())

    def _scrolled(self, value):
        if self._pending and value >= self.rows.verticalScrollBar().maximum() - 400:
            self._add_more()

    def _add_more(self, upto=None):
        """Make the next PAGE rows, or the rows up to the decision whose choice key is `upto`."""
        n = self.PAGE
        if upto is not None:
            at = next((i for i, d in enumerate(self._pending)
                       if d.get("address") and merge_cli.choice_key(d) == upto), None)
            n = max(n, (at or 0) + 1)
        batch, self._pending = self._pending[:n], self._pending[n:]
        for d in batch:
            self._add_row(d)
        total = len(self._rows) + len(self._pending)
        self.more_label.setText(f"{len(self._rows)} of {total} rows. Scroll down to see more.")
        self.more_label.setVisible(bool(self._pending))

    # --- versions -------------------------------------------------------------------

    def refresh_versions(self):
        """Fill the version boxes from the tracker: the newest commit to merge in, and
        the commit before it as the version the copies were ported to."""
        tags = [tag_of(m) for _h, m in self.win.commits]
        for combo, default in ((self.old_combo, 1), (self.new_combo, 0)):
            keep = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(tags)
            i = combo.findText(keep)
            combo.setCurrentIndex(i if i >= 0 else min(default, len(tags) - 1))
            combo.blockSignals(False)

    def old_tag(self):
        return self.old_combo.currentText()

    def new_tag(self):
        return self.new_combo.currentText()

    # --- buttons ----------------------------------------------------------------------

    def _gate(self, button, why):
        """Enable `button` when `why` is empty; else disable it and give the reason."""
        if button.property("idle_tip") is None:
            button.setProperty("idle_tip", button.toolTip())
        button.setEnabled(not why)
        button.setToolTip(why or button.property("idle_tip"))

    def _apply_block(self, rel):
        """Why Apply file cannot write `rel` now, or ""."""
        f = self.file(rel)
        if f is None:
            return "Pick a file."
        if rel in self.building:
            return "Wait: the page is updating this file with your last choices."
        if f["open"]:
            return f"{f['open']} decision{'' if f['open'] == 1 else 's'} for you {'is' if f['open'] == 1 else 'are'} left."
        if not merge_cli.ready_to_apply(f):
            return "The merge cannot write this file. Merge it by hand."
        return ""

    def set_busy(self, busy):
        wait = getattr(self.win, "busy_reason", "") if busy else ""
        has_plan = self.plan is not None
        self._gate(self.plan_button, wait or ("" if self.win.commits else "The tracker has no commits."))
        ready = [f["file"] for f in self.plan["files"] if not self._apply_block(f["file"])] if has_plan else []
        self._gate(self.apply_ready_button, wait or ("" if ready else "No file is ready."))
        self._gate(self.apply_button, wait or (self._apply_block(self.current) if self.current else "Pick a file."))
        for b in self.file_buttons.values():
            self._gate(b, wait or ("" if self.file(self.current) else "Pick a file."))
        self._gate(self.clear_button, wait or ("" if self.choices.get(self.current) else "This file has no choices."))
        for b in self._row_buttons:
            self._gate(b, wait)

    # --- running the command line ----------------------------------------------------------

    @staticmethod
    def _temp(name):
        fd, path = tempfile.mkstemp(prefix=f"pdx-audit-{name}-", suffix=".json")
        os.close(fd)
        return Path(path)

    @staticmethod
    def _drop(*paths):
        """Delete the page's temporary files, one named file at a time."""
        for p in paths:
            if p is not None and Path(p).is_file():
                remove_file(p, tempfile.gettempdir(), r"pdx-audit-(plan|choices)-[A-Za-z0-9_]+\.json")

    def _choices_file(self, rels=None):
        """A temporary choices file for the files `rels` (all when None), or None."""
        files = {r: c for r, c in self.choices.items() if (c["nodes"] or c.get("all") or c.get("own"))
                 and (rels is None or r in rels)}
        if not files:
            return None
        path = self._temp("choices")
        path.write_text(json.dumps({"files": files}, ensure_ascii=False), encoding="utf-8")
        return path

    def _versions(self):
        return ["--old", self.old_tag(), "--new", self.new_tag()]

    def _run(self, argv, then, text):
        """Run a merge command in the app's one command slot, which blocks the other
        commands of the app until it ends."""
        self.win._start(self._versions() + argv, then, text, command="merge")

    # --- the plan -------------------------------------------------------------------

    def plan_all(self):
        if not self.old_tag() or not self.new_tag():
            return
        self._load_saved()
        out, choices = self._temp("plan"), self._choices_file()

        def done(code):
            text = out.read_text(encoding="utf-8") if code == 0 else ""
            self._drop(out, choices)
            if code != 0:
                self.win._job_failed("The merge plan stopped with an error. Its output is in the Log.")
                return
            self.plan = json.loads(text)
            self.building = set()
            self._show_plan()
            self.win._status(self.summary.text())
        argv = ["--dry-run", "--quiet", "--plan-out", str(out)] + (["--choices", str(choices)] if choices else [])
        self._run(argv, done, "Planning the merge…")

    def file(self, rel):
        return next((f for f in (self.plan or {}).get("files", []) if f["file"] == rel), None)

    def ready(self, rel):
        f = self.file(rel)
        return f is not None and merge_cli.ready_to_apply(f)

    def file_names(self):
        return [item.data(0, Qt.ItemDataRole.UserRole) for item in self._file_items()]

    def _file_items(self, parent=None):
        """The items of the file list that show a file, in the order the list shows them."""
        parent = self.file_list.invisibleRootItem() if parent is None else parent
        for i in range(parent.childCount()):
            item = parent.child(i)
            if item.data(0, Qt.ItemDataRole.UserRole):
                yield item
            yield from self._file_items(item)

    def _toggle_folder(self, item):
        """Open or close a folder. The file on the page stays the current item."""
        if item.data(0, FOLDER):
            item.setExpanded(not item.isExpanded())
            self.select_file(self.current, rebuild=False)

    def _state(self, f):
        if not f["removed_check"]["passed"] or f.get("format", {}).get("failed") or f["stale_entries"]:
            return "hand"
        return "decide" if f["open"] else "ready"

    def _has_work(self, f):
        """True when file `f` holds a change to apply, a decision to make, or a choice of
        the user. A file whose choices keep all of the user's text holds no change, and
        it stays so that the user can change those choices."""
        rec = self.choices.get(f["file"]) or {}
        return bool(f.get("diff", "").strip() or f["open"] or self._state(f) != "ready"
                    or rec.get("nodes") or rec.get("all") or rec.get("own"))

    def _listed(self, f):
        """A file is listed when it has work (_has_work), and it passes the search and
        the filter."""
        if not self._has_work(f):
            return False
        needle = self.search.text().strip().lower()
        if needle and needle not in f["file"].lower():
            return False
        show = self.show_combo.currentData()
        return show == "all" or self._state(f) == show

    def _sort_key(self, f):
        sort = self.sort_combo.currentData()
        if sort == "most":
            return (-f["open"], f["file"])
        if sort == "fewest":
            return (f["open"] == 0, f["open"], f["file"])
        if sort == "name":
            return (f["file"],)
        return ({"ready": 0, "decide": 1, "hand": 2}[self._state(f)], f["open"], f["file"])

    def _item_text(self, f):
        taken = sum(1 for d in f["decisions"] if d["action"] == merge.TAKE)
        changes = f"{taken} change{'' if taken == 1 else 's'} from vanilla"
        state = {"ready": "ready",
                 "decide": f"{f['open']} decision{'' if f['open'] == 1 else 's'} for you",
                 "hand": "merge by hand"}[self._state(f)]
        if f["file"] in self.building:
            state += " · updating"
        colour = {"ready": self.C["added"], "decide": self.C["stale"], "hand": self.C["broken"]}[self._state(f)]
        full = self.paths_box.isChecked() and not self.tree_box.isChecked()
        return f"{f['file'] if full else f['file'].rsplit('/', 1)[-1]}\n{changes} · {state}", colour

    def _file_item(self, parent, f):
        item = QTreeWidgetItem(parent)
        item.setData(0, Qt.ItemDataRole.UserRole, f["file"])
        item.setToolTip(0, f["file"])
        self._set_item(item, f)
        return item

    def _set_item(self, item, f):
        text, colour = self._item_text(f)
        item.setText(0, text)
        item.setForeground(0, QColor(colour))

    def _fill_tree(self, files):
        """The files `files`, in their folders. A folder that holds only one folder
        shows as one item with both names. Folders come first, by name; the files of a
        folder keep the order of `files`."""
        root = {"dirs": {}, "files": []}
        for f in files:
            node = root
            for part in f["file"].split("/")[:-1]:
                node = node["dirs"].setdefault(part, {"dirs": {}, "files": []})
            node["files"].append(f)

        def add(parent, node, path):
            for name in sorted(node["dirs"]):
                sub, label = node["dirs"][name], name
                while len(sub["dirs"]) == 1 and not sub["files"]:
                    [(more, sub)] = sub["dirs"].items()
                    label += "/" + more
                item = QTreeWidgetItem(parent)
                item.setData(0, FOLDER, path + label)
                item.setToolTip(0, path + label)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                add(item, sub, path + label + "/")
                count = sum(1 for _ in self._file_items(item))
                item.setText(0, f"{label}  ({count})")
                item.setForeground(0, QColor(self.C["muted"]))
                item.setExpanded(path + label not in self._closed_folders)
            for f in node["files"]:
                self._file_item(parent, f)
        add(self.file_list.invisibleRootItem(), root, "")

    def _show_summary(self):
        every = [f for f in self.plan["files"] if self._has_work(f)]
        ready = sum(1 for f in every if self._state(f) == "ready")
        held = sum(1 for f in every if self._state(f) == "decide")
        self.summary.setText(f"{len(every)} files: {ready} ready to apply, {held} wait for decisions")

    def _show_plan(self, keep=None):
        if self.plan is None:
            return
        self._show_summary()
        files = sorted((f for f in self.plan["files"] if self._listed(f)), key=self._sort_key)
        tree = self.tree_box.isChecked()
        self.paths_box.setEnabled(not tree)
        self.file_list.setRootIsDecorated(tree)
        self.file_list.setIndentation(14 if tree else 0)
        self.file_list.blockSignals(True)
        self.file_list.clear()
        if tree:
            self._fill_tree(files)
        else:
            for f in files:
                self._file_item(self.file_list.invisibleRootItem(), f)
        self.file_list.blockSignals(False)
        target = keep if keep and any(f["file"] == keep for f in files) else (files[0]["file"] if files else None)
        if target != self.current or target is None:
            self.select_file(target)
        else:
            self.select_file(target, rebuild=False)

    def _refresh_file(self, rel):
        """Show a file's new plan without moving the list or the rows."""
        f = self.file(rel)
        if f is not None:
            self._refresh_file_item(f)
        if self.plan is not None:
            self._show_summary()
        if rel != self.current:
            return
        if f is None:
            self._show_file(None)
            return
        # The rows stay when the file holds the same decisions in the same order.
        fresh = self.decisions()
        same = lambda a, b: (a["kind"], a.get("address"), a.get("copy"), a.get("line")) == (   # noqa: E731
            b["kind"], b.get("address"), b.get("copy"), b.get("line"))
        if len(fresh) != len(self._decs) or not all(same(a, b) for a, b in zip(self._decs, fresh)):
            bar = self.rows.verticalScrollBar()
            at = bar.value()
            self._show_file(rel)
            QTimer.singleShot(0, lambda: bar.setValue(at))
            return
        index = {id(d): i for i, d in enumerate(self._decs)}
        new = lambda d: fresh[index[id(d)]]     # noqa: E731
        self._decs, self._pending = fresh, [new(d) for d in self._pending]
        self._rows = [(new(d), row) for d, row in self._rows]
        for d, row in self._rows:
            self._fill_row(row, d)
        self._apply_row_filter()
        self._show_header(f)
        self._show_bottom(again=True)       # the merged text changed

    # --- one file ---------------------------------------------------------------------

    def select_file(self, rel, rebuild=True):
        for item in self._file_items():
            if item.data(0, Qt.ItemDataRole.UserRole) == rel:
                self.file_list.blockSignals(True)
                self.file_list.setCurrentItem(item)
                self.file_list.blockSignals(False)
        if rebuild or rel != self.current:
            self._show_file(rel if self.file(rel) else None)
        else:
            self._show_header(self.file(rel))

    def decisions(self):
        """The file's decisions, in the order of the file (merge_cli.build sorts them)."""
        f = self.file(self.current)
        return list(f["decisions"]) if f is not None else []

    def _show_header(self, f):
        notes = []
        rel = f["file"]
        if rel in self.building:
            notes.append("Updating the file with your choices.")
        elif f.get("not_set"):
            word = dict(FILE_BUTTONS)[f["choices"]["all"]]
            n = sum(c for _why, c in f["not_set"])
            notes.append(f"{word} does not apply to {n} decision{'' if n == 1 else 's'}:")
            notes += [f"{why[:1].upper()}{why[1:]} ({c})." for why, c in f["not_set"]]
        if f["open"]:
            notes.append(f"{f['open']} decision{'' if f['open'] == 1 else 's'} for you.")
        if not f["removed_check"]["passed"]:
            notes.append("The merge deletes lines that no vanilla change explains. Merge this file by hand.")
        if f["stale_entries"]:
            notes.append("An intent store entry is out of date. Confirm it with pdx-audit intent confirm.")
        if f.get("format", {}).get("failed"):
            notes.append("pdx-format did not accept the merged text. Merge this file by hand.")
        self.file_note.setText(" ".join(notes) or "Ready to apply.")
        self.set_busy(self.win.process is not None)

    def _show_file(self, rel):
        keep = self._selected_key()
        if rel != self.current:
            self._touched = set()
        self.current = rel
        # Clear the old file's rows first: the new box changes the scroll range, and
        # _scrolled then makes the next rows of whatever _pending holds.
        self._row_buttons, self._rows, self._selected = [], [], None
        self._decs, self._pending = [], []
        self._new_row_box()
        f = self.file(rel)
        self.open_file_button.setEnabled(f is not None)
        if f is None:
            self.file_label.setText("Plan the merge, then pick a file." if self.plan is None
                                    else "No file to merge." if not self.plan["files"] else "")
            self.file_note.setText("")
            self._show_bottom()
            self.set_busy(self.win.process is not None)
            return
        self.file_label.setText(rel)
        self._gathered = {r["key"]: r for r in self._gathered_rows(f) if r["key"]}
        self._decs = self.decisions()
        self._pending = [d for d in self._decs if self._passes(d)]
        self._add_more(keep)
        self._show_empty_note()
        self._show_header(f)
        rows = [row for d, row in self._rows if keep is not None and d.get("address")
                and merge_cli.choice_key(d) == keep]
        shown = [row for d, row in self._rows if self._passes(d)]
        self.select_row((rows or shown or [None])[0], scroll=True)

    def _passes(self, d):
        """True when the row filter shows decision `d`. A row that the user changed
        stays, so a click never takes a row away."""
        show = self.row_combo.currentData()
        key = merge_cli.choice_key(d) if d.get("address") else None
        if show == "all" or key in self._touched:
            return True
        return self._shown_action(d) == merge.OPEN

    def _apply_row_filter(self):
        for d, row in self._rows:
            row.setVisible(self._passes(d))
        self._show_empty_note()

    @staticmethod
    def _gathered_rows(f):
        """The gathered decisions of plan file `f` (see merge_cli.build)."""
        g = f.get("gathered") or {}
        for cp in g.get("copies", ()):
            yield from cp.get("decisions", ())
        yield from (g.get("added") or {}).get("decisions", ())
        yield from g.get("removed", ())

    def _choice(self, d):
        """The user's choice for decision `d`: its own, else the file's choice for all."""
        rec = self.choices.get(self.current)
        if rec is None or not d.get("address"):
            return None
        return rec["nodes"].get(merge_cli.choice_key(d)) or rec.get("all")

    def _shown_action(self, d):
        """The action of `d`. While the page updates the file, the action that the
        user's choice gives the decision (merge_cli._chosen), so a row shows a click
        at once."""
        if self.current not in self.building or not d.get("address"):
            return d["action"]
        row = self._gathered.get(merge_cli.choice_key(d))
        if row is None:
            return d["action"]
        return merge_cli._chosen(row, self.choices.get(self.current), [])[0]["action"]

    def _code_column(self, title, text):
        box = QVBoxLayout()
        box.setSpacing(2)
        head = QLabel(title, objectName="faint")
        box.addWidget(head)
        shown = text if isinstance(text, RealText) or text in (MISSING_OURS, MISSING_THEIRS) else layout(text)
        shown = shown.replace("\t", INDENT)            # a tab is 4 columns, as in the block view
        code = QLabel(f'<pre style="white-space: pre-wrap; margin: 0;">{html.escape(shown)}</pre>',
                      objectName="codeBox")
        code.setTextFormat(Qt.TextFormat.RichText)
        code.setWordWrap(True)
        code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        code.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        code.installEventFilter(self)
        box.addWidget(code, 1)
        return box

    @staticmethod
    def where(d):
        """The place of a decision: its copy, the keys down to its node, and its line, or
        the line near which the merge puts a node that your file does not hold."""
        steps = [] if "/" in (d.get("copy") or "/") else [d["copy"]]
        for seg in d.get("path") or []:
            key = str(seg.get("key", ""))
            if steps and key == steps[-1]:
                continue
            steps.append(f"{key} {seg['name']}" if seg.get("name") else key)
        place = f" · line {d['line']}" if d.get("line") else f" · near line {d['at']}" if d.get("at") else ""
        return " › ".join(steps) + place

    def _add_row(self, d):
        row = QFrame(objectName="card")
        row.setProperty("selected", False)
        row.setCursor(Qt.CursorShape.PointingHandCursor)
        row.installEventFilter(self)
        v = QVBoxLayout(row)
        v.setContentsMargins(14, 12, 14, 10)
        v.setSpacing(6)
        row.head = QLabel()
        row.head.setTextFormat(Qt.TextFormat.RichText)
        row.head.setWordWrap(True)
        v.addWidget(row.head)
        row.what = QLabel(objectName="hint")
        row.what.setTextFormat(Qt.TextFormat.PlainText)
        row.what.setWordWrap(True)
        v.addWidget(row.what)
        new_tag = (self.plan or {}).get("new") or "new"
        ours = d.get("ours") or (MISSING_OURS if d["kind"] in ("removed_changed", "vanilla_added") else None)
        theirs = d.get("theirs") or (MISSING_THEIRS if d["kind"] in ("both_changed", "vanilla_removed") else None)
        # Your text and vanilla's as the files hold them, from the decision's op, in place
        # of the canonical text that the merge compares.
        span, f = d.get("span"), self.file(self.current)
        if span and span[0] < span[1] and f is not None:
            ours = RealText(_dedent(f["gathered"]["ours"][span[0]:span[1]]))
        if (d.get("vanilla") or "").strip():
            theirs = RealText(_dedent(d["vanilla"].strip("\n")))
        columns = [(f"Vanilla {d.get('base_version') or 'before'}, before the change", d.get("base")),
                   ("Your text", ours), (f"Vanilla {new_tag}", theirs)]
        columns = [(t, x) for t, x in columns if x]
        if d["kind"] in ("vanilla_added", "vanilla_removed") or len(columns) < 2:
            columns = [(t, x) for t, x in columns if not t.endswith("before the change")] or columns
        if columns:
            grid = QHBoxLayout()
            grid.setContentsMargins(0, 4, 0, 4)
            grid.setSpacing(10)
            for title, text in columns:
                grid.addLayout(self._code_column(title, text), 1)
            v.addLayout(grid)
        foot = QFrame(objectName="cardFoot")       # a line divides the buttons from the text
        row.buttons = QHBoxLayout(foot)
        row.buttons.setContentsMargins(0, 10, 0, 0)
        row.buttons.setSpacing(6)
        v.addWidget(foot)
        for w in row.findChildren(QLabel):
            w.installEventFilter(self)
        self.row_box.insertWidget(self.row_box.count() - 3, row)   # before the more label, the note and the stretch
        self._rows.append((d, row))
        self._fill_row(row, d)

    @staticmethod
    def _sentence(text):
        text = text.replace("--old ", "")
        return text[:1].upper() + text[1:] + ("" if text.endswith(".") else ".")

    def _what(self, d):
        """What happened to the text of decision `d`, and the reason of its action."""
        what = KIND_WORDS.get(d["kind"], d["kind"])
        if d["kind"] == "both_changed" and not d.get("theirs"):
            what = "You changed this. Vanilla deleted it."
        return what + (" " + self._sentence(d["reason"]) if d.get("reason") else "")

    def _blocked(self, d, act):
        """Why the button of action `act` cannot change decision `d`, or ""."""
        row = self._gathered.get(merge_cli.choice_key(d)) if d.get("address") else None
        variant = row.get(act) if row else None
        if variant is None:
            return "No choice applies to this change" + (". Merge it by hand." if d["action"] == merge.OPEN else ".")
        if variant[0] != act:
            return self._sentence(variant[2].split("; ", 1)[-1])
        return ""

    def _fill_row(self, row, d):
        """Show the row's state and its buttons. The button of the action that Apply
        file does now shows ✓ and cannot be clicked. A button that no choice can use is
        off, and its tooltip and the row say why."""
        C = self.C
        choice = self._choice(d)
        own = d.get("own")                   # the span of your own text that decides it
        action = self._shown_action(d)
        mark = (f'<span style="color:{C["stale"]}; font-weight:600;">●&nbsp;Decision for you</span>&nbsp;&nbsp;'
                if action == merge.OPEN else "")
        row.head.setText(f'{mark}<span style="font-family:\'{self.mono}\'; color:{C["text"]};">'
                         f'{html.escape(self.where(d))}</span>')
        row.what.setText(self._what(d))
        # Qt gives the focus of a hidden button to the next widget, and the list scrolls
        # to show that widget. The row holds the focus, so the list stays where it is.
        if row.isAncestorOf(QApplication.focusWidget()):
            row.setFocus(Qt.FocusReason.OtherFocusReason)
        while row.buttons.count():
            w = row.buttons.takeAt(0).widget()
            if w is not None:
                if w in self._row_buttons:
                    self._row_buttons.remove(w)
                w.hide()                     # it stays on screen until Qt deletes it
                w.deleteLater()
        row.labels = []
        notes = []
        for act, text in BUTTONS:
            b = QPushButton(text)
            if own:
                b.setEnabled(False)
                b.setToolTip("Your own text decides this change. Click My own text to change or remove it.")
            elif act == action:
                b.setText("✓ " + text)
                b.setProperty("chosen", True)
                b.setEnabled(False)
            elif self._blocked(d, act):
                b.setEnabled(False)
                b.setToolTip(self._blocked(d, act))
                if self._blocked(d, act) not in notes:
                    notes.append(self._blocked(d, act))
            else:
                b.clicked.connect(lambda _c=False, d=d, act=act: self.choose(d, act))
                self._row_buttons.append(b)
                row.labels.append(text)
                if self.win.process is not None:
                    self._gate(b, getattr(self.win, "busy_reason", ""))
            row.buttons.addWidget(b)
        # Your own text: it takes the place of the whole block that holds the change.
        b = QPushButton("✓ My own text" if own else "Write my own")
        why = "" if own or d.get("span") is not None else "No text of your file holds this change."
        if own:
            b.setProperty("chosen", action != merge.OPEN)
        if why:
            b.setEnabled(False)
            b.setToolTip(why)
        else:
            b.setToolTip("Write the text that Apply file puts in place of the block that holds this change.")
            b.clicked.connect(lambda _c=False, d=d: self.edit_text(d))
            self._row_buttons.append(b)
            row.labels.append("Write my own")
            if self.win.process is not None:
                self._gate(b, getattr(self.win, "busy_reason", ""))
        row.buttons.addWidget(b)
        if own:
            notes.insert(0, "Your own text" if action != merge.OPEN else "Your own text cannot go in")
        elif action != merge.OPEN:
            by = "Your choice" if choice or d.get("by") == "choice" else "Default"
            notes.insert(0, by + (f" ({d['by']})" if d.get("by") and d["by"] != "choice" else ""))
        elif choice and self.current not in self.building:
            notes.insert(0, f"Your choice {dict(BUTTONS)[choice]} does not apply")
        if notes:
            note = QLabel(". ".join(n.rstrip(".") for n in notes) + ("." if len(notes) > 1 else ""),
                          objectName="faint")
            note.setTextFormat(Qt.TextFormat.PlainText)
            note.setWordWrap(True)
            note.installEventFilter(self)
            row.buttons.addWidget(note, 1)
        else:
            row.buttons.addStretch(1)
        if d.get("line"):
            b = QPushButton("Open in editor")
            b.clicked.connect(lambda _c=False, d=d: self.win.open_file(self.current, d["line"]))
            row.buttons.addWidget(b)
            self._row_buttons.append(b)
            row.labels.append("Open in editor")
            if self.win.process is not None:
                self._gate(b, getattr(self.win, "busy_reason", ""))
        # Qt shows a widget that goes into a shown layout one event later, so the row
        # paints one frame with no buttons. Show them now, in their places.
        for i in range(row.buttons.count()):
            w = row.buttons.itemAt(i).widget()
            if w is not None:
                w.show()
        row.buttons.activate()

    # --- the bottom view ---------------------------------------------------------------

    def eventFilter(self, obj, event):
        """A click on a row, or on the text in it, selects the row."""
        if event.type() == QEvent.Type.MouseButtonPress:
            rows = {id(row): row for _d, row in self._rows}
            w = obj
            while w is not None and id(w) not in rows:
                w = w.parentWidget()
            if w is not None and w is not self._selected:
                self.select_row(w)
        return False

    def _selected_key(self):
        d = self._row_decision(self._selected)
        return merge_cli.choice_key(d) if d is not None and d.get("address") else None

    def _row_decision(self, row):
        return next((d for d, r in self._rows if r is row), None)

    def select_row(self, row, scroll=False):
        """Show the block of `row` in the bottom view, or the whole file when it is None.
        `scroll`: move the list until the row shows. A click never moves the list."""
        for _d, r in self._rows:
            if r.property("selected") != (r is row):
                r.setProperty("selected", r is row)
                r.style().unpolish(r)
                r.style().polish(r)
        self._selected = row
        if row is not None and scroll:
            QTimer.singleShot(0, lambda: self._scroll_to(row))
        self._show_bottom()

    def _scroll_to(self, row):
        """Scroll the rows up or down, never sideways, only as far as `row` needs to show."""
        if row not in [r for _d, r in self._rows]:
            return
        bar, height = self.rows.verticalScrollBar(), self.rows.viewport().height()
        top, bottom = row.y() - 8, row.y() + row.height() + 8
        if top < bar.value():
            bar.setValue(max(top, 0))
        elif bottom > bar.value() + height:
            bar.setValue(min(top, bottom - height))

    def _show_bottom(self, again=False):
        """Fill the bottom view: the block of the selected row, or the whole file with
        its unchanged stretches folded. The merge worker process makes the rows, so a
        click never holds the window; only the result of the last click shows.
        `again`: the view shows the same block with new text, so its title stays."""
        f = self.file(self.current)
        d = self._row_decision(self._selected)
        self.whole_button.setVisible(f is not None and d is not None)
        if f is None or (d is None and self._nothing_shown()):     # no row to show a block of
            self.view_pending = False
            self.win._generations["merge view"] = self.win._generations.get("merge view", 0) + 1
            self._set_view_head("", "")
            self.block_view.clear()
            return
        self.view_pending = True
        if not again:
            self._set_view_head(self.where(d) if d else "Whole file",
                                "Finding the block…" if d else "Reading the file…")
        key = self._key(f)
        self.win._in_background("merge view", lambda: self._coloured(self._work("view", key, f, d)),
                                lambda r: self._view_done((d, r)), lambda: self._view_done(("error", None)))

    def _key(self, f):
        """The merge worker's name for plan file `f`: a token of the plan and the file.
        A new plan gets a new token, and the worker drops the files of the old one."""
        if self._token_plan is not self.plan:
            old, self._token, self._token_plan = self._token, uuid.uuid4().hex, self.plan
            if old is not None:
                threading.Thread(target=worker.call, args=("merge", "pdxaudit.merge_rows:forget", old),
                                 daemon=True).start()
        return self._token, f["file"]

    @staticmethod
    def _work(name, key, f, *args):
        """Runs on a worker thread: merge_rows.`name` for plan file `f` (worker name
        `key`) in the merge worker process. The worker gets the file the first time, and
        again when it lost it."""
        call = f"pdxaudit.merge_rows:{name}"
        out = worker.call("merge", call, key, *args)
        if out == MISSING:
            worker.call("merge", "pdxaudit.merge_rows:load", key, f)
            out = worker.call("merge", call, key, *args)
        return out

    @staticmethod
    def _coloured(out):
        """Runs on a worker thread: give the shown rows of view result `out` their
        syntax colours, so the view paints them one time, with their colours."""
        _kind, rows, visible = out
        if rows is not None:
            from .app import BlockView
            BlockView._colour([r for r in visible if "fold" not in r])
        return out

    def _set_view_head(self, title, hint, legend=False):
        """The bar above the block view: `title`, `hint`, and with `legend`, what the
        signs of the lines mean."""
        self.view_title.setText(title)
        self.view_title.setToolTip(title)
        self.view_hint.setText(hint)
        self.view_hint.setVisible(bool(hint))
        new = html.escape((self.plan or {}).get("new") or "new")
        self.view_legend.setText(f'<span style="color:{self.C["broken"]}; font-weight:600;">−</span> yours'
                                 f'&nbsp;&nbsp;&nbsp;<span style="color:{self.C["added"]}; font-weight:600;">+</span>'
                                 f' vanilla {new}')
        self.view_legend.setVisible(legend)

    def _view_done(self, result):
        self.view_pending = False
        d, out = result
        if d == "error":
            self._set_view_head("Cannot show the block", "The error is in the Log.")
            self.block_view.clear()
            return
        kind, rows, visible = out
        where = self.where(d or {})
        self._set_view_head(*{"file": ("Whole file", "Click a fold to open it."),
                              "block": (where, ""),
                              "none": (where, "No file holds this text. The row shows vanilla's text.")}[kind],
                            legend=rows is not None)
        if rows is None:
            self.block_view.clear()
            return
        prepared = (rows, visible)
        new = (self.plan or {}).get("new") or "new"
        columns = (f"Yours and vanilla {new}", "After Apply file")
        self.block_view.set_rows(rows, [], SELECTED, columns, prepared)

    def view_text(self):
        """The lines the bottom view shows, as "your line | line after apply"."""
        return "\n".join(f"{row[1] or ''} | {row[0] or ''}" if isinstance(row, tuple) else row or ""
                         for row in self.block_view.visible_texts())

    def row_buttons(self):
        """[(decision, [button labels])] in the order the rows show."""
        return [(d, row.labels) for d, row in self._rows]

    def row_widgets(self):
        return [row for _d, row in self._rows]

    def row_texts(self):
        """The text each row shows, without markup."""
        return [" ".join(re.sub(r"<[^>]+>", " ", html.unescape(w.text().replace("&nbsp;", " ")))
                         for w in row.findChildren(QLabel)) for _d, row in self._rows]

    def help_text(self):
        return re.sub(r"<[^>]+>", " ", self.help.text())

    # --- choices and apply ----------------------------------------------------------------

    def _record(self, f):
        """The user's choices for plan file `f`, made new when the file changed."""
        rec = self.choices.get(f["file"])
        if rec is None or rec.get("sha") != f["before_sha"]:
            rec = self.choices[f["file"]] = {"sha": f["before_sha"], "all": None, "nodes": {}, "own": []}
        return rec

    def _mark(self, d, action):
        f = self.file(self.current)
        if f is None or not d.get("address"):
            return
        rec = self._record(f)
        key = merge_cli.choice_key(d)
        self._touched.add(key)
        if action is None:
            rec["nodes"].pop(key, None)
        else:
            rec["nodes"][key] = action
        self._save()
        self.rebuild(f["file"])
        for rd, row in self._rows:
            if rd.get("address") and merge_cli.choice_key(rd) == key:
                self._fill_row(row, rd)
                if row is not self._selected:
                    self.select_row(row)
        self._apply_row_filter()

    def _refresh_file_item(self, f):
        for item in self._file_items():
            if item.data(0, Qt.ItemDataRole.UserRole) == f["file"]:
                self._set_item(item, f)

    def choose(self, d, action):
        """Record the user's choice for one decision. The row shows it at once, and the
        file shows it when merge_cli.build ends."""
        self._mark(d, action)

    def reset_choice(self, d):
        self._mark(d, None)

    def choose_all(self, action):
        """Choose `action` for each decision of the file on the page. This clears the
        choices of single rows, but not your own texts; a row choice that the user makes
        after it stays. The rows show the choice when the build ends, so they are made
        one time only."""
        f = self.file(self.current)
        if f is None:
            return
        rec = self._record(f)
        rec["all"], rec["nodes"] = action, {}
        self._save()
        self.rebuild(f["file"])             # the rows follow when the build ends

    def clear_choices(self):
        """Use the default of each decision of the file on the page again."""
        f = self.file(self.current)
        if f is None or not self.choices.pop(f["file"], None):
            return
        self._save()
        self.rebuild(f["file"])             # the rows follow when the build ends

    # --- saved choices -------------------------------------------------------------------

    def _load_saved(self):
        """Use the choices saved for the versions in the version boxes, when the page
        has none of its own for them (merge_cli.saved_choices)."""
        versions = (self.old_tag(), self.new_tag())
        if self._versions_of_choices != versions and self.win.store is not None:
            self.choices = dict(merge_cli.saved_choices(self.win.store, *versions)["files"])
            self._versions_of_choices = versions

    def _save(self):
        """Keep the page's choices on disk, so they stay after the app closes."""
        if self.win.store is None or self._versions_of_choices is None:
            return
        try:
            merge_cli.save_choices(self.win.store, *self._versions_of_choices, self.choices)
        except OSError as e:
            self.win._status(f"Could not save your choices: {e}")

    def clear_saved(self):
        """Delete the choices for these versions in every file, on the page and on disk."""
        if not self.choices:
            return
        if QMessageBox.question(self, "Clear saved choices",
                                f"Delete your choices in {len(self.choices)} file"
                                f"{'' if len(self.choices) == 1 else 's'}? The defaults of the plan apply "
                                "again.") != QMessageBox.StandardButton.Yes:
            return
        self.clear_all()

    def clear_all(self):
        rels, self.choices = list(self.choices), {}
        self._save()
        for rel in rels:
            self.rebuild(rel)

    # --- your own text --------------------------------------------------------------------

    def own_block(self, d):
        """What the editor of your own text for decision `d` shows (merge_rows.own_block).
        It runs in the merge worker and waits for it."""
        f = self.file(self.current)
        return self._work("own_block", self._key(f), f, d)

    def write_own(self, d, body, block=None):
        """Record your own text `body`, in the editor's form, for the block that holds
        decision `d` (`block`: own_block's result). It takes the place of any own text
        that overlaps that block. Returns why the text cannot go in, or ""."""
        block = block or self.own_block(d)
        raw = from_body(body, tuple(block["frame"]))
        why = block["problem"] or merge_cli.own_problem(raw)
        if why:
            return why
        self._set_own(d, block["span"], raw, block["edges"])
        return ""

    def remove_own(self, d, block=None):
        """Delete your own text for the block that holds decision `d`."""
        block = block or self.own_block(d)
        self._set_own(d, block["span"], None)

    def _set_own(self, d, span, raw, edges=None):
        f = self.file(self.current)
        if f is None:
            return
        rec = self._record(f)
        if d.get("address"):
            self._touched.add(merge_cli.choice_key(d))
        s, e = span
        rec["own"] = [o for o in rec.get("own") or [] if not (o["span"][0] < e and s < o["span"][1])
                      and o["span"] != [s, e]]
        if raw is not None:
            rec["own"] = sorted(rec["own"] + [{"span": [s, e], "text": raw, "edges": list(edges)}],
                                key=lambda o: o["span"])
        self._save()
        self.rebuild(f["file"])             # the rows follow when the build ends

    def edit_text(self, d):
        """A dialog to write your own text for the block that holds decision `d`. The
        merge worker finds the block first."""
        f = self.file(self.current)
        if f is None:
            return
        key = self._key(f)
        self.win._in_background("merge own text", lambda: self._work("own_block", key, f, d),
                                lambda block: self._own_dialog(d, block),
                                lambda: self.win._status("Could not read the block. The error is in the Log."))

    def _own_dialog(self, d, block):
        first, last = block["lines"]
        where = f"lines {first} to {last}" if last > first else f"line {first}"
        dlg = QDialog(self)
        dlg.setWindowTitle("Write your own text")
        dlg.resize(820, 600)
        v = QVBoxLayout(dlg)
        head = QLabel(f"{self.where(d)} · {where}")
        head.setStyleSheet(f"font-family: '{self.mono}';")
        v.addWidget(head)
        hint = QLabel(f"Your text takes the place of {where} of your file, with each change in them. "
                      "Apply file writes it as it stands.", objectName="hint")
        hint.setWordWrap(True)
        v.addWidget(hint)
        if block["problem"]:
            warn = QLabel(f"You cannot save a text here: {block['problem']}.", objectName="hint")
            warn.setWordWrap(True)
            warn.setStyleSheet(f"color: {self.C['broken']};")
            v.addWidget(warn)
        edit = QPlainTextEdit(block["own"] if block["own"] is not None else block["result"])
        edit.setTabStopDistance(4 * edit.fontMetrics().horizontalAdvance(" "))
        edit.setStyleSheet(f"font-family: '{self.mono}';")
        v.addWidget(edit, 1)
        bar = QHBoxLayout()
        starts = (("Start from mine", block["mine"], "Your lines as your file holds them."),
                  ("Start from vanilla", block["vanilla"], "Your lines with each vanilla change in them."),
                  ("Start from the result", block["result"], "The lines as Apply file writes them with your "
                                                              "other choices."))
        for label, text, tip in starts:
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(lambda _c=False, t=text: edit.setPlainText(t))
            bar.addWidget(b)
        bar.addStretch(1)
        if block["own"] is not None:
            remove = QPushButton("Remove my text")
            remove.setToolTip("Use your choices for the changes in this block again.")
            remove.clicked.connect(lambda: (self.remove_own(d, block), dlg.reject()))
            bar.addWidget(remove)
        cancel, save = QPushButton("Cancel"), QPushButton("Save")
        save.setProperty("kind", "primary")
        save.setEnabled(not block["problem"])
        cancel.clicked.connect(dlg.reject)

        def accept():
            why = self.write_own(d, edit.toPlainText(), block)
            if why:
                QMessageBox.warning(dlg, "Write your own text", f"Your text cannot go in: {why}.")
            else:
                dlg.accept()
        save.clicked.connect(accept)
        bar.addWidget(cancel)
        bar.addWidget(save)
        v.addLayout(bar)
        dlg.exec()

    def rebuild(self, rel):
        """Make plan file `rel` again from the plan and the user's choices, with
        merge_cli.build in the merge worker process (merge_rows.build). The build merges
        only the copies whose choices changed (its memo), and it runs no command and
        reads no file. Only the result of the last click shows."""
        f = self.file(rel)
        if f is None:
            return
        rec = self.choices.get(rel)
        choices = {rel: dict({"all": rec.get("all"), "nodes": dict(rec["nodes"])},
                             **({"own": list(rec["own"])} if rec.get("own") else {}))} if rec else {}
        self.building.add(rel)

        def done(out):
            self.building.discard(rel)
            if self.plan is not None and self.file(rel) is f:
                work = dict(f, **out[0])
                self.plan["files"] = [work if x is f else x for x in self.plan["files"]]
            self._refresh_file(rel)

        def failed():
            self.building.discard(rel)
            self._refresh_file(rel)
            self.win._status(f"Could not update {rel}. The error is in the Log.")
        key = self._key(f)
        self.win._in_background(f"merge build {rel}", lambda: self._work("build", key, f, choices), done, failed)
        QTimer.singleShot(self.UPDATING_DELAY, lambda: self._show_updating(rel))

    def _show_updating(self, rel):
        """Say that file `rel` updates, when its build did not end yet."""
        f = self.file(rel)
        if rel not in self.building or f is None:
            return
        self._refresh_file_item(f)
        if rel == self.current:
            self._show_header(f)

    def _plan_of(self, rels):
        """A temporary plan file that holds the files `rels`. Each file holds `expect`,
        the sha of the merged text that the page shows, so --apply writes only that text."""
        path = self._temp("plan")
        part = dict(self.plan, files=[dict(f, expect=merge_cli._sha(f["merged"]))
                                      for f in self.plan["files"] if f["file"] in rels])
        path.write_text(json.dumps(part, ensure_ascii=False), encoding="utf-8")
        return path

    def _apply(self, rels, text):
        """Run `merge --apply` for the files `rels`. With the user's choices, the command
        builds each file again from the plan with them, as the page did."""
        path, choices = self._plan_of(set(rels)), self._choices_file(set(rels))
        argv = ["--apply", str(path)] + (["--choices", str(choices)] if choices else [])
        self._run(argv, lambda _code: (self._drop(path, choices), self._after_apply(rels)), text)

    def apply_file(self, rel):
        if self._apply_block(rel):
            return
        self._apply([rel], f"Writing {rel}…")

    def apply_ready(self):
        ready = [f["file"] for f in self.plan["files"] if not self._apply_block(f["file"])] if self.plan else []
        if ready:
            self._apply(ready, f"Writing {len(ready)} files…")

    def _after_apply(self, rels):
        """Take each file that now holds its merged text off the list, with its choices."""
        done = []
        for rel in rels:
            f = self.file(rel)
            path = self.win.mod_root / rel
            if f is not None and path.is_file() and merge_cli.read_mod_file(path)[0] == f["merged"]:
                done.append(rel)
        self.plan["files"] = [f for f in self.plan["files"] if f["file"] not in done]
        for rel in done:
            self.choices.pop(rel, None)
        self._save()
        self._show_plan()
        refused = len(rels) - len(done)
        self.win._status(f"Wrote {len(done)} file{'' if len(done) == 1 else 's'}"
                         + (f"; {refused} refused, see the Log." if refused else "."))
