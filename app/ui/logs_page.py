"""Logs page: live, filterable activity log (scans, rate limits, errors, reconnects)."""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.config import Paths
from app.services.logging_service import MAX_MEMORY_RECORDS, QtLogHandler
from app.ui.theme import C
from app.ui.widgets import PageHeader, SearchBox, ToggleRow, make_button

LEVEL_COLORS = {"DEBUG": C.MUTED, "INFO": C.INFO, "WARNING": C.WARNING, "ERROR": C.DANGER, "CRITICAL": C.DANGER}
LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


class LogModel(QAbstractTableModel):
    HEADERS = ["TIME", "LEVEL", "SOURCE", "MESSAGE"]

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[dict] = []

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 4

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        e = self.entries[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return (e["time"].strftime("%Y-%m-%d %H:%M:%S"), e["level"], e["source"], e["message"])[col]
        if role == Qt.ItemDataRole.ForegroundRole:
            if col == 1:
                return QColor(LEVEL_COLORS.get(e["level"], C.TEXT_2))
            if col in (0, 2):
                return QColor(C.TEXT_2)
            if e["level"] in ("ERROR", "CRITICAL"):
                return QColor("#FFB4B4")
        if role == Qt.ItemDataRole.ToolTipRole and col == 3:
            return e["message"]
        if role == Qt.ItemDataRole.UserRole:
            return e
        return None

    def append(self, entry: dict) -> None:
        if len(self.entries) >= MAX_MEMORY_RECORDS:
            self.beginRemoveRows(QModelIndex(), 0, 499)
            del self.entries[:500]
            self.endRemoveRows()
        n = len(self.entries)
        self.beginInsertRows(QModelIndex(), n, n)
        self.entries.append(entry)
        self.endInsertRows()

    def clear(self) -> None:
        self.beginResetModel()
        self.entries.clear()
        self.endResetModel()


class LogFilter(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self.min_level = 20
        self.text = ""

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:  # noqa: N802
        e = self.sourceModel().entries[row]
        if LEVEL_ORDER.get(e["level"], 20) < self.min_level:
            return False
        if self.text:
            t = self.text.lower()
            return t in e["message"].lower() or t in e["source"].lower()
        return True


class LogsPage(QWidget):
    def __init__(self, handler: QtLogHandler, paths: Paths, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.paths = paths
        v = QVBoxLayout(self)
        v.setContentsMargins(28, 24, 28, 20)
        v.setSpacing(14)
        header = PageHeader("Logs", "Scans, processed users, rate-limit waits, API errors and reconnects")
        open_btn = make_button("Open log folder", "folder", kind="Ghost")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.paths.logs))))
        header.add_action(open_btn)
        clear_btn = make_button("Clear view", "trash")
        clear_btn.clicked.connect(lambda: self.model.clear())
        header.add_action(clear_btn)
        v.addWidget(header)

        bar = QFrame()
        bar.setObjectName("Card")
        bh = QHBoxLayout(bar)
        bh.setContentsMargins(14, 10, 14, 10)
        bh.setSpacing(10)
        self.search = SearchBox("Filter log messages…")
        bh.addWidget(self.search, 1)
        self.level = QComboBox()
        for label, lvl in (("Info and above", 20), ("Warnings and errors", 30), ("Errors only", 40),
                           ("Everything (debug)", 10)):
            self.level.addItem(label, lvl)
        bh.addWidget(self.level)
        self.follow = ToggleRow("Auto-scroll", "", True)
        bh.addWidget(self.follow)
        v.addWidget(bar)

        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(1, 1, 1, 1)
        self.model = LogModel()
        self.proxy = LogFilter()
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.setColumnWidth(0, 170)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 120)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        cl.addWidget(self.table)
        v.addWidget(card, 1)

        self.search.textChanged.connect(self._apply_filter)
        self.level.currentIndexChanged.connect(self._apply_filter)

        for entry in list(handler.history):
            self.model.append(entry)
        handler.bus.record.connect(self._on_record)
        self.table.scrollToBottom()

    def _apply_filter(self, *_args) -> None:
        self.proxy.text = self.search.text().strip()
        self.proxy.min_level = self.level.currentData()
        self.proxy.invalidateFilter()

    def _on_record(self, entry: dict) -> None:
        self.model.append(entry)
        if self.follow.isChecked():
            self.table.scrollToBottom()
