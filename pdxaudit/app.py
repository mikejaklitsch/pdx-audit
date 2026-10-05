"""The --display desktop app: run the audits, read the findings and act on them
with buttons.

Runs, commits and orphaned-record removal call the command
line in a background process, so they behave exactly as they do in a terminal.
A run writes its findings to results.json in the per-user data folder, and the
window reads them back. Dismiss and Restore change the findings record
directly, so they apply without a new run."""
import html
import json
import os
import re
import sys
import threading
from pathlib import Path

from PySide6.QtCore import (QEvent, QPoint, QPointF, QProcess, QProcessEnvironment,
                            QPropertyAnimation, QRect, QRectF, QSettings, QSize, Qt, QTimer, Signal)
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetrics, QFontMetricsF, QIcon, QPainter,
                           QPainterPath, QPalette, QPen, QPixmap)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractScrollArea, QApplication, QButtonGroup, QCheckBox, QComboBox,
    QFileDialog, QFormLayout, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSplitter, QStackedWidget, QStyle, QStyledItemDelegate, QTextBrowser, QToolButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import config, highlight, results
from .report import KIND
from .store import open_store
from .tracker import get_commits, patch_name as tracker_patch_name

HERE = Path(__file__).parent
ROLE = Qt.ItemDataRole.UserRole
SEV_ORDER = ("broken", "stale", "review")
AUDIT_CHIPS = (("override", "Override"), ("deps", "Dependency"), ("gui", "GUI"),
               ("files", "File copy"), ("loc", "Localization"), ("dupes", "Duplicate"))
CLI_TO_AUDIT = {"overrides": "override", "deps": "deps", "gui": "gui", "files": "files", "loc": "loc",
                "dupes": "dupes"}
AUDIT_LABEL = dict(AUDIT_CHIPS)

C = {"bg": "#14161a", "rail": "#101215", "bar": "#171a1f", "list": "#16191d", "code": "#111317",
     "line": "#22262d", "field": "#1a1d22", "edge": "#2a2f37", "edge2": "#353b45",
     "popup": "#1d2026", "hover": "#1b1f25",
     "text": "#e4e7ec", "text2": "#c7ccd5", "soft": "#aab1bd", "muted": "#8b93a1",
     "faint": "#6d7584", "dim": "#5d6573", "gutter": "#3c424c",
     "accent": "#5fb3c8", "accent_ink": "#0d1a1e", "accent_bg": "#1f3a41",
     "accent_edge": "#2d5660", "accent_text": "#9ad8e6", "added": "#7fd39a",
     "broken": "#f06b5f", "stale": "#ff9a52", "review": "#7aa2f7", "danger": "#d8483e"}
TINT = {"stale": QColor(255, 154, 82, 43), "review": QColor(122, 162, 247, 40)}
# The words that differ between your line and vanilla's, drawn over the tint. Neutral,
# so script coloured like the severity (orange keys on an orange row) stays readable.
EMPH = {"stale": QColor(255, 255, 255, 34), "review": QColor(255, 255, 255, 34)}
# Gutter characters say what happened to a line; the bar and tint say how urgent it is.
SIGN = {"-": "−", "+": "+"}
SIGN_MEANING = {"-": "your copy's line",
                "+": "vanilla's current line"}
# What a mark means, by its row's cause (results._cause).
LEGEND = {("stale", "inject"): "vanilla changed a key you inject",
          ("stale", None): "vanilla changed a block you also edited",
          ("review", "conflict"): "vanilla changed a statement you also changed",
          ("review", None): "vanilla changed a block you didn’t edit"}
# What a side-by-side line's colour means (results.side_rows); {base} is the version the copy matches.
DIFF = {"add": "added since {base}", "del": "deleted since {base}"}
DIFF_SIGN = {"add": "+", "del": "−"}
# A finding's severity as the summary prints it.
ICON = {"stale": "✗", "review": "⚠"}

SANS, MONO = "Segoe UI", "Consolas"


def _settings():
    """The app's remembered preferences, kept per user by Qt."""
    return QSettings("pdx-audit", "pdx-audit")
_fonts_loaded = False

# The rail's pages, in order: icon, and what the page is for, since it has no labels.
RAIL_PAGES = (
    ("findings", "Findings: what to fix, from the last run"),
    ("dismissed", "Dismissed: findings you hid, and how to bring them back"),
    ("commits", "Tracker: the vanilla commits to compare with, and making a new one"),
    ("output", "Output: the run's report and its log"),
    ("settings", "Settings: where the tracker and the game are"),
)

ICONS = {
    "findings": '<path d="M3 4h10M3 8h10M3 12h6"/>',
    "dismissed": '<path d="M2.5 5h11v8h-11z"/><path d="M2 2.5h12V5H2z"/><path d="M6.5 8h3"/>',
    "commits": '<path d="M8 2v3M8 11v3"/><circle cx="8" cy="8" r="3"/>',
    "output": '<path d="M2.5 3.5h11v9h-11z"/><path d="M5 7l2 1.5L5 10M8.5 10.5h2.5"/>',
    "folders": '<path d="M2 4.5h4.5l1.5 1.5H14v6.5H2z"/>',
    "files": '<path d="M3 4h10M3 8h10M3 12h10"/>',
    "search": '<circle cx="7" cy="7" r="4.2"/><path d="M10.2 10.2l3 3"/>',
    "play": '<path d="M5 3.5l7 4.5-7 4.5z" fill="currentColor"/>',
    "chevron": '<path d="M4.5 6.5L8 10l3.5-3.5"/>',
    "settings": '<path d="M2.5 5.5h11M2.5 10.5h11"/><circle cx="6" cy="5.5" r="1.7"/>'
                '<circle cx="10.5" cy="10.5" r="1.7"/>',
}


def _load_fonts():
    global SANS, MONO, _fonts_loaded
    if _fonts_loaded:
        return
    _fonts_loaded = True
    for name, role in (("Geist[wght].ttf", "sans"), ("GeistMono[wght].ttf", "mono")):
        fid = QFontDatabase.addApplicationFont(str(HERE / "fonts" / name))
        families = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
        if families and role == "sans":
            SANS = families[0]
        elif families:
            MONO = families[0]


def font(px, mono=False, weight=QFont.Weight.Normal, italic=False):
    f = QFont(MONO if mono else SANS)
    f.setPointSizeF(px * 0.75)
    f.setWeight(weight)
    f.setItalic(italic)
    return f


def svg_pixmap(name, color, size):
    body = ICONS[name].replace("currentColor", color)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="none" '
           f'stroke="{color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    QSvgRenderer(svg.encode()).render(p)
    p.end()
    pm.setDevicePixelRatio(2)
    return pm


def svg_icon(name, off, on=None, size=16):
    icon = QIcon()
    icon.addPixmap(svg_pixmap(name, off, size), QIcon.Mode.Normal, QIcon.State.Off)
    icon.addPixmap(svg_pixmap(name, on or off, size), QIcon.Mode.Normal, QIcon.State.On)
    return icon


def _esc(s):
    return html.escape(str(s), quote=False)


def _spaces(s):
    return _esc(s).replace("  ", "&nbsp;&nbsp;")


def _plain(text):
    return html.unescape(re.sub(r"<[^>]+>", " ", text or "").replace("&nbsp;", " "))


def _cap(s):
    return s[:1].upper() + s[1:]


def _worst(recs):
    return next((s for s in SEV_ORDER if any(r["sev"] == s for r in recs)), "review")


def _expand(text):
    return text.replace("\t", "    ")


_code_width = None


def code_width():
    """The width of one character of the code font. The block view draws code on a grid
    of these, and puts the boxes that mark changed words on the same grid, so the two
    must measure it the same way. It is a fraction of a pixel, which the integer
    QFontMetrics rounds away: rounding 7.8 up to 8 moves a box a whole character to the
    right by the end of a long line."""
    global _code_width
    if _code_width is None:
        _code_width = QFontMetricsF(font(12.5, mono=True)).horizontalAdvance("M")
    return _code_width


