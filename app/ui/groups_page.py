"""Groups page: discover authorized groups, enable monitoring, per-group counters."""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.config import SettingsStore
from app.database.db import Database, from_db_ts
from app.database.models import LeadCriteria
from app.database.repositories import GroupRepository, LeadQueryRepository
from app.ui.dashboard import GROUP_STATUS
from app.ui.theme import C
from app.ui.widgets import Badge, EmptyState, PageHeader, ToggleSwitch, make_button

HEADERS = ["Monitor", "Group Name", "Group ID", "Type", "Members", "Tracked Users", "Matches", "New Today",
           "Last Scan", "Status", "Explicit Location"]
TYPE_LABELS = {"supergroup": "Supergroup", "group": "Group", "channel": "Channel", "gigagroup": "Broadcast group"}


class _NumItem(QTableWidgetItem):
    def __lt__(self, other) -> bool:
        a = self.data(Qt.ItemDataRole.UserRole)
        b = other.data(Qt.ItemDataRole.UserRole)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return a < b
        if isinstance(a, str) and isinstance(b, str):
            return a.lower() < b.lower()
        return super().__lt__(other)


class GroupsPage(QWidget):
    discover_requested = Signal()
    add_requested = Signal(str)
    groups_changed = Signal()
    scan_requested = Signal()
    open_group = Signal(object)

    def __init__(self, db: Database, settings: SettingsStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.db = db
        self.settings = settings
        self.repo = GroupRepository(db)
        self.leads = LeadQueryRepository(db)

        v = QVBoxLayout(self)
        v.setContentsMargins(28, 24, 28, 20)
        v.setSpacing(14)
        header = PageHeader("Groups", "Groups and channels your account already belongs to")
        self.discover_btn = make_button("Discover my groups", "refresh")
        self.discover_btn.clicked.connect(self._discover)
        header.add_action(self.discover_btn)
        scan = make_button("Scan now", "refresh", kind="Primary")
        scan.clicked.connect(self.scan_requested)
        header.add_action(scan)
        v.addWidget(header)

        add_card = QFrame()
        add_card.setObjectName("Card")
        ah = QHBoxLayout(add_card)
        ah.setContentsMargins(14, 12, 14, 12)
        ah.setSpacing(10)
        self.ref_input = QLineEdit()
        self.ref_input.setPlaceholderText("Add a group you are a member of: @username, t.me/name or -100… ID")
        self.ref_input.returnPressed.connect(self._add)
        ah.addWidget(self.ref_input, 1)
        add_btn = make_button("Add group", "plus")
        add_btn.clicked.connect(self._add)
        ah.addWidget(add_btn)
        v.addWidget(add_card)

        self.info = QLabel("")
        self.info.setObjectName("Muted")
        self.info.setWordWrap(True)
        v.addWidget(self.info)

        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(1, 1, 1, 1)
        self.stack = QStackedWidget()
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels([h.upper() for h in HEADERS])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(50)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hh.setStretchLastSection(True)
        for col, width in enumerate((96, 260, 150, 130, 100, 130, 100, 110, 150, 140, 180)):
            self.table.setColumnWidth(col, width)
        self.table.doubleClicked.connect(self._open_selected)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.stack.addWidget(self.table)
        self.stack.addWidget(EmptyState(
            "groups", "No groups yet",
            "Click “Discover my groups” to list the groups your account belongs to, or add one by @username."))
        cl.addWidget(self.stack)
        v.addWidget(card, 1)

        note = QLabel("Only chats your account is a member of can be monitored. Private one-to-one chats are never "
                      "listed or read. Broadcast channels only expose their member list to admins — monitor their "
                      "linked discussion group instead. Double-click a group to see its users.")
        note.setObjectName("Help")
        note.setWordWrap(True)
        v.addWidget(note)

    def criteria(self) -> LeadCriteria:
        s = self.settings.get()
        return LeadCriteria(min_score=s.min_relevance, joined_before=s.joined_before_date)

    def refresh(self) -> None:
        stats = self.leads.group_stats(self.criteria())
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(stats))
        for r, g in enumerate(stats):
            switch = ToggleSwitch(g.enabled)
            switch.toggled.connect(lambda checked, gid=g.telegram_group_id: self._toggle(gid, checked))
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(12, 0, 0, 0)
            hl.addWidget(switch)
            hl.addStretch(1)
            sort_item = _NumItem()
            sort_item.setData(Qt.ItemDataRole.UserRole, int(g.enabled))
            self.table.setItem(r, 0, sort_item)
            self.table.setCellWidget(r, 0, holder)
            name = QTableWidgetItem(g.group_name)
            name.setData(Qt.ItemDataRole.UserRole + 5, g.telegram_group_id)
            f = name.font()
            f.setBold(True)
            name.setFont(f)
            self.table.setItem(r, 1, name)
            self._num(r, 2, g.telegram_group_id, str(g.telegram_group_id), C.TEXT_2)
            self.table.setItem(r, 3, QTableWidgetItem(TYPE_LABELS.get(g.group_type, g.group_type)))
            self._num(r, 4, g.member_count or 0, f"{g.member_count:,}" if g.member_count else "—")
            self._num(r, 5, g.users, f"{g.users:,}")
            self._num(r, 6, g.matches, f"{g.matches:,}", C.SUCCESS if g.matches else None)
            self._num(r, 7, g.new_today, f"{g.new_today:,}", C.ACCENT if g.new_today else None)
            last = from_db_ts(g.last_scan_at)
            self.table.setItem(r, 8, QTableWidgetItem(last.astimezone().strftime("%Y-%m-%d %H:%M") if last else "never"))
            label, color = GROUP_STATUS.get(g.status, (g.status.title(), C.MUTED))
            if not g.enabled:
                label, color = "Not monitored", C.MUTED
            status_item = _NumItem()
            status_item.setData(Qt.ItemDataRole.UserRole, label)
            status_item.setToolTip(g.last_error or label)
            self.table.setItem(r, 9, status_item)
            badge_host = QWidget()
            bl = QHBoxLayout(badge_host)
            bl.setContentsMargins(10, 0, 0, 0)
            bl.addWidget(Badge(label, color))
            bl.addStretch(1)
            badge_host.setToolTip(g.last_error or label)
            self.table.setCellWidget(r, 9, badge_host)
            loc = QTableWidgetItem(g.explicit_location or "—")
            if not g.explicit_location:
                loc.setForeground(QColor(C.MUTED))
            self.table.setItem(r, 10, loc)
        self.table.setSortingEnabled(True)
        self.stack.setCurrentIndex(0 if stats else 1)
        enabled = sum(1 for g in stats if g.enabled)
        self.info.setText(f"{len(stats)} groups known · {enabled} monitored")

    def _num(self, row: int, col: int, value, text: str, color: str | None = None) -> None:
        item = _NumItem(text)
        item.setData(Qt.ItemDataRole.UserRole, value)
        if color:
            item.setForeground(QColor(color))
        self.table.setItem(row, col, item)

    def _toggle(self, gid: int, checked: bool) -> None:
        self.repo.set_enabled(gid, checked)
        self.groups_changed.emit()
        if checked:
            self.scan_requested.emit()

    def _discover(self) -> None:
        self.info.setText("Discovering your groups…")
        self.discover_requested.emit()

    def _add(self) -> None:
        ref = self.ref_input.text().strip()
        if not ref:
            return
        self.info.setText(f"Resolving {ref}…")
        self.add_requested.emit(ref)
        self.ref_input.clear()

    def show_message(self, text: str) -> None:
        self.info.setText(text)

    def _gid_at(self, row: int) -> int | None:
        item = self.table.item(row, 1)
        return item.data(Qt.ItemDataRole.UserRole + 5) if item else None

    def _open_selected(self, index) -> None:
        gid = self._gid_at(index.row())
        if gid is not None:
            self.open_group.emit(gid)

    def _menu(self, pos: QPoint) -> None:
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        gid = self._gid_at(index.row())
        name = self.table.item(index.row(), 1).text()
        menu = QMenu(self)
        menu.addAction("View users", lambda: self.open_group.emit(gid))
        menu.addAction("Copy group ID", lambda: QGuiApplication.clipboard().setText(str(gid)))
        menu.addSeparator()
        menu.addAction("Remove group and its data", lambda: self._remove(gid, name))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _remove(self, gid: int, name: str) -> None:
        answer = QMessageBox.question(
            self, "Remove group",
            f"Remove “{name}” and all users, scores and notifications stored for this group?\n"
            "This only deletes local data; you stay a member of the group in Telegram.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.repo.delete(gid)
            self.groups_changed.emit()
            self.refresh()
