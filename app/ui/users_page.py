"""Users page: group tabs, filter bar, polished lead table, detail drawer, CSV export."""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import (
    QAbstractTableModel,
    QDate,
    QModelIndex,
    QPoint,
    QRect,
    QRectF,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QGuiApplication, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTabBar,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.config import Paths, SettingsStore
from app.database.db import Database, from_db_ts
from app.database.models import JOINED_UNAVAILABLE, LeadCriteria, LeadFilter, LeadRow, REGIONS
from app.database.repositories import GroupRepository, LeadQueryRepository, UserRepository
from app.services.export import export_csv
from app.services.relevance import TOPIC_NAMES
from app.ui.theme import STATUS_COLORS, C, score_color, with_alpha
from app.ui.user_details import UserDetailDrawer, fmt_local
from app.ui.widgets import AVATARS, EmptyState, FlowLayout, PageHeader, SearchBox, make_button

ROW_ROLE = Qt.ItemDataRole.UserRole + 1
SORT_ROLE = Qt.ItemDataRole.UserRole + 2

COLUMNS = [
    ("No", 56), ("Avatar", 74), ("User ID", 118), ("Username", 150), ("First Name", 130), ("Last Name", 120),
    ("Group ID", 138), ("Group Name", 170), ("Joined Date", 170), ("First Detected", 140), ("Last Seen", 140),
    ("Region", 90), ("Relevance Score", 150), ("Topics", 260), ("Status", 100),
]
COL = {name: i for i, (name, _) in enumerate(COLUMNS)}


class LeadTableModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[LeadRow] = []

    def set_rows(self, rows: list[LeadRow]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return COLUMNS[section][0].upper()
            if role == Qt.ItemDataRole.TextAlignmentRole:
                return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        col = index.column()
        if role == ROW_ROLE:
            return row
        if role == SORT_ROLE:
            return self._sort_value(row, col, index.row())
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(row, col, index.row())
        if role == Qt.ItemDataRole.ForegroundRole:
            if col == COL["Joined Date"] and not row.joined_at:
                return QColor(C.MUTED)
            if col in (COL["User ID"], COL["Group ID"], COL["First Detected"], COL["Last Seen"], COL["No"]):
                return QColor(C.TEXT_2)
            if col == COL["Region"] and row.region == "Unknown":
                return QColor(C.MUTED)
        if role == Qt.ItemDataRole.FontRole and col == COL["Joined Date"] and not row.joined_at:
            f = QFont()
            f.setItalic(True)
            return f
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == COL["Joined Date"]:
                return ("Telegram did not provide an exact join date for this member. "
                        "First Detected is when this app first saw them.") if not row.joined_at else \
                    f"Source: {row.joined_at_source}"
            if col == COL["Topics"]:
                return ", ".join(row.topics) or "No topics detected"
            if col == COL["Relevance Score"]:
                return f"{row.supporting_messages_count} supporting public group message(s)"
        return None

    @staticmethod
    def _display(row: LeadRow, col: int, r: int):
        if col == COL["No"]:
            return str(r + 1)
        if col == COL["User ID"]:
            return str(row.user_id)
        if col == COL["Username"]:
            return f"@{row.username}" if row.username else "—"
        if col == COL["First Name"]:
            return row.first_name or "—"
        if col == COL["Last Name"]:
            return row.last_name or "—"
        if col == COL["Group ID"]:
            return str(row.group_id)
        if col == COL["Group Name"]:
            return row.group_name
        if col == COL["Joined Date"]:
            dt = from_db_ts(row.joined_at)
            return dt.astimezone().strftime("%Y-%m-%d") if dt else JOINED_UNAVAILABLE
        if col == COL["First Detected"]:
            return fmt_local(row.first_detected_at)
        if col == COL["Last Seen"]:
            return fmt_local(row.last_seen_at)
        if col == COL["Region"]:
            return row.region
        if col == COL["Relevance Score"]:
            return f"{row.relevance_score}%"
        if col == COL["Topics"]:
            return ", ".join(row.topics)
        if col == COL["Status"]:
            return row.status
        return None

    @staticmethod
    def _sort_value(row: LeadRow, col: int, r: int):
        if col == COL["No"]:
            return r
        if col == COL["Avatar"]:
            return int(row.has_photo)
        if col == COL["User ID"]:
            return row.user_id
        if col == COL["Group ID"]:
            return row.group_id
        if col == COL["Relevance Score"]:
            return row.relevance_score
        if col == COL["Joined Date"]:
            return row.joined_at or "9999"
        if col == COL["First Detected"]:
            return row.first_detected_at
        if col == COL["Last Seen"]:
            return row.last_seen_at
        if col == COL["Topics"]:
            return len(row.topics)
        value = LeadTableModel._display(row, col, r)
        return (value or "").lower()


class LeadProxy(QSortFilterProxyModel):
    """Sorting proxy that keeps the "No" column numbered in visual order."""

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if index.column() == COL["No"] and role == Qt.ItemDataRole.DisplayRole:
            return str(index.row() + 1)
        return super().data(index, role)


# --------------------------------------------------------------------------- #
# Delegates
# --------------------------------------------------------------------------- #


class _BaseDelegate(QStyledItemDelegate):
    def paint_background(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget else None
        if style:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)


class AvatarDelegate(_BaseDelegate):
    def paint(self, painter, option, index) -> None:
        self.paint_background(painter, option, index)
        row: LeadRow = index.data(ROW_ROLE)
        if row is None:
            return
        size = 30
        px = AVATARS.get(row.avatar_path, row.display_name.lstrip("@"), row.user_id, size,
                         painter.device().devicePixelRatioF())
        x = option.rect.x() + 12
        y = option.rect.y() + (option.rect.height() - size) // 2
        painter.drawPixmap(x, y, px)


class ScoreDelegate(_BaseDelegate):
    def paint(self, painter, option, index) -> None:
        self.paint_background(painter, option, index)
        row: LeadRow = index.data(ROW_ROLE)
        if row is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(10, 0, -10, 0)
        color = QColor(score_color(row.relevance_score))
        text_w = 40
        bar = QRectF(r.x(), r.center().y() - 3, max(20, r.width() - text_w - 8), 6)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(C.BORDER))
        painter.drawRoundedRect(bar, 3, 3)
        fill = QRectF(bar.x(), bar.y(), bar.width() * row.relevance_score / 100, bar.height())
        painter.setBrush(color)
        painter.drawRoundedRect(fill, 3, 3)
        painter.setPen(color if row.relevance_score else QColor(C.MUTED))
        f = QFont(option.font)
        f.setBold(True)
        painter.setFont(f)
        painter.drawText(QRect(int(bar.right()) + 8, r.y(), text_w, r.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, f"{row.relevance_score}%")
        painter.restore()


def _pill(painter: QPainter, rect: QRectF, text: str, color: str, font: QFont) -> None:
    c = QColor(color)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(with_alpha(color, 40))
    painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
    painter.setPen(c)
    painter.setFont(font)
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)


class StatusDelegate(_BaseDelegate):
    def paint(self, painter, option, index) -> None:
        self.paint_background(painter, option, index)
        row: LeadRow = index.data(ROW_ROLE)
        if row is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        f = QFont(option.font)
        f.setPointSizeF(max(7.5, option.font.pointSizeF() - 1))
        f.setBold(True)
        painter.setFont(f)
        w = painter.fontMetrics().horizontalAdvance(row.status) + 20
        rect = QRectF(option.rect.x() + 10, option.rect.center().y() - 10, w, 20)
        _pill(painter, rect, row.status, STATUS_COLORS.get(row.status, C.MUTED), f)
        painter.restore()


class TopicsDelegate(_BaseDelegate):
    def paint(self, painter, option, index) -> None:
        self.paint_background(painter, option, index)
        row: LeadRow = index.data(ROW_ROLE)
        if row is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        f = QFont(option.font)
        f.setPointSizeF(max(7.5, option.font.pointSizeF() - 1))
        painter.setFont(f)
        fm = painter.fontMetrics()
        x = option.rect.x() + 10
        right = option.rect.right() - 10
        cy = option.rect.center().y()
        if not row.topics:
            painter.setPen(QColor(C.MUTED))
            painter.drawText(QRect(x, option.rect.y(), right - x, option.rect.height()),
                             Qt.AlignmentFlag.AlignVCenter, "—")
        for i, topic in enumerate(row.topics):
            w = fm.horizontalAdvance(topic) + 16
            remaining = len(row.topics) - i
            more_w = fm.horizontalAdvance(f"+{remaining}") + 14
            if x + w > right - (more_w if remaining > 1 else 0):
                _pill(painter, QRectF(x, cy - 10, more_w, 20), f"+{remaining}", C.TEXT_2, f)
                break
            _pill(painter, QRectF(x, cy - 10, w, 20), topic, C.PRIMARY, f)
            x += w + 6
        painter.restore()


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #


class UsersPage(QWidget):
    toast = Signal(str, str)
    profile_requested = Signal(int)
    scan_requested = Signal()

    def __init__(self, db: Database, settings: SettingsStore, paths: Paths, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.db = db
        self.settings = settings
        self.paths = paths
        self.leads = LeadQueryRepository(db)
        self.groups = GroupRepository(db)
        self.users = UserRepository(db)
        self._suppress = False

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        main = QWidget()
        main.setObjectName("PageBody")
        v = QVBoxLayout(main)
        v.setContentsMargins(28, 24, 28, 20)
        v.setSpacing(14)
        root.addWidget(main, 1)

        header = PageHeader("Users", "Members detected in your authorized groups — always kept per group")
        self.columns_btn = make_button("Columns", "columns", kind="Ghost")
        self.columns_btn.clicked.connect(self._columns_menu)
        header.add_action(self.columns_btn)
        self.export_btn = make_button("Export CSV", "export")
        self.export_btn.clicked.connect(self.export)
        header.add_action(self.export_btn)
        scan_btn = make_button("Scan now", "refresh", kind="Primary")
        scan_btn.clicked.connect(self.scan_requested)
        header.add_action(scan_btn)
        v.addWidget(header)

        # Group tabs
        self.tabs = QTabBar()
        self.tabs.setObjectName("GroupTabs")
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideNone)
        self.tabs.currentChanged.connect(self._tab_changed)
        v.addWidget(self.tabs)

        # Filters
        filters = QFrame()
        filters.setObjectName("Card")
        fl = QVBoxLayout(filters)
        fl.setContentsMargins(14, 12, 14, 12)
        fl.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.search = SearchBox("Search username, name, user ID, group or topic…")
        self.search.setMinimumWidth(280)
        top.addWidget(self.search, 1)
        self.search_field = QComboBox()
        for label, key in (("All fields", "all"), ("Username", "username"), ("User ID", "user_id"),
                           ("Name", "name"), ("Group", "group"), ("Topic", "topic")):
            self.search_field.addItem(label, key)
        top.addWidget(self.search_field)
        self.reset_btn = make_button("Reset", "close", kind="Ghost")
        self.reset_btn.clicked.connect(self.reset_filters)
        top.addWidget(self.reset_btn)
        fl.addLayout(top)

        flow_host = QWidget()
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        policy.setHeightForWidth(True)
        flow_host.setSizePolicy(policy)
        flow = FlowLayout(flow_host, spacing=8)
        self.group_combo = QComboBox()
        self.group_combo.setMinimumWidth(170)
        flow.addWidget(self.group_combo)
        self.region_combo = QComboBox()
        for r in ("All",) + REGIONS:
            self.region_combo.addItem(f"Region: {r}", r)
        flow.addWidget(self.region_combo)
        self.score_spin = QSpinBox()
        self.score_spin.setRange(0, 100)
        self.score_spin.setPrefix("Score ≥ ")
        self.score_spin.setSuffix("%")
        self.score_spin.setMinimumWidth(110)
        flow.addWidget(self.score_spin)
        self.topic_combo = QComboBox()
        self.topic_combo.addItem("All topics", None)
        for t in TOPIC_NAMES:
            self.topic_combo.addItem(t, t)
        flow.addWidget(self.topic_combo)
        self.joined_check = QCheckBox("Joined before")
        flow.addWidget(self.joined_check)
        self.joined_date = QDateEdit()
        self.joined_date.setCalendarPopup(True)
        self.joined_date.setDisplayFormat("yyyy-MM-dd")
        self.joined_date.setMinimumWidth(120)
        flow.addWidget(self.joined_date)
        self.joined_known = QComboBox()
        for label, key in (("Joined date: any", "any"), ("Joined date known", "known"),
                           ("Joined date unknown", "unknown")):
            self.joined_known.addItem(label, key)
        flow.addWidget(self.joined_known)
        self.chip_new = self._chip("New today")
        self.chip_avatar = self._chip("Has avatar")
        self.chip_matches = self._chip("Matches only")
        self.chip_bots = self._chip("Include bots")
        for chip in (self.chip_new, self.chip_avatar, self.chip_matches, self.chip_bots):
            flow.addWidget(chip)
        fl.addWidget(flow_host)
        v.addWidget(filters)

        # Result summary
        summary = QHBoxLayout()
        self.count_label = QLabel("")
        self.count_label.setObjectName("Muted")
        summary.addWidget(self.count_label)
        summary.addStretch(1)
        legend = QLabel("Joined-before filter only applies where Telegram provided a real join date.")
        legend.setObjectName("Help")
        summary.addWidget(legend)
        v.addLayout(summary)

        # Table
        table_card = QFrame()
        table_card.setObjectName("Card")
        tl = QVBoxLayout(table_card)
        tl.setContentsMargins(1, 1, 1, 1)
        self.stack = QStackedWidget()
        self.model = LeadTableModel()
        self.proxy = LeadProxy()
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(SORT_ROLE)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(COL["Relevance Score"], Qt.SortOrder.DescendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        self.table.setWordWrap(False)
        self.table.setMouseTracking(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(48)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setSectionsMovable(True)
        hh.setStretchLastSection(True)
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for i, (_, width) in enumerate(COLUMNS):
            self.table.setColumnWidth(i, width)
        self.table.setItemDelegateForColumn(COL["Avatar"], AvatarDelegate(self.table))
        self.table.setItemDelegateForColumn(COL["Relevance Score"], ScoreDelegate(self.table))
        self.table.setItemDelegateForColumn(COL["Topics"], TopicsDelegate(self.table))
        self.table.setItemDelegateForColumn(COL["Status"], StatusDelegate(self.table))
        self.table.clicked.connect(self._row_clicked)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.stack.addWidget(self.table)
        self.empty = EmptyState("users", "No users match these filters",
                                "Enable groups on the Groups page and start a scan, or relax the filters.")
        self.stack.addWidget(self.empty)
        tl.addWidget(self.stack)
        v.addWidget(table_card, 1)

        # Drawer
        self.drawer = UserDetailDrawer(db, self.criteria)
        self.drawer.region_changed.connect(lambda _uid: self.refresh())
        self.drawer.profile_requested.connect(self.profile_requested)
        self.drawer.closed.connect(self.table.clearSelection)
        root.addWidget(self.drawer)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(220)
        self._debounce.timeout.connect(self.refresh)
        self.search.textChanged.connect(self._schedule)
        for combo in (self.search_field, self.region_combo, self.topic_combo, self.joined_known):
            combo.currentIndexChanged.connect(self._schedule)
        self.group_combo.currentIndexChanged.connect(self._group_combo_changed)
        self.score_spin.valueChanged.connect(self._schedule)
        self.joined_check.toggled.connect(self._schedule)
        self.joined_check.toggled.connect(self.joined_date.setEnabled)
        self.joined_date.dateChanged.connect(self._schedule)
        for chip in (self.chip_new, self.chip_avatar, self.chip_matches, self.chip_bots):
            chip.toggled.connect(self._schedule)

        self.reset_filters()
        self.reload_groups()

    # ------------------------------------------------------------------ #

    def _chip(self, text: str) -> QPushButton:
        chip = QPushButton(text)
        chip.setObjectName("Chip")
        chip.setCheckable(True)
        chip.setCursor(Qt.CursorShape.PointingHandCursor)
        return chip

    def criteria(self) -> LeadCriteria:
        s = self.settings.get()
        return LeadCriteria(min_score=s.min_relevance, joined_before=s.joined_before_date)

    def _schedule(self, *_args) -> None:
        if not self._suppress:
            self._debounce.start()

    def reset_filters(self) -> None:
        self._suppress = True
        s = self.settings.get()
        self.search.clear()
        self.search_field.setCurrentIndex(0)
        self.region_combo.setCurrentIndex(0)
        self.score_spin.setValue(0)
        self.topic_combo.setCurrentIndex(0)
        jb = s.joined_before_date
        self.joined_date.setDate(QDate(jb.year, jb.month, jb.day))
        self.joined_check.setChecked(False)
        self.joined_date.setEnabled(False)
        self.joined_known.setCurrentIndex(0)
        for chip in (self.chip_new, self.chip_avatar, self.chip_matches, self.chip_bots):
            chip.setChecked(False)
        self._suppress = False
        self._schedule()

    def current_filter(self) -> LeadFilter:
        d = self.joined_date.date()
        return LeadFilter(
            group_id=self.group_combo.currentData(),
            search=self.search.text(),
            search_field=self.search_field.currentData(),
            region=self.region_combo.currentData(),
            min_score=self.score_spin.value(),
            topic=self.topic_combo.currentData(),
            joined_before=date(d.year(), d.month(), d.day()) if self.joined_check.isChecked() else None,
            joined_known=self.joined_known.currentData(),
            new_today=self.chip_new.isChecked(),
            has_avatar=self.chip_avatar.isChecked(),
            matches_only=self.chip_matches.isChecked(),
            include_bots=self.chip_bots.isChecked(),
        )

    def refresh(self) -> None:
        rows = self.leads.query(self.current_filter(), self.criteria())
        selected = self._selected_key()
        self.model.set_rows(rows)
        matches = sum(1 for r in rows if r.is_match)
        unique = len({r.user_id for r in rows})
        suffix = " (limit reached — refine filters)" if len(rows) >= LeadFilter().limit else ""
        self.count_label.setText(
            f"{len(rows):,} memberships · {unique:,} unique users · {matches:,} matches{suffix}"
        )
        self.stack.setCurrentIndex(0 if rows else 1)
        self.export_btn.setEnabled(bool(rows))
        if selected:
            self._reselect(selected)
        if self.drawer.is_open:
            self.drawer.refresh()

    def _selected_key(self) -> tuple[int, int] | None:
        idx = self.table.currentIndex()
        if not idx.isValid():
            return None
        row: LeadRow = idx.data(ROW_ROLE)
        return (row.user_id, row.group_id) if row else None

    def _reselect(self, key: tuple[int, int]) -> None:
        for r in range(self.proxy.rowCount()):
            row: LeadRow = self.proxy.index(r, 0).data(ROW_ROLE)
            if row and (row.user_id, row.group_id) == key:
                self.table.selectRow(r)
                return

    # -- groups / tabs --------------------------------------------------- #

    def reload_groups(self) -> None:
        stats = [g for g in self.leads.group_stats(self.criteria()) if g.enabled or g.users]
        current = self.group_combo.currentData()
        self._suppress = True
        self.group_combo.clear()
        self.group_combo.addItem("All groups", None)
        for g in stats:
            self.group_combo.addItem(g.group_name, g.telegram_group_id)
        idx = self.group_combo.findData(current)
        self.group_combo.setCurrentIndex(max(0, idx))
        while self.tabs.count():
            self.tabs.removeTab(0)
        total = sum(g.users for g in stats)
        self.tabs.addTab(f"All Groups  {total:,}")
        self.tabs.setTabData(0, None)
        for g in stats:
            i = self.tabs.addTab(f"{g.group_name}  {g.users:,}")
            self.tabs.setTabData(i, g.telegram_group_id)
            self.tabs.setTabToolTip(i, f"Group ID: {g.telegram_group_id}\nUsers: {g.users:,}\nMatches: {g.matches:,}")
        self._select_tab(self.group_combo.currentData())
        self._suppress = False

    def _select_tab(self, group_id: int | None) -> None:
        for i in range(self.tabs.count()):
            if self.tabs.tabData(i) == group_id:
                self.tabs.setCurrentIndex(i)
                return

    def _tab_changed(self, index: int) -> None:
        if self._suppress or index < 0:
            return
        gid = self.tabs.tabData(index)
        self._suppress = True
        self.group_combo.setCurrentIndex(max(0, self.group_combo.findData(gid)))
        self._suppress = False
        self._schedule()

    def _group_combo_changed(self, _index: int) -> None:
        if self._suppress:
            return
        self._suppress = True
        self._select_tab(self.group_combo.currentData())
        self._suppress = False
        self._schedule()

    def show_group(self, group_id: int | None) -> None:
        self.group_combo.setCurrentIndex(max(0, self.group_combo.findData(group_id)))

    def apply_preset(self, preset: str) -> None:
        """Presets used by dashboard cards: all | matches | new_today | business."""
        self.reset_filters()
        self._suppress = True
        self.group_combo.setCurrentIndex(0)
        self._select_tab(None)
        self.chip_matches.setChecked(preset == "matches")
        self.chip_new.setChecked(preset == "new_today")
        if preset == "business":
            self.score_spin.setValue(self.settings.get().min_relevance)
        self._suppress = False
        self.refresh()

    # -- interactions ------------------------------------------------------ #

    def _row_clicked(self, index: QModelIndex) -> None:
        row: LeadRow = index.data(ROW_ROLE)
        if row:
            self.drawer.open_user(row.user_id, row.group_id)

    def open_user(self, user_id: int, group_id: int | None) -> None:
        self.drawer.open_user(user_id, group_id)

    def _context_menu(self, pos: QPoint) -> None:
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        row: LeadRow = index.data(ROW_ROLE)
        menu = QMenu(self)
        menu.addAction("View details", lambda: self.drawer.open_user(row.user_id, row.group_id))
        menu.addSeparator()
        menu.addAction("Copy user ID", lambda: QGuiApplication.clipboard().setText(str(row.user_id)))
        if row.username:
            menu.addAction("Copy username", lambda: QGuiApplication.clipboard().setText(f"@{row.username}"))
            menu.addAction("Open in Telegram",
                           lambda: QDesktopServices.openUrl(QUrl(f"https://t.me/{row.username}")))
        region_menu = menu.addMenu("Tag region")
        for label, value in (("Not tagged", None), ("US", "US"), ("EU", "EU"), ("Other", "Other")):
            region_menu.addAction(label, lambda v=value: self._tag_region(row.user_id, v))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _tag_region(self, user_id: int, region: str | None) -> None:
        self.users.set_manual_region(user_id, region)
        self.refresh()

    def _columns_menu(self) -> None:
        menu = QMenu(self)
        for i, (name, _) in enumerate(COLUMNS):
            act = QAction(name, menu)
            act.setCheckable(True)
            act.setChecked(not self.table.isColumnHidden(i))
            act.toggled.connect(lambda checked, col=i: self.table.setColumnHidden(col, not checked))
            menu.addAction(act)
        menu.exec(self.columns_btn.mapToGlobal(QPoint(0, self.columns_btn.height() + 4)))

    def export(self) -> None:
        rows = [self.proxy.index(r, 0).data(ROW_ROLE) for r in range(self.proxy.rowCount())]
        if not rows:
            return
        default = self.paths.exports / f"telegram-leads-{date.today().isoformat()}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Export filtered results", str(default), "CSV files (*.csv)")
        if not path:
            return
        try:
            count = export_csv(rows, path)
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", f"Could not write the CSV file:\n{exc}")
            return
        self.toast.emit("Export complete", f"{count:,} rows written to {path}")