def _chevron(p, x, cy, down, color):
    pen = QPen(QColor(color), 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    path = QPainterPath()
    if down:
        path.moveTo(x + 1.5, cy - 2)
        path.lineTo(x + 5, cy + 1.5)
        path.lineTo(x + 8.5, cy - 2)
    else:
        path.moveTo(x + 3, cy - 3.5)
        path.lineTo(x + 6.5, cy)
        path.lineTo(x + 3, cy + 3.5)
    p.drawPath(path)


def stylesheet():
    a = (HERE / "assets").as_posix()
    return f"""
    QWidget {{ color: {C['text']}; font-family: "{SANS}"; font-size: 13px; }}
    QMainWindow, #root, #page {{ background: {C['bg']}; }}
    #rail {{ background: {C['rail']}; border-right: 1px solid {C['line']}; }}
    #rail QToolButton {{ border: none; border-radius: 8px; }}
    #rail QToolButton:hover {{ background: {C['field']}; }}
    #rail QToolButton:checked {{ background: {C['accent_bg']}; }}
    #topbar {{ background: {C['bar']}; border-bottom: 1px solid {C['line']}; }}
    #strip, #listHead {{ border-bottom: 1px solid {C['line']}; }}
    #listPanel {{ background: {C['list']}; }}
    #statusbar {{ background: {C['rail']}; border-top: 1px solid {C['line']}; }}
    #statusbar QLabel {{ color: {C['faint']}; font-size: 11px; }}
    #detailHead, #values, #legend {{ border-bottom: 1px solid {C['line']}; }}
    #dismissBar {{ background: {C['bar']}; border-top: 1px solid {C['line']}; }}
    QSplitter::handle {{ background: {C['line']}; }}
    QLineEdit {{ background: {C['field']}; border: 1px solid {C['edge']}; border-radius: 6px;
                 padding: 0 8px; min-height: 26px; color: {C['text2']};
                 selection-background-color: {C['accent_edge']}; }}
    QLineEdit:focus {{ border-color: {C['accent_edge']}; }}
    QLineEdit:disabled {{ color: {C['dim']}; }}
    QComboBox {{ background: #23272e; border: 1px solid {C['edge2']}; border-radius: 6px;
                 padding: 0 10px; min-height: 28px; color: #d4d8df; }}
    QComboBox:hover {{ border-color: #454c57; }}
    QComboBox:disabled {{ color: #4f5663; border-color: {C['edge']}; }}
    QComboBox::drop-down {{ border: none; width: 24px; }}
    QComboBox::down-arrow {{ image: url({a}/chevron-down.svg); width: 12px; height: 12px; }}
    QComboBox QAbstractItemView {{ background: {C['popup']}; border: 1px solid #2f343d; color: #d4d8df;
                                   selection-background-color: {C['accent_bg']}; outline: none; padding: 4px; }}
    QCheckBox {{ spacing: 9px; color: {C['text2']}; }}
    QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid #4a515c; border-radius: 4px; background: transparent; }}
    QCheckBox::indicator:checked {{ background: {C['accent']}; border-color: {C['accent']}; image: url({a}/check.svg); }}
    QPushButton {{ background: transparent; border: 1px solid {C['edge2']}; border-radius: 6px; padding: 0 12px;
                   min-height: 28px; color: #d4d8df; font-weight: 600; font-size: 12.5px; }}
    QPushButton:hover {{ background: #1f2329; }}
    QPushButton:disabled {{ color: #4f5663; border-color: {C['edge']}; }}
    QPushButton[kind="primary"] {{ background: {C['accent']}; border: none; color: {C['accent_ink']}; font-weight: 700; }}
    QPushButton[kind="primary"]:hover {{ background: #74c2d5; }}
    QPushButton[kind="primary"]:disabled {{ background: #2c4a52; color: #5f7c83; }}
    QPushButton[kind="runMain"] {{ background: {C['accent']}; border: none; color: {C['accent_ink']}; font-weight: 700;
                                   border-top-right-radius: 0; border-bottom-right-radius: 0; padding: 0 12px 0 12px; }}
    QPushButton[kind="runMain"]:hover {{ background: #74c2d5; }}
    QPushButton[kind="runArrow"] {{ background: #4c9fb3; border: none; border-left: 1px solid #3f8799;
                                    border-top-left-radius: 0; border-bottom-left-radius: 0; padding: 0; min-width: 28px; }}
    QPushButton[kind="runArrow"]:hover {{ background: #5aaec1; }}
    QPushButton[kind="runMain"]:disabled, QPushButton[kind="runArrow"]:disabled {{ background: #2c4a52; color: #5f7c83; }}
    QPushButton[kind="danger"] {{ background: {C['danger']}; border: none; color: #ffffff; padding: 0 14px; }}
    QPushButton[kind="danger"]:hover {{ background: #e25a50; }}
    QPushButton[kind="danger"]:disabled {{ background: #3a2624; color: #7d6a68; }}
    QPushButton[kind="ghost"] {{ border: none; color: {C['muted']}; }}
    QPushButton[kind="ghost"]:hover {{ color: {C['text']}; background: transparent; }}
    #windowPill {{ background: {C['field']}; border: 1px solid {C['edge']}; border-radius: 6px; padding: 0 10px;
                   min-height: 26px; color: {C['text2']}; font-family: "{MONO}"; font-size: 12px; }}
    #windowPill:hover {{ border-color: #3a404a; }}
    #seg {{ border: 1px solid {C['edge']}; border-radius: 7px; }}
    #seg QToolButton {{ border: none; border-radius: 5px; padding: 0 9px; min-height: 22px; color: {C['muted']}; font-size: 12px; }}
    #seg QToolButton:checked {{ background: {C['accent_bg']}; color: {C['accent_text']}; }}
    #popup {{ background: {C['popup']}; border: 1px solid #2f343d; border-radius: 8px; }}
    #popup QLabel {{ color: {C['muted']}; font-size: 12px; }}
    QTreeWidget, QListWidget {{ background: {C['list']}; border: none; outline: none; }}
    #dataTree {{ background: {C['bar']}; }}
    #dataTree::item {{ padding: 4px 0; }}
    #dataTree::item:selected, QListWidget::item:selected {{ background: {C['accent_bg']}; color: #ffffff; }}
    QHeaderView::section {{ background: {C['bar']}; color: {C['faint']}; border: none; border-bottom: 1px solid {C['line']};
                            padding: 6px 8px; font-size: 11px; }}
    QPlainTextEdit, QTextBrowser {{ background: {C['code']}; border: none; color: {C['text2']};
                                    selection-background-color: {C['accent_edge']}; }}
    QPlainTextEdit {{ font-family: "{MONO}"; font-size: 12.5px; }}
    #card {{ background: {C['bar']}; border: 1px solid {C['line']}; border-radius: 10px; }}
    #cardTitle {{ font-size: 13px; font-weight: 600; }}
    #hint {{ color: {C['muted']}; font-size: 12px; }}
    QToolTip {{ background: {C['popup']}; color: {C['text']}; border: 1px solid #2f343d; padding: 4px 6px; }}
    QMessageBox {{ background: {C['bar']}; }}
    QProgressBar {{ background: {C['line']}; border: none; border-radius: 2px; }}
    QProgressBar::chunk {{ background: {C['accent']}; border-radius: 2px; }}
    """


# --- small widgets --------------------------------------------------------------

class OverlayScrollBar(QWidget):
    """A thin rounded thumb drawn over the edge of a scroll area, shown while the
    pointer is over the area or it scrolls, and faded out a second after."""

    THICK, MARGIN = 7, 2

    def __init__(self, area, orientation=Qt.Orientation.Vertical):
        super().__init__(area)
        self.area = area
        self.vertical = orientation == Qt.Orientation.Vertical
        self.bar = area.verticalScrollBar() if self.vertical else area.horizontalScrollBar()
        policy = Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        (area.setVerticalScrollBarPolicy if self.vertical else area.setHorizontalScrollBarPolicy)(policy)
        self.hovered = self.dragging = False
        self._grab = None
        self.effect = QGraphicsOpacityEffect(self)
        self.effect.setOpacity(0.0)
        self.setGraphicsEffect(self.effect)
        self.anim = QPropertyAnimation(self.effect, b"opacity", self)
        self.anim.setDuration(180)
        self.anim.finished.connect(self._settle)
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.setInterval(1000)
        self.hide_timer.timeout.connect(lambda: self._fade(0.0))
        self.bar.valueChanged.connect(self._poke)
        self.bar.rangeChanged.connect(lambda *_: (self._place(), self.update()))
        area.installEventFilter(self)
        area.viewport().installEventFilter(self)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._place()

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t in (QEvent.Type.Resize, QEvent.Type.Show):
            self._place()
        elif t == QEvent.Type.Enter:
            self.hide_timer.stop()
            self._fade(1.0)
        elif t == QEvent.Type.Leave and not self.dragging:
            self.hide_timer.start()
        return False

    def _poke(self):
        self._fade(1.0)
        if not self.area.underMouse():
            self.hide_timer.start()
        self.update()

    def _fade(self, to):
        if self.bar.maximum() <= self.bar.minimum():
            to = 0.0
        if to > 0:
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.anim.stop()
        self.anim.setStartValue(self.effect.opacity())
        self.anim.setEndValue(to)
        self.anim.start()

    def _settle(self):
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, self.effect.opacity() < 0.05)

    def _place(self):
        vp = self.area.viewport().geometry()
        span = self.THICK + self.MARGIN
        if self.vertical:
            self.setGeometry(vp.right() - span + 1, vp.top(), span, vp.height())
        else:
            self.setGeometry(vp.left(), vp.bottom() - span + 1, max(vp.width() - span, 0), span)
        self.raise_()

    def _thumb(self):
        lo, hi, page = self.bar.minimum(), self.bar.maximum(), max(self.bar.pageStep(), 1)
        length = self.height() if self.vertical else self.width()
        if hi <= lo or length <= 0:
            return None
        size = max(28, int(length * page / (hi - lo + page)))
        pos = int((length - size) * (self.bar.value() - lo) / (hi - lo))
        return (QRectF(0, pos, self.THICK, size) if self.vertical
                else QRectF(pos, 0, size, self.THICK))

    def paintEvent(self, _e):
        thumb = self._thumb()
        if thumb is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(154, 163, 178, 140 if (self.hovered or self.dragging) else 87))
        p.drawRoundedRect(thumb, 3.5, 3.5)

    def enterEvent(self, _e):
        self.hovered = True
        self.hide_timer.stop()
        self._fade(1.0)
        self.update()

    def leaveEvent(self, _e):
        self.hovered = False
        self.update()
        if not self.dragging:
            self.hide_timer.start()

    def _pos(self, e):
        return e.position().y() if self.vertical else e.position().x()

    def mousePressEvent(self, e):
        thumb = self._thumb()
        if thumb is None:
            return
        pos = self._pos(e)
        start = thumb.top() if self.vertical else thumb.left()
        size = thumb.height() if self.vertical else thumb.width()
        if start <= pos <= start + size:
            self.dragging = True
            self._grab = (pos, self.bar.value())
        else:
            step = self.bar.pageStep()
            self.bar.setValue(self.bar.value() + (step if pos > start else -step))

    def mouseMoveEvent(self, e):
        if not self.dragging:
            return
        thumb = self._thumb()
        length = self.height() if self.vertical else self.width()
        size = thumb.height() if self.vertical else thumb.width()
        if length - size > 0:
            moved = (self._pos(e) - self._grab[0]) * (self.bar.maximum() - self.bar.minimum()) / (length - size)
            self.bar.setValue(int(self._grab[1] + moved))

    def mouseReleaseEvent(self, _e):
        self.dragging = False
        self.update()
        if not self.underMouse():
            self.hide_timer.start()


class Chip(QPushButton):
    """An audit filter chip: its name and how many findings it has."""

    def __init__(self, label):
        super().__init__()
        self.label, self.count = label, 0
        self.setCheckable(True)
        self.setChecked(True)
        self.setEnabled(False)
        self.setFixedHeight(26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_count(self, n):
        self.count = n
        self.setEnabled(n > 0)
        self.updateGeometry()
        self.update()

    def sizeHint(self):
        w = (QFontMetrics(font(12, weight=QFont.Weight.Medium)).horizontalAdvance(self.label)
             + QFontMetrics(font(12, mono=True)).horizontalAdvance(str(self.count)) + 26)
        return QSize(w, 26)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isChecked() and self.isEnabled()
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(C["accent_edge"] if on else C["edge"]), 1))
        p.setBrush(QColor(C["accent_bg"]) if on else Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, 6, 6)
        name_color = C["accent_text"] if on else (C["faint"] if self.isEnabled() else "#4f5663")
        count_color = "#6fa9b8" if on else (C["dim"] if self.isEnabled() else "#4f5663")
        f = font(12, weight=QFont.Weight.Medium)
        p.setFont(f)
        p.setPen(QColor(name_color))
        x = 10
        p.drawText(QRect(x, 0, self.width(), self.height()), Qt.AlignmentFlag.AlignVCenter, self.label)
        x += QFontMetrics(f).horizontalAdvance(self.label) + 6
        p.setFont(font(12, mono=True))
        p.setPen(QColor(count_color))
        p.drawText(QRect(x, 0, self.width(), self.height()), Qt.AlignmentFlag.AlignVCenter, str(self.count))


class SeverityBar(QWidget):
    def __init__(self):
        super().__init__()
        self.counts = {}
        self.setFixedSize(240, 6)

    def set_counts(self, counts):
        self.counts = counts
        self.update()

    def paintEvent(self, _e):
        total = sum(self.counts.values())
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()), 3, 3)
        p.setClipPath(clip)
        if not total:
            p.fillRect(self.rect(), QColor(C["line"]))
            return
        present = [s for s in SEV_ORDER if self.counts.get(s)]
        width = self.width() - 2 * (len(present) - 1)
        x = 0.0
        for i, s in enumerate(present):
            w = width * self.counts[s] / total
            p.fillRect(QRectF(x, 0, w, self.height()), QColor(C[s]))
            x += w + 2


class TreeDelegate(QStyledItemDelegate):
    """Paints the findings list: section labels, folders and files with their
    count and worst-severity dot, and findings with their id."""

    def __init__(self, tree):
        super().__init__(tree)
        self.tree = tree

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), 28)

    def paint(self, p, option, index):
        info = index.data(ROLE)
        if not info:
            return
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect
        kind = info["type"]
        if kind == "section":
            f = font(10.5, weight=QFont.Weight.DemiBold)
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
            p.setFont(f)
            p.setPen(QColor(C["faint"]))
            p.drawText(r.adjusted(12, 0, -12, 0), Qt.AlignmentFlag.AlignVCenter,
                       f"{info['title'].upper()} · {info['count']}")
            p.restore()
            return
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            p.fillRect(r, QColor(C["accent_bg"]))
            p.setPen(QPen(QColor(C["accent_edge"]), 1))
            p.drawRect(QRectF(r).adjusted(0.5, 0.5, -0.5, -0.5))
        elif option.state & QStyle.StateFlag.State_MouseOver:
            p.fillRect(r, QColor(C["hover"]))
        x = r.left() + 12 + info["depth"] * 16
        cy = r.center().y() + 0.5
        if kind != "leaf":
            _chevron(p, x, cy, self.tree.isExpanded(index), C["faint"])
            x += 16
        else:
            x += 6
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(C[info["sev"]]))
        p.drawEllipse(QPointF(x + 3.5, cy), 3.5, 3.5)
        x += 14
        right = r.right() - 12
        if kind == "leaf":
            rf, rt = font(11.5, mono=True), info["id"]
            rc = C["accent_text"] if selected else C["dim"]
        else:
            rf, rt, rc = font(12), str(info["count"]), C["dim"]
        if rt:
            rw = QFontMetrics(rf).horizontalAdvance(rt)
            p.setFont(rf)
            p.setPen(QColor(rc))
            p.drawText(QRect(right - rw, r.top(), rw, r.height()), Qt.AlignmentFlag.AlignVCenter, rt)
            right -= rw + 12
        avail = max(right - x, 0)
        box = lambda left, width: QRect(int(left), r.top(), int(width), r.height())
        if kind == "leaf":
            f = font(12.5, mono=True)
            p.setFont(f)
            p.setPen(QColor("#ffffff" if selected else C["text"]))
            p.drawText(box(x, avail), Qt.AlignmentFlag.AlignVCenter,
                       QFontMetrics(f).elidedText(info["title"], Qt.TextElideMode.ElideRight, avail))
        elif kind == "folder":
            f = font(12)
            p.setFont(f)
            p.setPen(QColor(C["soft"]))
            p.drawText(box(x, avail), Qt.AlignmentFlag.AlignVCenter,
                       QFontMetrics(f).elidedText(info["title"], Qt.TextElideMode.ElideRight, avail))
        elif not info.get("full"):
            f = font(12, mono=True)
            p.setFont(f)
            p.setPen(QColor(C["text2"]))
            p.drawText(box(x, avail), Qt.AlignmentFlag.AlignVCenter,
                       QFontMetrics(f).elidedText(info["title"], Qt.TextElideMode.ElideRight, avail))
        else:
            f = font(11.5, mono=True)
            fm = QFontMetrics(f)
            folder, _, name = info["title"].rpartition("/")
            folder = folder + "/" if folder else ""
            name_w = fm.horizontalAdvance(name)
            p.setFont(f)
            if avail - name_w < fm.horizontalAdvance("…/") + 2:
                p.setPen(QColor(C["text2"]))
                p.drawText(box(x, avail), Qt.AlignmentFlag.AlignVCenter,
                           fm.elidedText(name, Qt.TextElideMode.ElideRight, avail))
            else:
                shown = fm.elidedText(folder, Qt.TextElideMode.ElideLeft, avail - name_w)
                p.setPen(QColor(C["faint"]))
                p.drawText(box(x, avail), Qt.AlignmentFlag.AlignVCenter, shown)
                p.setPen(QColor(C["text2"]))
                p.drawText(box(x + fm.horizontalAdvance(shown), name_w + 2), Qt.AlignmentFlag.AlignVCenter, name)
        p.restore()


