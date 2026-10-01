"""Reusable widgets: state badges, KPI tiles, gauges, a line chart, dict-backed tables and
confirmation dialogs that state the consequence in rupees (FR-12.3)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPointF, QSortFilterProxyModel, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QMessageBox, QProgressBar, QTableView,
                               QVBoxLayout, QWidget)

from .theme import STATE_STYLE, TOKENS, badge_css

# Current theme tokens, updated by the main window when the theme changes.
CURRENT: dict[str, str] = dict(TOKENS["light"])


# --------------------------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------------------------
def inr(v: float | None, decimals: int = 0) -> str:
    """Rupees with Indian digit grouping: 12,34,56,789."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    neg = v < 0
    whole, _, frac = f"{abs(v):.{decimals}f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    s = "₹" + whole + (f".{frac}" if decimals else "")
    return f"-{s}" if neg else s


def num(v: Any, fmt: str = "{:,.2f}") -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    try:
        return fmt.format(v)
    except (ValueError, TypeError):
        return str(v)


def pct(v: float | None, decimals: int = 1) -> str:
    return "-" if v is None else f"{v:.{decimals}f}%"


def age(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def ts_short(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(str(iso)).strftime("%d-%b %H:%M")
    except ValueError:
        return str(iso)


# --------------------------------------------------------------------------------------------
# Badges, tiles, gauges
# --------------------------------------------------------------------------------------------
class StateBadge(QLabel):
    """Coloured label that always carries an icon and text as well (FR-12.4)."""

    def set_state(self, kind: str, text: str) -> None:
        self.setText(f"{STATE_STYLE.get(kind, '')} {text}")
        self.setStyleSheet(badge_css(kind, CURRENT))
        self.setAccessibleName(text)


class KpiTile(QFrame):
    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Tile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        self.title = QLabel(title)
        self.title.setObjectName("TileTitle")
        self.value = QLabel("-")
        self.value.setObjectName("TileValue")
        self.sub = QLabel("")
        self.sub.setObjectName("Muted")
        for w in (self.title, self.value, self.sub):
            lay.addWidget(w)

    def set(self, value: str, sub: str = "", kind: str | None = None) -> None:
        self.value.setText(value)
        self.sub.setText(sub)
        self.value.setStyleSheet(f"color: {CURRENT[kind]};" if kind else "")


class Gauge(QWidget):
    """Usage versus a limit; turns amber at 80% and red at 100% of the cap (text says so too)."""

    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(title)
        self.label.setObjectName("Muted")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(True)
        lay.addWidget(self.label)
        lay.addWidget(self.bar)
        self.title = title

    def set(self, used: float, cap: float, text: str = "") -> None:
        frac = used / cap if cap else 0.0
        self.bar.setValue(int(min(max(frac, 0.0), 1.0) * 1000))
        kind = "bad" if frac >= 1.0 else "warn" if frac >= 0.8 else "ok"
        label = {"ok": "", "warn": " - near limit", "bad": " - AT LIMIT"}[kind]
        self.bar.setFormat(f"{frac:.0%} of cap{label}")
        self.label.setText(f"{self.title}: {text}" if text else self.title)
        self.bar.setStyleSheet(f"QProgressBar::chunk {{ background: {CURRENT[kind]}; }}"
                               f"QProgressBar {{ border: 1px solid {CURRENT['border']}; border-radius: 3px;"
                               f" text-align: center; height: 16px; }}")


class LineChart(QWidget):
    """Minimal line chart (equity curves) drawn with QPainter - no QtCharts dependency."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.series: list[tuple[str, list[float], str]] = []
        self.labels: list[str] = []
        self.setMinimumHeight(180)

    def set_series(self, labels: list[str], series: list[tuple[str, list[float], str]]) -> None:
        self.labels = labels
        self.series = series
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt naming
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        left, right, top, bottom = 80, 12, 10, 24
        p.fillRect(self.rect(), QColor(CURRENT["panel"]))
        values = [v for _, s, _ in self.series for v in s if v is not None]
        if len(values) < 2:
            p.setPen(QColor(CURRENT["muted"]))
            p.drawText(self.rect(), Qt.AlignCenter, "No data yet")
            return
        lo, hi = min(values), max(values)
        if hi == lo:
            hi, lo = hi + 1, lo - 1
        pw, ph = w - left - right, h - top - bottom
        p.setPen(QPen(QColor(CURRENT["border"]), 1))
        for i in range(5):
            y = top + ph * i / 4
            p.drawLine(left, int(y), w - right, int(y))
            p.setPen(QColor(CURRENT["muted"]))
            p.drawText(2, int(y) + 4, inr(hi - (hi - lo) * i / 4))
            p.setPen(QPen(QColor(CURRENT["border"]), 1))
        if self.labels:
            p.setPen(QColor(CURRENT["muted"]))
            p.drawText(left, h - 6, str(self.labels[0]))
            p.drawText(w - right - 80, h - 6, str(self.labels[-1]))
        for _name, s, colour in self.series:
            n = len(s)
            if n < 2:
                continue
            path = QPainterPath()
            for i, v in enumerate(s):
                pt = QPointF(left + pw * i / (n - 1), top + ph * (1 - (v - lo) / (hi - lo)))
                path.moveTo(pt) if i == 0 else path.lineTo(pt)
            p.setPen(QPen(QColor(CURRENT.get(colour, colour)), 2))
            p.drawPath(path)


# --------------------------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------------------------
@dataclass
class Col:
    key: str
    title: str
    fmt: Callable[[Any], str] | None = None
    kind: Callable[[dict], str | None] | None = None   # row -> ok/warn/bad for colouring
    numeric: bool = False


class DictTableModel(QAbstractTableModel):
    def __init__(self, cols: list[Col], parent=None):
        super().__init__(parent)
        self.cols = cols
        self.rows: list[dict[str, Any]] = []

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return len(self.cols)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.cols[section].title
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        col = self.cols[index.column()]
        v = row.get(col.key)
        if role == Qt.DisplayRole:
            return col.fmt(v) if col.fmt else ("-" if v is None else str(v))
        if role == Qt.UserRole:          # raw value for sorting
            return v if isinstance(v, (int, float)) else ("" if v is None else str(v))
        if role == Qt.TextAlignmentRole and col.numeric:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role in (Qt.ForegroundRole, Qt.BackgroundRole) and col.kind:
            k = col.kind(row)
            if k:
                return QBrush(QColor(CURRENT[k] if role == Qt.ForegroundRole else CURRENT[k + "_bg"]))
        if role == Qt.ToolTipRole:
            return row.get("_tooltip")
        return None


class DataTable(QTableView):
    """Sortable, filterable table over a list of dicts; keeps the selection across refreshes."""

    def __init__(self, cols: list[Col], key: str | None = None, parent=None):
        super().__init__(parent)
        self.model_ = DictTableModel(cols, self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model_)
        self.proxy.setSortRole(Qt.UserRole)
        self.proxy.setFilterKeyColumn(-1)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.setModel(self.proxy)
        self.key = key
        self.sized = False          # set True when a saved header layout is restored
        self.setSortingEnabled(True)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setSectionsMovable(True)

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        keep = self.selected_row()
        self.model_.set_rows(rows)
        if rows and not self.sized:
            self.resizeColumnsToContents()
            for c in range(self.model_.columnCount()):
                self.setColumnWidth(c, min(self.columnWidth(c) + 12, 320))
            self.sized = True
        if keep and self.key:
            for r in range(self.proxy.rowCount()):
                src = self.proxy.mapToSource(self.proxy.index(r, 0))
                if self.model_.rows[src.row()].get(self.key) == keep.get(self.key):
                    self.selectRow(r)
                    break

    def selected_row(self) -> dict[str, Any] | None:
        idx = self.selectionModel().selectedRows() if self.selectionModel() else []
        if not idx:
            return None
        src = self.proxy.mapToSource(idx[0])
        return self.model_.rows[src.row()] if src.row() < len(self.model_.rows) else None

    def set_filter(self, text: str) -> None:
        self.proxy.setFilterFixedString(text)


# --------------------------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------------------------
def confirm(parent: QWidget, title: str, text: str, detail: str = "", danger: bool = True) -> bool:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning if danger else QMessageBox.Question)
    box.setWindowTitle(title)
    box.setText(text)
    if detail:
        box.setInformativeText(detail)
    box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
    box.setDefaultButton(QMessageBox.Cancel)
    return box.exec() == QMessageBox.Yes


def typed_confirm(parent: QWidget, title: str, text: str, phrase: str) -> bool:
    """Second confirmation for whole-book actions: the operator types a phrase (PRD s.8)."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    lay = QVBoxLayout(dlg)
    lay.addWidget(QLabel(text))
    lay.addWidget(QLabel(f"Type <b>{phrase}</b> to confirm:"))
    edit = QLineEdit()
    lay.addWidget(edit)
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    bb.button(QDialogButtonBox.Ok).setEnabled(False)
    edit.textChanged.connect(lambda t: bb.button(QDialogButtonBox.Ok).setEnabled(t.strip() == phrase))
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)
    return dlg.exec() == QDialog.Accepted


def ask_text(parent: QWidget, title: str, label: str, required: bool = True, default: str = "") -> str | None:
    text, ok = QInputDialog.getText(parent, title, label, QLineEdit.Normal, default)
    if not ok:
        return None
    if required and not text.strip():
        QMessageBox.information(parent, title, "A written reason is required.")
        return None
    return text.strip()


def hbox(*widgets, stretch: bool = True) -> QHBoxLayout:
    lay = QHBoxLayout()
    for w in widgets:
        lay.addWidget(w)
    if stretch:
        lay.addStretch(1)
    return lay