class BlockView(QAbstractScrollArea):
    """The mod's block, one line per row, with vanilla's changes marked on the
    lines they affect: an orange (stale) or yellow (review) gutter bar and tint,
    and a note under the line. Long unchanged stretches fold; click to open."""

    LINE, NOTE, FOLD, HEAD, TOGGLE = 21, 18, 24, 28, 30
    # Where a side-by-side half draws its line number, sign and code, from its left edge.
    SIDE_NUMBER, SIDE_SIGN, SIDE_CODE = 16, 52, 68

    def __init__(self):
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.rows, self.visible, self.footer, self.selected = [], [], [], None
        self.columns, self.wrap = None, False
        self.items, self.content_w, self.content_h = [], 0, 0
        self.viewport().setMouseTracking(True)
        self.vscroll = OverlayScrollBar(self)
        self.hscroll = OverlayScrollBar(self, Qt.Orientation.Horizontal)

    def set_rows(self, rows, footer, selected, columns=None, prepared=None):
        """Show `rows`: block_rows' or source rows, or side_rows' with `columns` naming
        the left and right sides. `prepared` is prepare(rows)'s result when a worker
        thread already made it."""
        rows, visible = prepared or self.prepare(rows)
        self.rows, self.footer, self.selected, self.columns = rows, footer, selected, columns
        self.visible = list(visible)
        self._layout()
        target = next((y for y, _h, row in self.items
                       if (row.get("fid") == selected if selected else row.get("mark"))), None)
        self.horizontalScrollBar().setValue(0)
        self.verticalScrollBar().setValue(max(0, (target or 0) - self.viewport().height() // 3))

    def clear(self):
        self.set_rows([], [], None)

    @staticmethod
    def prepare(rows):
        """(rows, visible): `rows` folded for display, with every row left showing given
        its syntax colours; a fold's rows get theirs when it opens. It uses no Qt, so a
        worker thread can run it for a large block."""
        visible = results.fold_rows(rows)
        BlockView._colour([r for r in visible if "fold" not in r])
        return rows, visible

    @staticmethod
    def _colour(rows):
        """Syntax spans for each code row. The block's own lines are highlighted as
        one sequence so the grammar's state carries across them; a line vanilla
        added, and each duplicate definition, is highlighted on its own."""
        hl = highlight.highlighter()
        for side in ("left", "right"):
            cells = [r[side] for r in rows if r.get(side)]
            for cell, spans in zip(cells, hl.line_spans([_expand(c["text"]) for c in cells])):
                cell["spans"] = spans

        def run(seq):
            for row, spans in zip(seq, hl.line_spans([_expand(r["text"]) for r in seq])):
                row["spans"] = spans

        run([r for r in rows if "text" in r and not r.get("ghost")])
        for r in rows:
            if r.get("ghost"):
                run([r])
            elif "toggle" in r:
                run([c for c in r["rows"] if "text" in c])

    def _draw_code(self, p, spans, x, top, ghost=False, cols=None):
        """Draw a line's syntax spans from x; with `cols`, a line longer than that many
        characters continues on the lines below, starting at x again."""
        if ghost:
            p.setOpacity(0.8)
        start, col = x, 0
        for text, colour, bold, italic in spans:
            f = font(12.5, mono=True, weight=QFont.Weight.Bold if bold else QFont.Weight.Normal,
                     italic=italic or ghost)
            p.setFont(f)
            p.setPen(QColor(colour or C["text"]))
            while text:
                if cols is not None and col >= cols:
                    x, col, top = start, 0, top + self.LINE
                piece = text if cols is None else text[:cols - col]
                p.drawText(QRect(int(x), int(top), 4000, self.LINE), Qt.AlignmentFlag.AlignVCenter, piece)
                # One character is one cell of `code_width()`, which is where _emph puts
                # its boxes. Adding each span's own advance instead drifts away from that
                # grid, by a whole character over a long line.
                x += len(piece) * code_width()
                col += len(piece)
                text = text[len(piece):]
        if ghost:
            p.setOpacity(1.0)

    def _cols(self, pane, code_x):
        """How many characters fit on one line of a pane `pane` wide whose code starts at code_x."""
        return max(20, int((pane - code_x - 12) // code_width()))

    def _emph(self, p, text, spans, x, top, cols, colour):
        """Paint `spans`, character ranges of `text`, behind code drawn at x, following the
        text onto its wrapped lines when `cols` is set."""
        width = code_width()
        for s, e in spans:
            a, b = len(_expand(text[:s])), len(_expand(text[:e]))
            while a < b:
                line, col = divmod(a, cols) if cols else (0, a)
                end = min(b, (line + 1) * cols) if cols else b
                p.fillRect(QRectF(x + col * width, top + line * self.LINE + 2, (end - a) * width, self.LINE - 4),
                           colour)
                a = end

    def set_wrap(self, on):
        """Wrap long lines to the view's width instead of scrolling sideways."""
        self.wrap = on
        self.horizontalScrollBar().setValue(0)
        self._layout()

    def _flat(self):
        for row in self.visible:
            yield row
            if "toggle" in row and row["open"]:
                yield from row["rows"]

    def toggle(self, index):
        toggles = [r for r in self.visible if "toggle" in r]
        toggles[index]["open"] = not toggles[index]["open"]
        self._layout()

    def visible_texts(self):
        return [((row["left"] or {}).get("text"), (row["right"] or {}).get("text")) if "left" in row
                else row.get("toggle", row.get("text")) for _y, _h, row in self.items
                if "toggle" in row or "text" in row or "left" in row]

    def _layout(self):
        code_fm = QFontMetrics(font(12.5, mono=True))
        note_fm = QFontMetrics(font(11.5))
        self.items, y, widest = [], 10, 0
        if self.columns:
            self.items.append((y, self.HEAD, {"columns": self.columns}))
            y += self.HEAD
        width = self.viewport().width()
        side_cols, line_cols = self._cols(width // 2, self.SIDE_CODE), self._cols(width, 62)
        lines = lambda text, cols: max(1, -(-len(_expand(text)) // cols)) if self.wrap else 1
        for row in self._flat():
            if "left" in row:
                cells = [c for c in (row["left"], row["right"]) if c]
                for cell in cells:
                    widest = max(widest, self.SIDE_CODE + code_fm.horizontalAdvance(_expand(cell["text"])) + 20)
                h = self.LINE * max(lines(c["text"], side_cols) for c in cells)
                self.items.append((y, h, row))
                y += h
                continue
            if "toggle" in row:
                self.items.append((y, self.TOGGLE, row))
                widest = max(widest, 60 + note_fm.horizontalAdvance(row["toggle"]) + 40)
                y += self.TOGGLE
                continue
            if "fold" in row:
                self.items.append((y, self.FOLD, row))
                y += self.FOLD
                continue
            h = self.LINE * lines(row["text"], line_cols) + (self.NOTE if row.get("note") else 0)
            widest = max(widest, 62 + code_fm.horizontalAdvance(row["text"].replace("\t", "    ")) + 20,
                         62 + note_fm.horizontalAdvance(row.get("note") or "") + 20)
            self.items.append((y, h, row))
            y += h
        if self.footer:
            y += 12
            for heading, lines in self.footer:
                self.items.append((y, self.HEAD, {"heading": heading}))
                y += self.HEAD
                for line in lines:
                    self.items.append((y, self.LINE, {"footer": line, "spans": highlight.highlighter().line_spans([_expand(line)])[0]}))
                    widest = max(widest, 62 + code_fm.horizontalAdvance(line) + 20)
                    y += self.LINE
        self.content_h, self.content_w = y + 10, 0 if self.wrap else widest
        vp = self.viewport()
        self.verticalScrollBar().setRange(0, max(0, self.content_h - vp.height()))
        self.verticalScrollBar().setPageStep(max(vp.height(), 1))
        self.verticalScrollBar().setSingleStep(self.LINE)
        pane = vp.width() // 2 if self.columns else vp.width()
        self.horizontalScrollBar().setRange(0, max(0, self.content_w - pane))
        self.horizontalScrollBar().setPageStep(max(vp.width(), 1))
        self.horizontalScrollBar().setSingleStep(24)
        vp.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._layout()

    def scrollContentsBy(self, _dx, _dy):
        self.viewport().update()

    def paintEvent(self, _e):
        vp = self.viewport()
        p = QPainter(vp)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(vp.rect(), QColor(C["code"]))
        oy, ox, w = -self.verticalScrollBar().value(), -self.horizontalScrollBar().value(), vp.width()
        code_f, note_f = font(12.5, mono=True), font(11.5)
        for y, h, row in self.items:
            top = y + oy
            if top + h < 0 or top > vp.height():
                continue
            line_rect = lambda left, width=4000: QRect(int(left), int(top), int(width), self.LINE)
            if "fold" in row:
                p.fillRect(QRect(0, int(top), w, h), QColor(C["bar"]))
                _chevron(p, ox + 46, top + h / 2, False, C["faint"])
                p.setFont(font(12))
                p.setPen(QColor(C["faint"]))
                p.drawText(QRect(int(ox + 62), int(top), 4000, h), Qt.AlignmentFlag.AlignVCenter,
                           row.get("label") or f"{len(row['fold'])} unchanged lines")
                continue
            if "toggle" in row:
                p.fillRect(QRect(0, int(top), w, h - 1), QColor(C["bar"]))
                p.fillRect(QRect(0, int(top + h - 1), w, 1), QColor(C["line"]))
                _chevron(p, ox + 14, top + h / 2, row["open"], C["faint"])
                how, _sep, where = row["toggle"].partition(" · ")
                hf = font(12, weight=QFont.Weight.DemiBold)
                p.setFont(hf)
                p.setPen(QColor(C["accent_text"]))
                p.drawText(QRect(int(ox + 32), int(top), 4000, h), Qt.AlignmentFlag.AlignVCenter, how)
                p.setFont(font(12, mono=True))
                p.setPen(QColor(C["soft"]))
                p.drawText(QRect(int(ox + 42 + QFontMetrics(hf).horizontalAdvance(how)), int(top), 4000, h),
                           Qt.AlignmentFlag.AlignVCenter, where)
                continue
            if "columns" in row:
                half = w // 2
                p.setFont(font(12, weight=QFont.Weight.DemiBold))
                p.setPen(QColor(C["muted"]))
                for k, text in enumerate(row["columns"]):
                    p.drawText(QRect(int(k * half + 14), int(top), half - 28, h), Qt.AlignmentFlag.AlignVCenter, text)
                p.fillRect(QRect(half, int(top), 1, h), QColor(C["line"]))
                continue
            if "left" in row:
                self._paint_side(p, row, top, h, ox, w)
                continue
            if "heading" in row:
                p.setFont(font(12, weight=QFont.Weight.DemiBold))
                p.setPen(QColor(C["muted"]))
                p.drawText(QRect(int(ox + 14), int(top), 4000, h), Qt.AlignmentFlag.AlignVCenter, row["heading"])
                continue
            if "footer" in row:
                self._draw_code(p, row["spans"], ox + 62, top)
                continue
            mark = row.get("mark")
            if mark:
                p.fillRect(QRect(0, int(top), w, h), TINT[mark])
                p.fillRect(QRect(0, int(top), 3, h), QColor(C[mark]))
            p.setFont(code_f)
            if row.get("n") is not None:
                p.setPen(QColor(C["muted"] if mark else C["gutter"]))   # readable on the tint
                p.drawText(QRect(int(ox + 8), int(top), 30, self.LINE),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, str(row["n"]))
            if mark and row.get("sign"):
                p.setFont(font(12.5, mono=True, weight=QFont.Weight.Bold))
                p.setPen(QColor(C[mark]))
                p.drawText(line_rect(ox + 46, 14), Qt.AlignmentFlag.AlignVCenter, SIGN[row["sign"]])
            cols = self._cols(w, 62) if self.wrap else None
            if mark and row.get("emph"):
                self._emph(p, row["text"], row["emph"], ox + 62, top, cols, EMPH[mark])
            spans = row.get("spans") or [(_expand(row["text"]), None, False, False)]
            self._draw_code(p, spans, ox + 62, top, ghost=row.get("ghost", False), cols=cols)
            if row.get("note"):
                p.setFont(note_f)
                p.setPen(QColor(C["soft"]))
                p.drawText(QRect(int(ox + 62), int(top + h - self.NOTE - 2), 4000, self.NOTE),
                           Qt.AlignmentFlag.AlignVCenter, row["note"])
        p.setPen(QPen(QColor(C["accent"]), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        for first, last in self.selected_runs():
            p.drawRect(QRectF(0.5, first + oy + 0.5, w - 1, last - first - 1))

    def selected_runs(self):
        """[(top, bottom)] of each run of adjacent rows the selected finding covers, so
        one outline goes around a whole block rather than around each of its lines."""
        runs = []
        for y, h, row in self.items:
            if not self.selected or row.get("fid") != self.selected:
                continue
            if runs and runs[-1][1] == y:
                runs[-1] = (runs[-1][0], y + h)
            else:
                runs.append((y, y + h))
        return runs

    def _paint_side(self, p, row, top, h, ox, w):
        """One side-by-side row: vanilla's line in the left half and the copy's in the
        right, each coloured against the version the copy matches: green for a line that
        side added, red for one it deleted. A finding's first row carries its severity
        icon at the left edge; a blank side is shaded."""
        half, mark = w // 2, row.get("mark")
        code_f = font(12.5, mono=True)
        for k, side in enumerate(("left", "right")):
            x0, cell = k * half, row[side]
            p.save()
            p.setClipRect(QRect(x0, int(top), half, h))
            if cell is None:
                p.fillRect(QRect(x0, int(top), half, h), QColor(C["bar"]))
                p.restore()
                continue
            state = None if cell["quiet"] or cell["state"] == "same" else cell["state"]
            if state:
                colour = QColor(C["added"] if state == "add" else C["broken"])
                p.fillRect(QRect(x0, int(top), half, h), QColor(colour.red(), colour.green(), colour.blue(), 34))
                p.setFont(font(12.5, mono=True, weight=QFont.Weight.Bold))
                p.setPen(colour)
                p.drawText(QRect(x0 + self.SIDE_SIGN, int(top), 14, self.LINE), Qt.AlignmentFlag.AlignVCenter,
                           DIFF_SIGN[state])
            p.setFont(code_f)
            if cell["n"] is not None:
                p.setPen(QColor(C["muted"] if state else C["gutter"]))
                p.drawText(QRect(x0 + self.SIDE_NUMBER, int(top), 32, self.LINE),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, str(cell["n"]))
            p.setClipRect(QRect(x0 + self.SIDE_CODE - 2, int(top), half - self.SIDE_CODE + 2, h))
            cols = self._cols(half, self.SIDE_CODE) if self.wrap else None
            if cell.get("emph"):
                self._emph(p, cell["text"], cell["emph"], x0 + self.SIDE_CODE + ox, top, cols, EMPH["review"])
            spans = cell.get("spans") or [(_expand(cell["text"]), None, False, False)]
            self._draw_code(p, spans, x0 + self.SIDE_CODE + ox, top, cols=cols)
            p.restore()
        if mark and row.get("lead"):
            p.setFont(font(12, weight=QFont.Weight.Bold))
            p.setPen(QColor(C["text2"]))
            p.drawText(QRect(2, int(top), 14, self.LINE), Qt.AlignmentFlag.AlignCenter, ICON[mark])
        p.fillRect(QRect(half, int(top), 1, h), QColor(C["line"]))

    def mousePressEvent(self, e):
        y = e.position().y() + self.verticalScrollBar().value()
        for top, h, row in self.items:
            if "toggle" in row and top <= y < top + h:
                row["open"] = not row["open"]
                self._layout()
                return
            if "fold" in row and top <= y < top + h:
                i = self.visible.index(row)
                self._colour(row["fold"])
                self.visible[i:i + 1] = row["fold"]
                self._layout()
                return


# --- the window -------------------------------------------------------------------

class MainWindow(QMainWindow):
    # Reads that would freeze the window (the mod's files and git history on a
    # mounted drive) run on a worker thread and hand their result back here.
    _background_done = Signal(object, object)

    def __init__(self, mod_root, vanilla_repo, options, autorun=True):
        super().__init__()
        self.background_jobs = 0
        self._generations = {}
        self._prepared = {}      # (block id, side by side, flatten) -> BlockView.prepare's result
        self._store_waiters = []
        self.setting_buttons = []
        self._background_done.connect(self._apply_background, Qt.ConnectionType.QueuedConnection)
        _load_fonts()
        app = QApplication.instance()
        app.setStyleSheet(stylesheet())
        self.mod_root = Path(mod_root)
        # The tracker can be absent: the app opens on its Settings page to be pointed at
        # one, or the Tracker page makes the first commit, which creates it.
        self.vanilla_repo = str(vanilla_repo) if vanilla_repo else ""
        self.tracker_override = (options or {}).get("tracker_override") or None
        self.missing_tracker = (options or {}).get("missing_tracker") or None
        self.store, _err = open_store(self.mod_root)
        self.payload = None
        self.records = []
        self.current = None
        self.list_mode = "folders"
        self.process = None
        self.last_line = ""
        self.log_fresh = False
        self.run_banners = []
        self.orphans = []
        self.commits = []
        self._items, self._sections, self._titles = [], [], []
        self.targets = {}
        self.gui_blocks = []
        self.setWindowTitle(f"pdx-audit: {self.mod_root.name}")

        root = QWidget(objectName="root")
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_rail())
        column = QVBoxLayout()
        column.setSpacing(0)
        column.addWidget(self._build_topbar())
        self.banner_box = QVBoxLayout()
        self.banner_box.setContentsMargins(14, 0, 14, 0)
        column.addLayout(self.banner_box)
        column.addWidget(self._build_strip())
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_findings_page())
        self.pages.addWidget(self._build_dismissed_page())
        self.pages.addWidget(self._build_tracker_page())
        self.pages.addWidget(self._build_output_page())
        self.pages.addWidget(self._build_settings_page())
        column.addWidget(self.pages, 1)
        column.addWidget(self._build_statusbar())
        outer.addLayout(column, 1)
        self.setCentralWidget(root)

        self.load_commits()
        self.refresh_settings()
        self.refresh_store_views()
        options = options or {}
        self.apply_options(options)
        self._set_busy(False)
        self._show_record(None)
        if not self.vanilla_repo:
            # Nothing to compare against yet, so the window opens where that is fixed.
            self._go_to_page(4)
            if self.missing_tracker:
                self.log.appendPlainText(self.missing_tracker)
            autorun = False
        # The Run menu's categories and blocks come from reading every mod script,
        # so they load off the UI thread. A category or block asked for at launch
        # can only be chosen once they exist, so that choice and its run wait.
        waits = bool(options.get("category") or options.get("block"))
        self._in_background("mod's overrides", lambda: (results.override_targets(self.mod_root),
                                                        results.gui_targets(self.mod_root)),
                            lambda found: self._show_targets(found[0], options if waits else None,
                                                             autorun and waits, gui=found[1]),
                            failed=lambda: self._show_targets({}, options if waits else None,
                                                              autorun and waits))
        if autorun and not waits:
            self.start_run()

    def _show_targets(self, targets, options=None, run=False, gui=()):
        """Fill the Run menu's categories and blocks, then apply a category or block
        asked for at launch and start the run that waited for it. `gui` names the
        mod's GUI templates and types, listed as blocks under all categories."""
        self.targets = targets
        self.gui_blocks = list(gui)
        keep = self.category_combo.currentData()
        self.category_combo.blockSignals(True)
        self.category_combo.clear()
        self.category_combo.addItem("All categories", "")
        for category in targets:
            self.category_combo.addItem(category, category)
        i = self.category_combo.findData(keep)
        self.category_combo.setCurrentIndex(i if i >= 0 else 0)
        self.category_combo.blockSignals(False)
        self._fill_blocks()
        if options:
            self.set_run_choice(category=options.get("category") or "",
                                block=options.get("block") or "")
        if run:
            self.start_run()

    # --- layout -------------------------------------------------------------------

    def _build_rail(self):
        rail = QFrame(objectName="rail")
        rail.setFixedWidth(52)
        lay = QVBoxLayout(rail)
        lay.setContentsMargins(8, 12, 8, 12)
        lay.setSpacing(6)
        self.rail_group = QButtonGroup(self)
        for i, (icon, tip) in enumerate(RAIL_PAGES):
            b = QToolButton()
            b.setCheckable(True)
            b.setFixedSize(36, 36)
            b.setIcon(svg_icon(icon, C["faint"], C["accent_text"], 18))
            b.setIconSize(QSize(18, 18))
            b.setToolTip(tip)
            b.setChecked(i == 0)
            self.rail_group.addButton(b, i)
            lay.addWidget(b)
        self.rail_group.idClicked.connect(self._open_page)
        lay.addStretch(1)
        return rail

    def _build_topbar(self):
        bar = QFrame(objectName="topbar")
        bar.setFixedHeight(48)
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 0, 14, 0)
        h.setSpacing(8)
        name = QLabel(self.mod_root.name)
        name.setFont(font(13, weight=QFont.Weight.DemiBold))
        h.addWidget(name)
        h.addSpacing(8)
        self.chips = {}
        for tag, label in AUDIT_CHIPS:
            chip = Chip(label)
            chip.setToolTip(f"Show or hide {label.lower()} findings")
            chip.toggled.connect(lambda _on: self._rebuild_tree())
            self.chips[tag] = chip
            h.addWidget(chip)
        sep = QFrame()
        sep.setFixedSize(1, 20)
        sep.setStyleSheet(f"background: {C['edge']};")
        h.addSpacing(4)
        h.addWidget(sep)
        h.addSpacing(4)
        self.window_button = QPushButton(objectName="windowPill")
        self.window_button.setIcon(svg_icon("chevron", C["faint"]))
        self.window_button.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.window_button.clicked.connect(self._open_window_popup)
        h.addWidget(self.window_button)
        h.addStretch(1)
        self.changed_label = QLabel()
        self.changed_label.setStyleSheet(f"color: {C['accent_text']}; font-size: 12px;")
        self.changed_label.hide()
        h.addWidget(self.changed_label)
        h.addSpacing(6)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop_job)
        h.addWidget(self.stop_button)
        run = QHBoxLayout()
        run.setSpacing(0)
        self.run_button = QPushButton(" Run audits")
        self.run_button.setProperty("kind", "runMain")
        self.run_button.setIcon(svg_icon("play", C["accent_ink"], size=12))
        self.run_button.setFixedHeight(30)
        self.run_button.clicked.connect(self.start_run)
        self.run_arrow = QPushButton()
        self.run_arrow.setProperty("kind", "runArrow")
        self.run_arrow.setIcon(svg_icon("chevron", C["accent_ink"], size=14))
        self.run_arrow.setFixedSize(28, 30)
        self.run_arrow.setToolTip("Block, category and window options for the next run")
        self.run_arrow.clicked.connect(self._open_run_popup)
        run.addWidget(self.run_button)
        run.addWidget(self.run_arrow)
        h.addLayout(run)
        self._build_popups()
        return bar

    def _popup(self):
        pop = QFrame(self, Qt.WindowType.Popup)
        pop.setObjectName("popup")
        pop.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        return pop

    def _build_popups(self):
        self.window_popup = self._popup()
        form = QFormLayout(self.window_popup)
        form.setContentsMargins(14, 14, 14, 14)
        form.setSpacing(10)
        self.old_combo, self.new_combo = QComboBox(), QComboBox()
        for combo in (self.old_combo, self.new_combo):
            combo.setMinimumWidth(220)
            combo.currentIndexChanged.connect(lambda _i: self._update_window_label())
        form.addRow("Compare", self.old_combo)
        form.addRow("with", self.new_combo)

        self.run_popup = self._popup()
        self.run_popup.setFixedWidth(300)
        lay = QVBoxLayout(self.run_popup)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(6)
        lay.addWidget(QLabel("Category"))
        self.category_combo = QComboBox()
        self.category_combo.addItem("All categories", "")
        for category in self.targets:
            self.category_combo.addItem(category, category)
        self.category_combo.currentIndexChanged.connect(lambda _i: self._fill_blocks())
        lay.addWidget(self.category_combo)
        lay.addSpacing(6)
        lay.addWidget(QLabel("Block"))
        self.block_combo = QComboBox()
        self.block_combo.setMaxVisibleItems(18)
        lay.addWidget(self.block_combo)
        lay.addSpacing(6)
        self.full_box = QCheckBox("Compare from the oldest commit")
        self.full_box.setToolTip("Every audit over the whole history, not just the last patch: "
                                 "the thorough, slow run")
        self.full_box.toggled.connect(lambda _on: self._update_window_label())
        lay.addWidget(self.full_box)
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {C['edge']};")
        lay.addSpacing(6)
        lay.addWidget(line)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        reset = QPushButton("Reset")
        reset.setProperty("kind", "ghost")
        reset.clicked.connect(lambda: self.set_run_choice(category="", block="", full=False))
        go = QPushButton("Run with these")
        go.setProperty("kind", "primary")
        go.clicked.connect(lambda: (self.run_popup.hide(), self.start_run()))
        buttons.addWidget(reset)
        buttons.addWidget(go)
        lay.addLayout(buttons)
        self._fill_blocks()

    def _open_window_popup(self):
        self.window_popup.adjustSize()
        self.window_popup.move(self.window_button.mapToGlobal(QPoint(0, self.window_button.height() + 4)))
        self.window_popup.show()

    def _open_run_popup(self):
        self.run_popup.adjustSize()
        corner = self.run_arrow.mapToGlobal(QPoint(self.run_arrow.width(), self.run_arrow.height() + 4))
        self.run_popup.move(corner.x() - self.run_popup.width(), corner.y())
        self.run_popup.show()

    def _build_strip(self):
        strip = QFrame(objectName="strip")
        strip.setFixedHeight(40)
        h = QHBoxLayout(strip)
        h.setContentsMargins(14, 0, 14, 0)
        h.setSpacing(14)
        self.sev_bar = SeverityBar()
        h.addWidget(self.sev_bar)
        self.counts_label = QLabel()
        self.counts_label.setStyleSheet(f"color: {C['muted']}; font-size: 12px;")
        h.addWidget(self.counts_label)
        self.extra_label = QLabel()
        self.extra_label.setStyleSheet(f"color: {C['dim']}; font-size: 12px;")
        h.addWidget(self.extra_label)
        h.addStretch(1)
        return strip

    def _build_findings_page(self):
        split = QSplitter()
        split.setHandleWidth(1)
        split.setChildrenCollapsible(False)

        panel = QFrame(objectName="listPanel")
        panel.setMinimumWidth(260)
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        head = QFrame(objectName="listHead")
        head.setFixedHeight(40)
        hh = QHBoxLayout(head)
        hh.setContentsMargins(12, 0, 12, 0)
        hh.setSpacing(8)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter by name, file or id")
        self.filter_edit.addAction(svg_icon("search", C["faint"], size=14), QLineEdit.ActionPosition.LeadingPosition)
        self.filter_edit.textChanged.connect(lambda _t: self._rebuild_tree())
        hh.addWidget(self.filter_edit, 1)
        seg = QFrame(objectName="seg")
        sh = QHBoxLayout(seg)
        sh.setContentsMargins(2, 2, 2, 2)
        sh.setSpacing(2)
        self.mode_group = QButtonGroup(self)
        for mode, label in (("folders", "Folders"), ("files", "Files")):
            b = QToolButton()
            b.setText(label)
            b.setCheckable(True)
            b.setChecked(mode == "folders")
            b.setIcon(svg_icon(mode, C["muted"], C["accent_text"], 13))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            b.clicked.connect(lambda _c=False, m=mode: self.set_list_mode(m))
            self.mode_group.addButton(b)
            sh.addWidget(b)
        hh.addWidget(seg)
        v.addWidget(head)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(0)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.setMouseTracking(True)
        self.tree.setItemDelegate(TreeDelegate(self.tree))
        self.tree.itemClicked.connect(self._toggle_item)
        self.tree.currentItemChanged.connect(lambda item, _prev: self._show_item(item))
        OverlayScrollBar(self.tree)
        v.addWidget(self.tree, 1)
        split.addWidget(panel)
        split.addWidget(self._build_detail())
        split.setSizes([560, 820])
        split.setStretchFactor(1, 1)
        return split

    def _build_detail(self):
        detail = QWidget()
        v = QVBoxLayout(detail)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        head = QFrame(objectName="detailHead")
        hv = QVBoxLayout(head)
        hv.setContentsMargins(22, 18, 22, 14)
        hv.setSpacing(8)
        meta = QHBoxLayout()
        meta.setSpacing(10)
        self.meta_label = QLabel()
        self.meta_label.setStyleSheet(f"color: {C['muted']}; font-size: 11.5px;")
        self.meta_id = QLabel()
        self.meta_id.setStyleSheet(f"color: {C['faint']}; font-family: '{MONO}'; font-size: 11.5px;")
        self.meta_id.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        meta.addWidget(self.meta_label)
        meta.addStretch(1)
        meta.addWidget(self.meta_id)
        hv.addLayout(meta)
        self.title_label = QLabel()
        self.title_label.setStyleSheet(f"font-family: '{MONO}'; font-size: 18px; font-weight: 600;")
        self.title_label.setWordWrap(True)
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        hv.addWidget(self.title_label)
        self.where_label = QLabel()
        self.where_label.setStyleSheet(f"color: {C['faint']}; font-family: '{MONO}'; font-size: 12px;")
        self.where_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        hv.addWidget(self.where_label)
        self.sentence_label = QLabel()
        self.sentence_label.setWordWrap(True)
        self.sentence_label.setStyleSheet(f"color: {C['soft']}; font-size: 13px;")
        hv.addWidget(self.sentence_label)
        v.addWidget(head)

        self.values = QFrame(objectName="values")
        grid = QFormLayout(self.values)
        grid.setContentsMargins(22, 12, 22, 12)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(6)
        self.yours_label, self.vanilla_label = QLabel(), QLabel()
        for lab in (self.yours_label, self.vanilla_label):
            lab.setStyleSheet(f"font-family: '{MONO}'; font-size: 13px;")
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lab.setWordWrap(True)
        self.value_keys = {}
        for text, lab in (("yours", self.yours_label), ("vanilla", self.vanilla_label)):
            key = QLabel(text)
            key.setStyleSheet(f"color: {C['faint']}; font-family: '{MONO}'; font-size: 13px;")
            self.value_keys[text] = key
            grid.addRow(key, lab)
        v.addWidget(self.values)

        self.legend = QFrame(objectName="legend")
        lh = QHBoxLayout(self.legend)
        lh.setContentsMargins(22, 8, 22, 8)
        lh.setSpacing(16)
        title = QLabel("Your block")
        title.setStyleSheet(f"color: {C['text2']}; font-weight: 600; font-size: 11.5px;")
        lh.addWidget(title)
        self.legend_labels, self.sign_labels = {}, {}
        for (sev, cause), text in LEGEND.items():
            lab = QLabel(f'<span style="color:{C[sev]}; font-weight:700;">▍</span>&nbsp;{_esc(text)}')
            lab.setStyleSheet(f"color: {C['muted']}; font-size: 11.5px;")
            self.legend_labels[(sev, cause)] = lab
            lh.addWidget(lab)
        for sign, text in SIGN_MEANING.items():
            lab = QLabel(f'<span style="font-family:\'{MONO}\'; font-weight:700; color:{C["text2"]};">'
                         f'{SIGN[sign]}</span>&nbsp;&nbsp;{_esc(text)}')
            lab.setStyleSheet(f"color: {C['muted']}; font-size: 11.5px;")
            self.sign_labels[sign] = lab
            lh.addWidget(lab)
        self.diff_labels = {}
        for state in DIFF:
            lab = QLabel()
            lab.setStyleSheet(f"color: {C['muted']}; font-size: 11.5px;")
            self.diff_labels[state] = lab
            lh.addWidget(lab)
        lh.addStretch(1)
        self.side_by_side = QCheckBox("Side by side")
        self.side_by_side.setChecked(_settings().value("side_by_side", True, type=bool))
        self.side_by_side.toggled.connect(self._side_by_side_toggled)
        lh.addWidget(self.side_by_side)
        self.flatten = QCheckBox("Flatten")
        self.flatten.setToolTip("Drop indentation and join runs of closing braces into one line")
        self.flatten.setChecked(_settings().value("flatten", False, type=bool))
        self.flatten.toggled.connect(self._flatten_toggled)
        lh.addWidget(self.flatten)
        self.wrap_lines = QCheckBox("Wrap lines")
        self.wrap_lines.setChecked(_settings().value("wrap_lines", False, type=bool))
        self.wrap_lines.toggled.connect(self._wrap_toggled)
        lh.addWidget(self.wrap_lines)
        v.addWidget(self.legend)

        self.source_bar = QFrame(objectName="legend")
        sh = QHBoxLayout(self.source_bar)
        sh.setContentsMargins(22, 8, 22, 8)
        self.expand_sources = QCheckBox("Expand source code")
        self.expand_sources.setToolTip("Show the script of each definition of this name, "
                                       "instead of collapsing them")
        self.expand_sources.setChecked(_settings().value("expand_sources", True, type=bool))
        self.expand_sources.toggled.connect(self._expand_sources_toggled)
        sh.addWidget(self.expand_sources)
        sh.addStretch(1)
        self.source_bar.hide()
        v.addWidget(self.source_bar)

        self.body = QStackedWidget()
        self.block_view = BlockView()
        self.block_view.set_wrap(self.wrap_lines.isChecked())
        self.info = QTextBrowser()
        self.info.setOpenLinks(False)
        self.info.document().setDocumentMargin(18)
        OverlayScrollBar(self.info)
        self.empty = QLabel("Select a finding to see it here.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet(f"color: {C['dim']}; background: {C['bg']};")
        for w in (self.block_view, self.info, self.empty):
            self.body.addWidget(w)
        v.addWidget(self.body, 1)

        bar = QFrame(objectName="dismissBar")
        bh = QHBoxLayout(bar)
        bh.setContentsMargins(14, 10, 14, 10)
        bh.setSpacing(8)
        self.reason_edit = QLineEdit()
        self.reason_edit.setPlaceholderText("Why it stays as it is (optional)")
        self.reason_edit.returnPressed.connect(self.dismiss_current)
        self.dismiss_note = QLabel()
        self.dismiss_note.setStyleSheet(f"color: {C['review']}; font-size: 12px;")
        self.dismiss_button = QPushButton("Dismiss")
        self.dismiss_button.setProperty("kind", "danger")
        self.dismiss_button.clicked.connect(self.dismiss_current)
        bh.addWidget(self.reason_edit, 1)
        bh.addWidget(self.dismiss_note, 1)
        bh.addWidget(self.dismiss_button)
        self.dismiss_bar = bar
        v.addWidget(bar)
        return detail

    def _card(self, title, hint=None):
        card = QFrame(objectName="card")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(10)
        head = QLabel(title, objectName="cardTitle")
        v.addWidget(head)
        card.hint = None
        if hint:
            h = QLabel(hint, objectName="hint")
            h.setWordWrap(True)
            v.addWidget(h)
            card.hint = h
        return card, v

    def _page(self):
        page = QWidget(objectName="page")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(14)
        return page, lay

    def _build_dismissed_page(self):
        page, lay = self._page()
        card, v = self._card("Dismissed findings", "Select rows and Restore to report them again. A "
                                                   "dismissal also lapses on its own once either "
                                                   "side's text changes.")
        self.dismissed_tree = QTreeWidget(objectName="dataTree")
        self.dismissed_tree.setHeaderLabels(["Id", "Name", "Finding", "Detail", "Dismissed", "Reason"])
        self.dismissed_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.dismissed_tree.setRootIsDecorated(False)
        OverlayScrollBar(self.dismissed_tree)
        v.addWidget(self.dismissed_tree, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.restore_button = QPushButton("Restore selected")
        self.restore_button.clicked.connect(self.restore_selected)
        row.addWidget(self.restore_button)
        v.addLayout(row)
        lay.addWidget(card, 1)
        return page

    def _build_tracker_page(self):
        page, lay = self._page()
        row = QHBoxLayout()
        row.setSpacing(14)
        snaps, sv = self._card("Commits", self._tracker_line())
        self.version_card = snaps
        self.version_tree = QTreeWidget(objectName="dataTree")
        self.version_tree.setHeaderLabels(["Version", "Hash"])
        self.version_tree.setRootIsDecorated(False)
        OverlayScrollBar(self.version_tree)
        sv.addWidget(self.version_tree, 1)
        row.addWidget(snaps, 1)

        side = QVBoxLayout()
        side.setSpacing(14)
        take, tv = self._card("Make a commit", "Record the installed game as a new version after each "
                                                 "patch, then run the audits. Add versions oldest first.")
        form = QFormLayout()
        form.setSpacing(8)
        self.snap_version = QLineEdit()
        self.snap_version.setPlaceholderText("for example 1.3.12")
        self.snap_patch = QLineEdit()
        self.snap_patch.setPlaceholderText(tracker_patch_name())
        self.snap_game = QLineEdit()
        self.snap_game.setPlaceholderText("the game_root setting, $PDX_GAME_ROOT, or the Steam install")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_game_root)
        game_row = QHBoxLayout()
        game_row.addWidget(self.snap_game, 1)
        game_row.addWidget(browse)
        form.addRow("Version", self.snap_version)
        form.addRow("Patch name", self.snap_patch)
        form.addRow("Game folder", game_row)
        tv.addLayout(form)
        self.commit_button = QPushButton("Commit version")
        self.commit_button.setProperty("kind", "primary")
        self.commit_button.clicked.connect(self.commit_version)
        tv.addWidget(self.commit_button, 0, Qt.AlignmentFlag.AlignRight)
        side.addWidget(take)

        orphans, ov = self._card("Orphaned records", "Remove the records left behind by rebased or "
                                                     "deleted branches. Your dismissals on live "
                                                     "branches are untouched.")
        self.orphan_list = QListWidget()
        OverlayScrollBar(self.orphan_list)
        ov.addWidget(self.orphan_list, 1)
        self.orphan_button = QPushButton("Remove orphaned records")
        self.orphan_button.clicked.connect(self.remove_orphans)
        ov.addWidget(self.orphan_button, 0, Qt.AlignmentFlag.AlignRight)
        side.addWidget(orphans, 1)
        row.addLayout(side, 1)
        lay.addLayout(row, 1)
        return page

    # --- settings ------------------------------------------------------------------

    def _build_settings_page(self):
        """The config file's settings, edited here and by `pdx-audit --set`. Both call
        config.set_value, so a change made in either shows in the other."""
        page, lay = self._page()
        self.setting_edits, self.setting_notes = {}, {}
        card, v = self._card("Settings", "Set these once; they apply to every mod. A change to "
                                         "REPLACE findings applies from the next run. pdx-audit --set "
                                         "writes the same file.")
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        for key in config.SETTABLE:
            spec = config.SETTINGS[key]
            if spec["kind"] == "choice":
                edit = QComboBox()
                edit.addItems(spec["choices"])
                edit.setToolTip(spec["help"])
                edit.activated.connect(lambda _i, k=key: self.save_setting(k))
            else:
                edit = QLineEdit()
                edit.setPlaceholderText(spec["help"])
                edit.returnPressed.connect(lambda k=key: self.save_setting(k))
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(edit, 1)
            if spec["kind"] == "path":
                browse = QPushButton("Browse…")
                browse.clicked.connect(lambda _c=False, k=key: self._browse_setting(k))
                row.addWidget(browse)
                self.setting_buttons.append(browse)
            save = QPushButton("Save")
            save.setProperty("kind", "primary")
            save.clicked.connect(lambda _c=False, k=key: self.save_setting(k))
            save.setVisible(spec["kind"] != "choice")
            row.addWidget(save)
            clear = QPushButton("Clear")
            clear.setToolTip("Remove this setting")
            clear.clicked.connect(lambda _c=False, k=key: self.clear_setting(k))
            row.addWidget(clear)
            self.setting_buttons += [save, clear]
            note = QLabel(objectName="hint")
            note.setWordWrap(True)
            block = QVBoxLayout()
            block.setSpacing(3)
            block.addLayout(row)
            block.addWidget(note)
            form.addRow(spec["label"], block)
            self.setting_edits[key] = edit
            self.setting_notes[key] = note
        v.addLayout(form)
        self.settings_message = QLabel(objectName="hint")
        self.settings_message.setWordWrap(True)
        self.settings_message.setVisible(False)
        v.addWidget(self.settings_message)
        lay.addWidget(card)

        lay.addStretch(1)
        return page

    def _tracker_line(self):
        return f"Tracker: {self.vanilla_repo or 'none yet'}"

    def _tracker_name(self):
        """The tracker's own name for the status bar: its folder, or the folder holding
        it when that folder is just `repo.git`."""
        if not self.vanilla_repo:
            return "no tracker"
        p = Path(self.vanilla_repo)
        return p.parent.name if p.stem == "repo" else p.name

    def _settings_note(self, text, error=False):
        self.settings_message.setText(text)
        self.settings_message.setStyleSheet(
            f"color: {C['broken'] if error else C['muted']}; font-size: 12px;")
        self.settings_message.setVisible(bool(text))
        if text:
            self._status(text)

    def refresh_settings(self):
        """Redraw the Settings page from the config file on disk."""
        view = config.config_view()
        if view["stale"]:
            self._settings_note(view["stale"].removeprefix("Note: "))
        for s in view["settings"]:
            edit = self.setting_edits.get(s["key"])
            if edit is None:
                continue
            shown = s["stored"] if s["stored"] not in (None, "") else s["value"]
            if isinstance(edit, QComboBox):
                edit.setCurrentText(str(shown))
            elif not edit.hasFocus():
                edit.setText("" if shown in (None, "") else str(shown))
            stored_here = s["stored"] not in (None, "")
            self.setting_notes[s["key"]].setText("" if stored_here else f"from {s['origin']}")
        # An empty box means "use the setting", which is what the CLI resolves anyway.
        self.snap_patch.setPlaceholderText(tracker_patch_name())

    def _browse_setting(self, key):
        edit = self.setting_edits[key]
        title = ("The vanilla tracker repository" if key == "vanilla_repo"
                 else "The game's 'game' folder")
        path = QFileDialog.getExistingDirectory(self, title, edit.text() or str(Path.home()))
        if path:
            edit.setText(path)
            self.save_setting(key)

    def save_setting(self, key):
        """Store one setting, as `pdx-audit --set` does, and apply it to this window."""
        edit = self.setting_edits[key]
        value = (edit.currentText() if isinstance(edit, QComboBox) else edit.text()).strip()
        if not value:
            return self.clear_setting(key)
        return self._setting_action(key, lambda: config.set_value(key, value))

    def clear_setting(self, key):
        return self._setting_action(key, lambda: config.unset_value(key))

    def _setting_action(self, key, fn):
        try:
            messages = fn()
        except config.ConfigError as e:
            self._settings_note(str(e), error=True)
            return False
        for m in messages:
            self.log.appendPlainText(m)
        self._settings_note(" ".join(messages),
                            error=any(m.startswith("Note:") for m in messages))
        self.refresh_settings()
        if key == "vanilla_repo":
            self.reload_tracker()
        return True

    def reload_tracker(self, quiet=False):
        """Re-resolve the tracker after its setting changed, and redraw what reads it.
        `quiet` returns without touching the window when the tracker is unchanged."""
        from .tracker import locate_vanilla_repo
        repo, missing = locate_vanilla_repo(self.mod_root, self.tracker_override)
        if quiet and str(repo or "") == self.vanilla_repo:
            return
        self.vanilla_repo = str(repo) if repo else ""
        self.missing_tracker = missing
        if self.version_card.hint is not None:
            self.version_card.hint.setText(self._tracker_line())
        if self.tracker_override:
            self._settings_note(f"Saved. This window keeps the tracker it was opened with, "
                                f"--vanilla-repo {self.tracker_override}.")
        self.load_commits()
        self._set_busy(self.process is not None)
        self.refresh_store_views()

    def _build_output_page(self):
        page, lay = self._page()
        seg = QFrame(objectName="seg")
        sh = QHBoxLayout(seg)
        sh.setContentsMargins(2, 2, 2, 2)
        sh.setSpacing(2)
        self.output_stack = QStackedWidget()
        group = QButtonGroup(self)
        for i, label in enumerate(("Report", "Log")):
            b = QToolButton()
            b.setText(label)
            b.setCheckable(True)
            b.setChecked(i == 0)
            b.clicked.connect(lambda _c=False, n=i: self.output_stack.setCurrentIndex(n))
            group.addButton(b)
            sh.addWidget(b)
        top = QHBoxLayout()
        top.addWidget(seg)
        top.addStretch(1)
        lay.addLayout(top)
        self.report_text, self.log = self._text_pane(), self._text_pane()
        self.output_stack.addWidget(self.report_text)
        self.output_stack.addWidget(self.log)
        frame = QFrame(objectName="card")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(1, 1, 1, 1)
        fl.addWidget(self.output_stack)
        lay.addWidget(frame, 1)
        return page

    def _text_pane(self):
        pane = QPlainTextEdit()
        pane.setReadOnly(True)
        pane.setFont(font(12.5, mono=True))
        pane.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        OverlayScrollBar(pane)
        OverlayScrollBar(pane, Qt.Orientation.Horizontal)
        return pane

    def _build_statusbar(self):
        bar = QFrame(objectName="statusbar")
        bar.setFixedHeight(24)
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 0, 14, 0)
        h.setSpacing(16)
        self.status_left = QLabel()
        self.busy_bar = QProgressBar()
        self.busy_bar.setRange(0, 0)
        self.busy_bar.setFixedSize(90, 4)
        self.busy_bar.setTextVisible(False)
        self.status_msg = QLabel()
        self.status_right = QLabel()
        self.status_right.setStyleSheet(f"font-family: '{MONO}'; font-size: 11px;")
        h.addWidget(self.status_left)
        h.addWidget(self.busy_bar)
        h.addWidget(self.status_msg, 1)
        h.addWidget(self.status_right)
        return bar

    def _status(self, text):
        self.status_msg.setText(text)

    # --- state shared with the command line ----------------------------------------

    def load_commits(self):
        try:
            self.commits = get_commits(self.vanilla_repo) if self.vanilla_repo else []
        except Exception:
            self.commits = []
        self.version_tree.clear()
        for h, msg in self.commits:
            self.version_tree.addTopLevelItem(QTreeWidgetItem([msg, h]))
        self.version_tree.resizeColumnToContents(0)
        for combo, default in ((self.old_combo, "Previous commit"), (self.new_combo, "Newest commit")):
            keep = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(default, "")
            for h, msg in self.commits:
                combo.addItem(msg, h)
            i = combo.findData(keep)
            combo.setCurrentIndex(i if i >= 0 else 0)
            combo.blockSignals(False)
        n = len(self.commits)
        self.status_right.setText(f"{self._tracker_name()} · {n} commit{'' if n == 1 else 's'}")
        self._update_window_label()

    def _update_window_label(self):
        msgs = [m for _h, m in self.commits]
        by_commit = {h: m for h, m in self.commits}
        new = by_commit.get(self.new_combo.currentData(), msgs[0] if msgs else "")
        tip = ""
        if self.full_box.isChecked():
            old = msgs[-1] if msgs else ""
        elif self.old_combo.currentData():
            old = by_commit.get(self.old_combo.currentData(), "")
        else:
            # REPLACE blocks and GUI copies reach back to the oldest commit, so the
            # pill names only the new version and the tooltip names both windows.
            i = msgs.index(new) if new in msgs else 0
            previous = msgs[i + 1] if i + 1 < len(msgs) else ""
            old = ""
            if previous:
                tip = (f"REPLACE blocks and GUI copies: every commit up to {new}. "
                       f"Other audits: from {previous}.")
        self.window_button.setText(f"{old}  →  {new}" if old else new)
        self.window_button.setToolTip(tip)

    def refresh_store_views(self, then=None):
        """Reload the Dismissed list and the orphaned records. The findings record
        and the mod's git history are read off the UI thread; `then` runs once the
        views show the fresh state."""
        if then is not None:
            self._store_waiters.append(then)
        self._in_background("findings record", lambda: results.store_views(self.mod_root),
                            self._show_store_views, failed=self._store_waiters.clear)

    def _show_store_views(self, views):
        entries, self.orphans = views
        self.dismissed_tree.clear()
        for e in entries:
            detail = " · ".join(t for t in (e["detail"], "no longer found in the last run" if e["gone"] else "") if t)
            item = QTreeWidgetItem([e["id"], e["name"], e["label"], detail, e["on"], e["reason"]])
            item.setData(0, ROLE, e["fid"])
            self.dismissed_tree.addTopLevelItem(item)
        self.rail_group.button(1).setToolTip(f"{RAIL_PAGES[1][1]} ({len(entries)})")
        self.orphan_list.clear()
        self.orphan_list.addItems([str(p) for p in self.orphans])
        self._render_banners()
        waiters, self._store_waiters = self._store_waiters, []
        for then in waiters:
            then()

    def _open_page(self, index):
        self.pages.setCurrentIndex(index)
        if index == 4:
            config.invalidate()      # a --set from a terminal since the window opened
            self.refresh_settings()
            if self.process is None:
                self.reload_tracker(quiet=True)

    def _in_background(self, name, work, apply, failed=None):
        """Run work() on a worker thread, then apply(result) on the UI thread. A
        newer job under the same name supersedes one still running, so only the
        latest result is shown. A failure is written to the Log."""
        generation = self._generations.get(name, 0) + 1
        self._generations[name] = generation
        self.background_jobs += 1

        def run():
            try:
                outcome = (work(), None)
            except Exception as e:
                outcome = (None, e)
            try:
                self._background_done.emit((name, generation, apply, failed), outcome)
            except (RuntimeError, TypeError):
                pass                 # the window was deleted while this ran; Qt reports
                                     # the deleted signal as either of these
        threading.Thread(target=run, daemon=True).start()

    def _apply_background(self, job, outcome):
        name, generation, apply, failed = job
        value, error = outcome
        self.background_jobs -= 1
        if generation != self._generations.get(name):
            return
        if error is not None:
            self.log.appendPlainText(f"Could not read the {name}: {error}")
            if failed is not None:
                failed()
            return
        apply(value)

    def apply_options(self, options):
        audits = {CLI_TO_AUDIT[a] for a in options.get("audits") or [] if a in CLI_TO_AUDIT}
        for tag, chip in self.chips.items():
            chip.blockSignals(True)
            chip.setChecked(not audits or tag in audits)
            chip.blockSignals(False)
        for combo, flag in ((self.old_combo, "old"), (self.new_combo, "new")):
            i = combo.findData(options.get(flag) or "")
            combo.setCurrentIndex(i if i >= 0 else 0)
        self.set_run_choice(category=options.get("category") or "", block=options.get("block") or "",
                            full=bool(options.get("full")))

    def _fill_blocks(self):
        category = self.category_combo.currentData()
        keep = self.block_combo.currentData()
        blocks = (self.targets.get(category, []) if category
                  else sorted({b for bs in self.targets.values() for b in bs} | set(self.gui_blocks)))
        self.block_combo.blockSignals(True)
        self.block_combo.clear()
        self.block_combo.addItem("All blocks", "")
        for b in blocks:
            self.block_combo.addItem(b, b)
        i = self.block_combo.findData(keep)
        self.block_combo.setCurrentIndex(i if i >= 0 else 0)
        self.block_combo.blockSignals(False)

    def category_choices(self):
        return [self.category_combo.itemData(i) for i in range(1, self.category_combo.count())]

    def block_choices(self):
        return [self.block_combo.itemData(i) for i in range(1, self.block_combo.count())]

    def set_run_choice(self, category=None, block=None, full=None):
        if category is not None:
            i = self.category_combo.findData(category)
            self.category_combo.setCurrentIndex(i if i >= 0 else 0)
            self._fill_blocks()
        if block is not None:
            i = self.block_combo.findData(block)
            self.block_combo.setCurrentIndex(i if i >= 0 else 0)
        if full is not None:
            self.full_box.setChecked(full)

    def run_options(self):
        full = self.full_box.isChecked()
        return {"old": "" if full else (self.old_combo.currentData() or ""),
                "new": self.new_combo.currentData() or "", "full": full,
                "block": self.block_combo.currentData() or "",
                "category": self.category_combo.currentData() or ""}

    # --- background jobs --------------------------------------------------------

    def _set_busy(self, busy, text=None):
        for w in (self.run_button, self.run_arrow, self.restore_button, self.commit_button,
                  self.orphan_button):
            w.setEnabled(not busy)
        for w in (self.run_button, self.run_arrow):
            if not self.vanilla_repo:
                w.setEnabled(False)
                w.setToolTip("Choose a tracker in Settings, or make the first commit")
            else:
                w.setToolTip("")
        # Settings are not edited while a run is reading the tracker they name.
        for w in self.setting_buttons:
            w.setEnabled(not busy)
        self.stop_button.setVisible(busy)
        self.busy_bar.setVisible(busy)
        if text:
            self._status(text)
        self._update_dismiss_state()

    def _start(self, argv, on_done, text):
        if self.process is not None:
            return
        proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        proc.setProcessEnvironment(env)
        proc.setWorkingDirectory(str(self.mod_root))
        proc.readyReadStandardOutput.connect(lambda: self._read(proc.readAllStandardOutput()))
        proc.readyReadStandardError.connect(lambda: self._read(proc.readAllStandardError()))
        proc.finished.connect(lambda code, _status: self._finished(code, on_done))
        proc.errorOccurred.connect(lambda err: self._failed_to_start(err, on_done))
        self.process = proc
        self.last_line = ""
        self.log.appendPlainText(f"\n$ pdx-audit {' '.join(argv)}")
        self.log_fresh = True       # the first output of this run starts its own line
        self._set_busy(True, text)
        # With no tracker, --commit is the one command that runs: it creates the repo
        # where the settings say, so the flag is left off for it to resolve that itself.
        tracker = ["--vanilla-repo", self.vanilla_repo] if self.vanilla_repo else []
        proc.start(sys.executable, ["-m", "pdxaudit.cli", "--mod-root", str(self.mod_root),
                                    *tracker, "--color", "never", *argv])

    def _read(self, data):
        text = bytes(data).decode("utf-8", "replace").replace("\r", "\n")
        self.log.moveCursor(self.log.textCursor().MoveOperation.End)
        if self.log_fresh:
            self.log.insertPlainText("\n")
            self.log_fresh = False
        self.log.insertPlainText(text)
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if lines:
            self.last_line = lines[-1]
            if self.process is not None:
                self._status(self.last_line)

    def _finished(self, code, on_done):
        if self.process is None:
            return
        self.process.deleteLater()
        self.process = None
        self._set_busy(False)
        on_done(code)

    def _failed_to_start(self, err, on_done):
        if err == QProcess.ProcessError.FailedToStart and self.process is not None:
            self.log.appendPlainText(f"Could not start {sys.executable}.")
            self._finished(1, on_done)

    def stop_job(self):
        if self.process is not None:
            self.process.kill()

    def closeEvent(self, event):
        if self.process is not None:
            self.process.kill()
            self.process.waitForFinished(3000)
        super().closeEvent(event)

    def _job_failed(self, message):
        self._status(message)
        self.rail_group.button(3).setChecked(True)
        self.pages.setCurrentIndex(3)
        self.output_stack.setCurrentIndex(1)

    # --- runs -----------------------------------------------------------------

    def start_run(self):
        if not self.vanilla_repo:
            self._go_to_page(4)
            self._settings_note("Choose a tracker, or make the first commit on the Tracker page.",
                                error=True)
            return
        path = self.store.results_path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.run_banners = []
        self._render_banners()
        self._start(results.run_argv(self.run_options()) + ["--results-file", str(path)],
                    self._run_done, "Running audits…")

    def _run_done(self, code):
        self.refresh_store_views()
        if code != 0:
            self.run_banners = [("error", f"The run stopped: {self.last_line or 'see the Log'}", None, None)]
            self._render_banners()
            self._job_failed("The run stopped with an error; its output is in the Log.")
            return
        self.show_results(json.loads(self.store.results_path.read_text(encoding="utf-8")))
        self._status("Audits finished.")

    def show_results(self, payload):
        self.payload = payload
        self.records = payload["records"]
        self._prepared = {}
        self.run_banners = []
        for w in payload.get("warnings") or []:
            if "OUT OF DATE" in w:
                self.run_banners.append(("warn", "The game has changed since the newest commit. Commit "
                                         "the new version, then run the audits again.",
                                         "Commit version", self._go_to_commit))
            else:
                self.run_banners.append(("warn", w, None, None))
        self._render_banners()
        self.report_text.setPlainText("\n\n".join(
            [payload.get("triage") or ""] + [t for _a, t in payload.get("details") or [] if t.strip()]))
        for tag, chip in self.chips.items():
            chip.set_count(sum(1 for r in self.records if r["audit"] == tag))
        self._update_summary()
        self._rebuild_tree()
        self.check_for_changes()

    def check_for_changes(self):
        """Show the note beside Run audits when mod files changed after the run
        these results came from. Nothing reruns until the user asks. The mod's
        files are compared off the UI thread."""
        payload = self.payload
        if not payload:
            self._show_changes([])
            return
        self._in_background("mod's files", lambda: results.changed_files(payload, self.mod_root),
                            self._show_changes)

    def _show_changes(self, changed):
        self.changed_label.setVisible(bool(changed))
        if changed:
            count = len(changed)
            self.changed_label.setText(f"Mod changed since this run · {count} file{'' if count == 1 else 's'}")
            shown = changed[:25] + ([f"… and {count - 25} more"] if count > 25 else [])
            self.changed_label.setToolTip("\n".join(shown))

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            # Choices made on the command line meanwhile show without a restart.
            QTimer.singleShot(0, self.refresh_store_views)
            if self.payload:
                QTimer.singleShot(0, self.check_for_changes)

    def _update_summary(self):
        p = self.payload or {}
        recs = self.records
        counts = {s: sum(1 for r in recs if r["sev"] == s) for s in SEV_ORDER}
        self.sev_bar.set_counts(counts)
        if recs:
            self.counts_label.setText("&nbsp;&nbsp;&nbsp;".join(
                f'<b style="color:{C[s]}; font-weight:600;">{counts[s]}</b> {s}' for s in SEV_ORDER if counts[s]))
        else:
            self.counts_label.setText("No action needed: everything the mod overrides is current with vanilla.")
        extra = [f"{len({r['file'] for r in recs})} files"] if recs else []
        if p.get("info"):
            extra.append(f"{p['info']} informational")
        if p.get("dismissed"):
            extra.append(f"{p['dismissed']} dismissed")
        self.extra_label.setText(" · ".join(extra))
        window = p.get("window") or f"{p.get('old', '')} → {p.get('new', '')}"
        self.status_left.setText(f"{window} · {len(recs)} findings" if p else "")

    # --- banners --------------------------------------------------------------

    def _render_banners(self):
        while self.banner_box.count():
            w = self.banner_box.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        banners = list(self.run_banners)
        if not self.vanilla_repo:
            banners.append(("error", "No vanilla tracker yet. Choose one in Settings, or take the "
                            "first commit on the Tracker page.", "Settings",
                            lambda: self._go_to_page(4)))
        if self.orphans:
            banners.append(("warn", f"{len(self.orphans)} findings record(s) for this mod point at "
                            f"commits on no branch of this repository.", "Review and remove",
                            self._go_to_tracker))
        for tone, text, button, action in banners:
            frame = QFrame()
            fg = C["broken"] if tone == "error" else C["stale"]
            tint = "rgba(240, 107, 95, 0.12)" if tone == "error" else "rgba(255, 154, 82, 0.10)"
            frame.setStyleSheet(f"QFrame {{ background: {tint}; border-radius: 6px; }} "
                                f"QLabel {{ color: {fg}; background: transparent; }}")
            row = QHBoxLayout(frame)
            row.setContentsMargins(12, 6, 8, 6)
            label = QLabel(text)
            label.setWordWrap(True)
            row.addWidget(label, 1)
            if button:
                b = QPushButton(button)
                b.clicked.connect(action)
                row.addWidget(b)
            self.banner_box.addWidget(frame)
        self.banner_box.setContentsMargins(14, 8 if banners else 0, 14, 8 if banners else 0)

    def _go_to_tracker(self):
        self._go_to_page(2)

    def _go_to_page(self, index):
        self.rail_group.button(index).setChecked(True)
        self._open_page(index)

    def _go_to_commit(self):
        self._go_to_tracker()
        self.snap_version.setFocus()

    # --- the findings list -------------------------------------------------------

    def _visible(self, rec):
        chip = self.chips.get(rec["audit"])
        if chip is not None and not chip.isChecked():
            return False
        q = self.filter_edit.text().strip().lower()
        return not q or any(q in (rec.get(k) or "").lower() for k in ("name", "file", "id"))

    def set_audit_visible(self, audit, on):
        self.chips[audit].setChecked(on)

    def set_list_mode(self, mode):
        self.list_mode = mode
        for b in self.mode_group.buttons():
            b.setChecked(b.text().lower() == mode)
        self._rebuild_tree()

    def _toggle_item(self, item, _column=0):
        info = item.data(0, ROLE)
        if info and info["type"] in ("folder", "file"):
            item.setExpanded(not item.isExpanded())

    def _rebuild_tree(self):
        keep = self.current["fid"] if self.current else None
        self.tree.blockSignals(True)
        self.tree.clear()
        self._items, self._sections, self._titles = [], [], []
        recs = [r for r in self.records if self._visible(r)]
        tag = (self.payload or {}).get("new_tag")
        expand = len(recs) <= 40
        for title, rs in ((f"This patch ({tag})" if tag else "This patch", [r for r in recs if not r["earlier"]]),
                          ("Still open from earlier patches", [r for r in recs if r["earlier"]])):
            if not rs:
                continue
            section = QTreeWidgetItem()
            section.setData(0, ROLE, {"type": "section", "title": title, "count": len(rs), "depth": 0})
            section.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tree.addTopLevelItem(section)
            section.setExpanded(True)
            self._sections.append(title)
            if self.list_mode == "files":
                self._add_files(section, rs, expand)
            else:
                self._add_folders(section, rs, expand)
        self.tree.blockSignals(False)
        hidden = len(self.records) - len(recs)
        self.empty.setText(f"{hidden} finding{' is' if hidden == 1 else 's are'} hidden by the audit chips or the "
                           f"filter." if hidden else "Select a finding to see it here.")
        target = next((it for it, r in self._items if r["fid"] == keep), None)
        if target is None and self._items:
            target = self._items[0][0]
        if target is not None:
            parent = target.parent()
            while parent is not None:
                parent.setExpanded(True)
                parent = parent.parent()
            self.tree.setCurrentItem(target)
        else:
            self._show_record(None)

    def _node(self, parent, info, tooltip=None):
        item = QTreeWidgetItem()
        item.setData(0, ROLE, info)
        if tooltip:
            item.setToolTip(0, tooltip)
        parent.addChild(item)
        return item

    def _add_leaf(self, parent, rec, depth):
        title = rec["name"] + (f" › {rec['path'].replace(' > ', ' › ')}" if rec.get("path") else "")
        self._node(parent, {"type": "leaf", "title": title, "id": rec["id"] if rec["dismissible"] else "",
                            "sev": rec["sev"], "depth": depth, "index": len(self._items)}, title)
        self._items.append((parent.child(parent.childCount() - 1), rec))

    def _sorted(self, recs):
        return sorted(recs, key=lambda r: (SEV_ORDER.index(r["sev"]), r["name"], r["detail"]))

    def _add_files(self, section, recs, expand):
        groups = {}
        for r in recs:
            groups.setdefault(r["file"], []).append(r)
        for path in sorted(groups, key=lambda f: (SEV_ORDER.index(_worst(groups[f])), -len(groups[f]), f)):
            item = self._node(section, {"type": "file", "title": path, "full": True, "depth": 0,
                                        "count": len(groups[path]), "sev": _worst(groups[path])}, path)
            self._titles.append(path)
            for r in self._sorted(groups[path]):
                self._add_leaf(item, r, 1)
            item.setExpanded(expand)

    def _add_folders(self, section, recs, expand):
        root = {"dirs": {}, "files": {}}
        for r in recs:
            parts = r["file"].split("/")
            node = root
            for part in parts[:-1]:
                node = node["dirs"].setdefault(part, {"dirs": {}, "files": {}})
            node["files"].setdefault(parts[-1], []).append(r)

        def gather(node):
            return ([r for rs in node["files"].values() for r in rs]
                    + [r for child in node["dirs"].values() for r in gather(child)])

        def add(parent, node, depth, prefix):
            for name in sorted(node["dirs"]):
                child = node["dirs"][name]
                inside = gather(child)
                item = self._node(parent, {"type": "folder", "title": name, "depth": depth,
                                           "count": len(inside), "sev": _worst(inside)}, prefix + name)
                self._titles.append(name)
                add(item, child, depth + 1, prefix + name + "/")
                item.setExpanded(expand)
            for name in sorted(node["files"]):
                rs = node["files"][name]
                item = self._node(parent, {"type": "file", "title": name, "depth": depth,
                                           "count": len(rs), "sev": _worst(rs)}, prefix + name)
                self._titles.append(name)
                for r in self._sorted(rs):
                    self._add_leaf(item, r, depth + 1)
                item.setExpanded(expand)

        add(section, root, 0, "")

    def listed_records(self):
        return [r for _item, r in self._items]

    def section_titles(self):
        return list(self._sections)

    def tree_titles(self):
        return list(self._titles)

    def select_record(self, rec):
        for item, r in self._items:
            if r["fid"] == rec["fid"]:
                self.tree.setCurrentItem(item)
                return

    def detail_text(self):
        parts = [self.meta_label.text(), self.meta_id.text(), self.title_label.text(),
                 self.where_label.text(), self.sentence_label.text()]
        if self.values.isVisibleTo(self):
            parts += ["yours", self.yours_label.text(), "vanilla", self.vanilla_label.text()]
        parts += [self.info.toPlainText(), self.dismiss_note.text()]
        return "\n".join(_plain(p) for p in parts)

    # --- the finding panel ---------------------------------------------------------

    def _show_item(self, item):
        info = item.data(0, ROLE) if item is not None else None
        if info and info["type"] == "leaf":
            self._show_record(self._items[info["index"]][1])
        else:
            self._show_record(None, info)

    def _show_record(self, rec, group=None):
        self.current = rec
        self.source_bar.hide()
        if rec is None:
            self.meta_label.setText("")
            self.meta_id.setText("")
            if group and group["type"] in ("folder", "file"):
                self.title_label.setText(_esc(group["title"]))
                self.sentence_label.setText(f"{group['count']} findings")
            else:
                self.title_label.setText("")
                self.sentence_label.setText("")
            self.where_label.setText("")
            self.values.hide()
            self.legend.hide()
            self.dismiss_bar.hide()
            self.body.setCurrentWidget(self.empty)
            self._update_dismiss_state()
            return

        block = self.payload["blocks"].get(rec["block"]) if rec.get("block") else None
        kind = (block or {}).get("type") or AUDIT_LABEL.get(rec["audit"], rec["audit"])
        meta = [f'<span style="color:{C[rec["sev"]]}; font-weight:600;">●&nbsp;{_cap(rec["sev"])}</span>', _esc(kind)]
        since = rec.get("since") or ""
        if since:
            meta.append(f"vanilla changed it in {_esc(since)}")
        if rec.get("base"):
            meta.append(f"your copy matches {_esc(rec['base'])}")
        self.meta_label.setText("&nbsp;&nbsp;&nbsp;".join(meta))
        self.meta_id.setText(rec["id"] if rec["dismissible"] else "")
        path = f' <span style="color:{C["dim"]};">›</span> ' + _esc(rec["path"]).replace(" &gt; ", f' <span style="color:{C["dim"]};">›</span> ') if rec.get("path") else ""
        self.title_label.setText(_esc(rec["name"]) + path)
        self.where_label.setText(_esc(rec["location"]))
        self.sentence_label.setText(f"{_esc(_cap(rec['label']))}. Fix: {_esc(rec['fix'])}.")

        yours, vanilla = self._value_pair(rec)
        self.value_keys["vanilla"].setText("vanilla")
        self.values.setVisible(yours is not None)
        if yours is not None:
            self.yours_label.setText(yours)
            self.vanilla_label.setText(vanilla)

        pairs = bool(block) and bool(block.get("pairs"))
        self.side_by_side.setVisible(bool(block) and (block.get("vanilla_lines") is not None or pairs))
        if block is not None:
            side = self.side_by_side.isChecked() and (block.get("vanilla_lines") is not None or pairs)
            flatten = self.flatten.isChecked()
            key = (rec["block"], side, flatten)
            columns = ((f"vanilla {block.get('vanilla_tag') or ''} · {block.get('vanilla_file') or ''}",
                        f"yours · {block['file']}") if side else None)
            if side and pairs:
                columns = (f"vanilla, the keys you inject · {block.get('vanilla_file') or ''}",
                           f"yours · {block['file']}")

            def build():
                if side and pairs:
                    rows = results.inject_side_rows(block)
                else:
                    rows = results.side_rows(block) if side else results.block_rows(block)
                return BlockView.prepare(results.flatten_rows(rows) if flatten else rows)

            def show(prepared):
                # Rows and their colours take seconds for a large GUI file, so they are
                # built on a worker thread once per view of a block and kept.
                self._prepared[key] = prepared
                if self.current is not rec:
                    return
                self.block_view.set_rows(prepared[0], [], rec["fid"], columns, prepared)
                self._show_legend(prepared[0], block.get("base_tag"), side=side)

            if key in self._prepared:
                show(self._prepared[key])
            else:
                self.block_view.clear()
                self.legend.hide()
                self._in_background("block view", build, show)
            self.body.setCurrentWidget(self.block_view)
            self.info.clear()
        elif (rec.get("data") or {}).get("sources"):
            self.block_view.set_rows(results.source_rows(rec, self.mod_root, self.expand_sources.isChecked()),
                                     [], None)
            self.legend.hide()
            self.source_bar.show()
            self.body.setCurrentWidget(self.block_view)
            self.info.clear()
        else:
            self.block_view.clear()
            self.legend.hide()
            self.info.setHtml(self._info_html(rec))
            self.body.setCurrentWidget(self.info)

        self.dismiss_bar.show()
        self.reason_edit.setVisible(rec["dismissible"])
        self.dismiss_note.setVisible(not rec["dismissible"])
        self.dismiss_note.setText("" if rec["dismissible"] else
                                  "Duplicate definitions cannot be dismissed; fix them in the mod.")
        self._update_dismiss_state()

    def _side_by_side_toggled(self, on):
        _settings().setValue("side_by_side", on)
        if self.current is not None:
            self._show_record(self.current)

    def _flatten_toggled(self, on):
        _settings().setValue("flatten", on)
        if self.current is not None:
            self._show_record(self.current)

    def _wrap_toggled(self, on):
        _settings().setValue("wrap_lines", on)
        self.block_view.set_wrap(on)

    def _expand_sources_toggled(self, on):
        _settings().setValue("expand_sources", on)
        if self.current is not None:
            self._show_record(self.current)

    def _show_legend(self, rows, base=None, side=False):
        """Shows the legend for a block.

        The legend lists only the marks, the signs and the colours that the block
        shows. `base` gives the name of the version that the side-by-side colours
        compare with. A `side` view shows the severity of a finding as its icon."""
        present = {(r["mark"], r.get("cause")) for r in rows if r.get("mark")}
        signs = {r["sign"] for r in rows if r.get("mark") and r.get("sign")}
        states = {cell["state"] for r in rows if "left" in r for cell in (r["left"], r["right"])
                  if cell and not cell["quiet"] and cell["state"] != "same"}
        for (sev, cause), lab in self.legend_labels.items():
            text = LEGEND[(sev, cause)]
            glyph = (f'<span style="color:{C["text2"]}; font-weight:700;">{ICON[sev]}</span>' if side
                     else f'<span style="color:{C[sev]}; font-weight:700;">▍</span>')
            lab.setText(f'{glyph}&nbsp;{_esc(text)}')
            lab.setVisible((sev, cause) in present)
        for sign, lab in self.sign_labels.items():
            text = SIGN_MEANING[sign]
            lab.setText(f'<span style="font-family:\'{MONO}\'; font-weight:700; color:{C["text2"]};">'
                        f'{SIGN[sign]}</span>&nbsp;&nbsp;{_esc(text)}')
            lab.setVisible(sign in signs)
        for state, lab in self.diff_labels.items():
            colour = C["added"] if state == "add" else C["broken"]
            lab.setText(f'<span style="font-family:\'{MONO}\'; font-weight:700; color:{colour};">'
                        f'{DIFF_SIGN[state]}</span>&nbsp;&nbsp;{_esc(DIFF[state].format(base=base or "your copy’s version"))}')
            lab.setVisible(state in states)
        self.legend.setVisible(bool(present) or bool(states))

    def legend_marks(self):
        return list(dict.fromkeys(sev for (sev, _cause), lab in self.legend_labels.items()
                                  if self.legend.isVisibleTo(self) and lab.isVisibleTo(self)))

    def _value_pair(self, rec):
        if rec.get("yours") is not None:
            return self._yours_html(rec["yours"]), self._vanilla_html(rec["vanilla"])
        if rec["audit"] == "loc":
            k = rec.get("key") or {}
            since = f"  ({rec['since']})" if rec.get("since") else ""
            if k.get("change") == "removed":
                return _spaces("(still defined)"), self._vanilla_html(f"deleted \"{k.get('old', '')}\"{since}")
            if k.get("change") == "added":
                return _spaces("(also defined)"), self._vanilla_html(f"added \"{k.get('new', '')}\"{since}")
            return (_spaces(f"\"{k.get('mod', '')}\""),
                    self._vanilla_html(f"\"{k.get('old', '')}\"  →  \"{k.get('new', '')}\"{since}"))
        return None, None

    def _yours_html(self, text):
        if text in ("(missing)", "(removed)", "(commented out)"):
            return f'<span style="color:{C["muted"]};">{_esc(text)}</span>'
        return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", _spaces(text))

    def _vanilla_html(self, text):
        dim, new = f'style="color:{C["faint"]};"', f'style="color:{C["added"]};"'
        tag = ""
        m = re.match(r"^(.*?)(\s+\([^()]*\))$", text)
        if m:
            text, tag = m.group(1), m.group(2)
        if "  →  " in text:
            old, value = text.rsplit("  →  ", 1)
            body = f"<span {dim}>{_spaces(old)}&nbsp;&nbsp;→&nbsp;&nbsp;</span><span {new}>{_spaces(value)}</span>"
        elif text.startswith("added "):
            body = f"<span {dim}>added </span><span {new}>{_spaces(text[6:])}</span>"
        else:
            body = f"<span {dim}>{_spaces(text)}</span>"
        return body + (f'<span style="color:{C["dim"]};">{_spaces(tag)}</span>' if tag else "")

    def _info_html(self, rec):
        data = rec.get("data") or {}
        mono = f"font-family:'{MONO}'; font-size:12.5px;"
        out = []

        def heading(text):
            out.append(f'<p style="margin:14px 0 6px 0; color:{C["muted"]}; font-weight:600;">{_esc(text)}</p>')

        def lines(items):
            for item in items or []:
                out.append(f'<p style="margin:0; padding:1px 8px; white-space:pre; {mono} color:{C["text"]};">'
                           f'{_esc(item)}</p>')

        if rec["audit"] == "dupes" and rec["detail"]:
            heading("Defined at")
            lines(rec["detail"].split("; "))
        elif rec["detail"]:
            out.append(f'<p style="color:{C["text2"]};">{_esc(rec["detail"])}</p>')
        if data.get("vanilla_file"):
            out.append(f'<p style="margin-top:14px; color:{C["faint"]};">Vanilla: '
                       f'<span style="{mono}">{_esc(data["vanilla_file"])}</span></p>')
        return f'<div style="color:{C["text2"]}; font-size:13px;">{"".join(out)}</div>'

    def _update_dismiss_state(self):
        rec = self.current
        can = bool(rec and rec["dismissible"] and self.process is None)
        self.dismiss_button.setEnabled(can)
        self.reason_edit.setEnabled(can)

    # --- record actions --------------------------------------------------------

    def dismiss_current(self):
        rec = self.current
        if not rec or not rec["dismissible"] or self.process is not None:
            return
        done, errors = results.dismiss(self.mod_root, self.payload["records"], [rec["fid"]],
                                       self.reason_edit.text().strip())
        if errors:
            QMessageBox.warning(self, "Not dismissed", "\n".join(errors))
            return
        self.reason_edit.clear()
        self.records = self.payload["records"] = [r for r in self.payload["records"] if r["fid"] != rec["fid"]]
        self.payload["dismissed"] = self.payload.get("dismissed", 0) + len(done)
        self.current = None
        for tag, chip in self.chips.items():
            chip.set_count(sum(1 for r in self.records if r["audit"] == tag))
        self._update_summary()
        self._rebuild_tree()
        self.refresh_store_views()
        self._status(f"Dismissed {rec['id']} {rec['name']}.")

    def restore_selected(self):
        ids = [item.data(0, ROLE) for item in self.dismissed_tree.selectedItems()]
        if not ids:
            self._status("Select the dismissed findings to restore.")
            return
        removed, errors = results.undismiss(self.mod_root, ids)
        if errors:
            QMessageBox.warning(self, "Not restored", "\n".join(errors))
        self.refresh_store_views()
        if removed:
            self._status(f"Restored {len(removed)} finding(s). Run the audits again to list them.")

    def commit_version(self):
        tag = self.snap_version.text().strip()
        if not tag:
            self._status("Enter the game version to record, for example 1.3.12.")
            self.snap_version.setFocus()
            return
        argv = ["--commit", tag]
        if self.snap_patch.text().strip():
            argv += ["--patch-name", self.snap_patch.text().strip()]
        if self.snap_game.text().strip():
            argv += ["--game-root", self.snap_game.text().strip()]
        self._start(argv, self._commit_done, f"Committing {tag}…")

    def _commit_done(self, code):
        if not self.vanilla_repo:
            self.reload_tracker()    # the first commit creates the tracker, which reloads them
        else:
            self.load_commits()
        if code != 0:
            self._job_failed(f"The commit was not made: {self.last_line}")
            return
        self.snap_version.clear()
        self._status(self.last_line or "Commit made.")

    def _browse_game_root(self):
        path = QFileDialog.getExistingDirectory(self, "The game's 'game' folder",
                                                self.snap_game.text() or str(Path.home()))
        if path:
            self.snap_game.setText(path)

    def remove_orphans(self):
        self.refresh_store_views(then=self._confirm_orphan_removal)

    def _confirm_orphan_removal(self):
        if not self.orphans:
            self._status("No orphaned records.")
            return
        listing = "\n".join(str(p) for p in self.orphans)
        answer = QMessageBox.question(
            self, "Remove orphaned records",
            f"Remove these {len(self.orphans)} record file(s)? Their commits are on no branch "
            f"of this repository. A separate clone of the mod shares these records, so its "
            f"unpushed commits can appear here too.\n\n{listing}")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start(["--remove-orphaned-records", "--force"], self._orphans_done,
                    "Removing orphaned records…")

    def _orphans_done(self, code):
        self.refresh_store_views()
        if code != 0:
            self._job_failed(f"Records were not removed: {self.last_line}")
            return
        self._status(self.last_line or "Orphaned records removed.")


def _dark_palette():
    pal = QPalette()
    for role, color in ((QPalette.ColorRole.Window, C["bg"]), (QPalette.ColorRole.Base, C["list"]),
                        (QPalette.ColorRole.AlternateBase, C["bar"]), (QPalette.ColorRole.Text, C["text"]),
                        (QPalette.ColorRole.WindowText, C["text"]), (QPalette.ColorRole.Button, C["field"]),
                        (QPalette.ColorRole.ButtonText, C["text"]), (QPalette.ColorRole.Highlight, C["accent_bg"]),
                        (QPalette.ColorRole.HighlightedText, "#ffffff"), (QPalette.ColorRole.ToolTipBase, C["popup"]),
                        (QPalette.ColorRole.ToolTipText, C["text"]), (QPalette.ColorRole.PlaceholderText, C["dim"])):
        pal.setColor(role, QColor(color))
    return pal


def _use_wslg_wayland_socket():
    """Under WSLg the Wayland socket can be missing from XDG_RUNTIME_DIR. Point Qt
    at WSLg's own runtime folder, so the window opens without the X11 libraries
    Qt's other platform plugin needs."""
    name = os.environ.get("WAYLAND_DISPLAY")
    wslg = Path("/mnt/wslg/runtime-dir")
    if (not sys.platform.startswith("linux") or not name or os.environ.get("QT_QPA_PLATFORM")
            or (Path(os.environ.get("XDG_RUNTIME_DIR", "")) / name).exists()
            or not (wslg / name).exists()):
        return
    os.environ["XDG_RUNTIME_DIR"] = str(wslg)
    os.environ["QT_QPA_PLATFORM"] = "wayland"


def launch(mod_root, vanilla_repo, options):
    """Open the app window, start a run with `options`, and return the exit code."""
    _use_wslg_wayland_socket()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("pdx-audit")
    app.setStyle("Fusion")
    app.setPalette(_dark_palette())
    win = MainWindow(mod_root, vanilla_repo, options)
    win.resize(1440, 900)
    win.show()
    return app.exec()
