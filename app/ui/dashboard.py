"""Dashboard: KPI cards, detections chart, recent matches and per-group counters."""

from __future__ import annotations

import time
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.config import SettingsStore
from app.database.db import Database, from_db_ts
from app.database.models import JOINED_UNAVAILABLE, LeadCriteria
from app.database.repositories import LeadQueryRepository
from app.services import worker as W
from app.ui.theme import C, score_color
from app.ui.widgets import (
    AvatarLabel,
    Badge,
    BarChart,
    Card,
    EmptyState,
    PageHeader,
    PulseDot,
    StatCard,
    make_button,
)

STATE_COLORS = {
    W.STATE_SCANNING: C.ACCENT,
    W.STATE_IDLE: C.SUCCESS,
    W.STATE_PAUSED: C.WARNING,
    W.STATE_RATE_LIMITED: C.WARNING,
    W.STATE_CONNECTING: C.INFO,
    W.STATE_RECONNECTING: C.WARNING,
    W.STATE_NEEDS_LOGIN: C.DANGER,
    W.STATE_NEEDS_CREDENTIALS: C.DANGER,
    W.STATE_ERROR: C.DANGER,
    W.STATE_STOPPED: C.MUTED,
}

GROUP_STATUS = {"ok": ("Active", C.SUCCESS), "pending": ("Pending", C.MUTED),
                "inaccessible": ("Inaccessible", C.DANGER), "error": ("Error", C.WARNING)}


def relative(ts: float | None) -> str:
    if not ts:
        return "never"
    delta = int(time.time() - ts)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60} min ago"
    return datetime.fromtimestamp(ts).strftime("%H:%M")


class ClickableFrame(QFrame):
    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class DashboardPage(QWidget):
    open_group = Signal(object)
    open_user = Signal(int, int)
    open_users = Signal(str)  # preset: "all" | "matches" | "new_today"
    scan_requested = Signal()
    scanning_toggled = Signal(bool)
    go_settings = Signal()
    go_groups = Signal()

    def __init__(self, db: Database, settings: SettingsStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.db = db
        self.settings = settings
        self.leads = LeadQueryRepository(db)
        self._status: dict = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("PageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        body.setObjectName("PageBody")
        scroll.setWidget(body)
        v = QVBoxLayout(body)
        v.setContentsMargins(28, 24, 28, 28)
        v.setSpacing(18)

        header = PageHeader("Dashboard", "Live overview of the Telegram groups your account is authorized to access")
        self.pause_btn = make_button("Pause scanning", "pause")
        self.pause_btn.clicked.connect(self._toggle_scanning)
        header.add_action(self.pause_btn)
        scan = make_button("Scan now", "refresh", kind="Primary")
        scan.clicked.connect(self.scan_requested)
        header.add_action(scan)
        v.addWidget(header)

        # Onboarding banner
        self.banner = QFrame()
        self.banner.setObjectName("Banner")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(18, 14, 18, 14)
        texts = QVBoxLayout()
        self.banner_title = QLabel("")
        self.banner_title.setObjectName("CardTitle")
        self.banner_text = QLabel("")
        self.banner_text.setObjectName("Muted")
        self.banner_text.setWordWrap(True)
        texts.addWidget(self.banner_title)
        texts.addWidget(self.banner_text)
        bl.addLayout(texts, 1)
        self.banner_btn = make_button("", None, kind="Primary")
        self.banner_btn.clicked.connect(self._banner_action)
        bl.addWidget(self.banner_btn)
        self.banner.hide()
        v.addWidget(self.banner)

        # KPI cards
        self.cards_host = QWidget()
        self.cards_grid = QGridLayout(self.cards_host)
        self.cards_grid.setContentsMargins(0, 0, 0, 0)
        self.cards_grid.setSpacing(14)
        self.card_groups = StatCard("Total Groups", "groups", C.PRIMARY)
        self.card_users = StatCard("Total Users", "users", C.ACCENT)
        self.card_matching = StatCard("Matching Users", "target", C.SUCCESS)
        self.card_new = StatCard("New Today", "sparkle", "#F472B6")
        self.card_business = StatCard("Business/Developer Matches", "briefcase", C.WARNING)
        self.card_status = StatCard("Scan Status", "activity", C.INFO)
        self.status_dot = PulseDot(C.MUTED, 9)
        self.card_status.extra_layout.addWidget(self.status_dot)
        self.card_status.value.setStyleSheet("font-size: 20px; font-weight: 700;")
        self.card_groups.clicked.connect(self.go_groups)
        self.card_users.clicked.connect(lambda: self.open_users.emit("all"))
        self.card_matching.clicked.connect(lambda: self.open_users.emit("matches"))
        self.card_new.clicked.connect(lambda: self.open_users.emit("new_today"))
        self.card_business.clicked.connect(lambda: self.open_users.emit("business"))
        self._cards = [self.card_groups, self.card_users, self.card_matching,
                       self.card_new, self.card_business, self.card_status]
        self._columns = 0
        v.addWidget(self.cards_host)

        # Chart + recent matches
        mid = QHBoxLayout()
        mid.setSpacing(14)
        chart_card = Card("New members detected", "First detections per day across enabled groups (last 14 days)")
        self.chart = BarChart()
        chart_card.body.addWidget(self.chart)
        mid.addWidget(chart_card, 3)
        self.recent_card = Card("Latest matches", "Newest users meeting your lead criteria")
        self.recent_list = QVBoxLayout()
        self.recent_list.setSpacing(4)
        self.recent_card.body.addLayout(self.recent_list)
        self.recent_card.body.addStretch(1)
        self.recent_card.setMinimumWidth(280)
        mid.addWidget(self.recent_card, 2)
        v.addLayout(mid)

        # Groups
        self.groups_card = Card("Groups", "Per-group counters — users are never mixed across groups")
        view_all = make_button("Manage groups", "groups", kind="Ghost")
        view_all.clicked.connect(self.go_groups)
        self.groups_card.header.addWidget(view_all, 0, Qt.AlignmentFlag.AlignTop)
        self.groups_grid = QGridLayout()
        self.groups_grid.setHorizontalSpacing(12)
        self.groups_grid.setVerticalSpacing(12)
        self.groups_card.body.addLayout(self.groups_grid)
        v.addWidget(self.groups_card)
        v.addStretch(1)

        self._ticker = QTimer(self)
        self._ticker.setInterval(1000)
        self._ticker.timeout.connect(self._update_status_card)
        self._ticker.start()
        self._layout_cards()

    # ------------------------------------------------------------------ #

    def criteria(self) -> LeadCriteria:
        s = self.settings.get()
        return LeadCriteria(min_score=s.min_relevance, joined_before=s.joined_before_date)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._layout_cards()
        if getattr(self, "_tile_cols", None) not in (None, self._group_columns()):
            self._fill_groups(self.criteria())

    def _group_columns(self) -> int:
        width = self.groups_card.width() or self.width()
        return 3 if width >= 1100 else 2 if width >= 700 else 1

    def _layout_cards(self) -> None:
        width = self.width()
        cols = 6 if width >= 1500 else 3 if width >= 820 else 2
        if cols == self._columns:
            return
        self._columns = cols
        for card in self._cards:
            self.cards_grid.removeWidget(card)
        for i, card in enumerate(self._cards):
            self.cards_grid.addWidget(card, i // cols, i % cols)
        for c in range(6):
            self.cards_grid.setColumnStretch(c, 1 if c < cols else 0)

    def refresh(self) -> None:
        crit = self.criteria()
        s = self.settings.get()
        stats = self.leads.dashboard_stats(crit)
        self.card_groups.set_value(stats.total_groups, f"{stats.enabled_groups} enabled for monitoring")
        self.card_users.set_value(stats.total_users, "Unique members tracked")
        self.card_matching.set_value(
            stats.matching_users, f"Score ≥ {s.min_relevance}% · joined before {s.joined_before} or unknown")
        self.card_new.set_value(stats.new_today, "First detected since midnight")
        self.card_business.set_value(stats.business_matches, f"Relevance ≥ {s.min_relevance}%, any join date")
        self.chart.set_data(self.leads.detections_per_day(14))
        self._fill_recent(crit)
        self._fill_groups(crit)
        self._update_banner(stats.enabled_groups)

    def _fill_recent(self, crit: LeadCriteria) -> None:
        while self.recent_list.count():
            item = self.recent_list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        rows = self.leads.recent_matches(crit, limit=6)
        if not rows:
            self.recent_list.addWidget(EmptyState("target", "No matches yet",
                                                  "Matches appear here as soon as a scan finds them."))
            return
        for row in rows:
            item = ClickableFrame()
            item.setCursor(Qt.CursorShape.PointingHandCursor)
            item.setStyleSheet(
                f"ClickableFrame {{ border-radius: 10px; }} ClickableFrame:hover {{ background: {C.CARD_HOVER}; }}")
            h = QHBoxLayout(item)
            h.setContentsMargins(8, 6, 8, 6)
            h.setSpacing(10)
            av = AvatarLabel(34)
            av.set_avatar(row.avatar_path, row.display_name.lstrip("@"), row.user_id)
            h.addWidget(av)
            texts = QVBoxLayout()
            texts.setSpacing(1)
            name = QLabel(row.display_name)
            name.setStyleSheet("font-weight: 600;")
            joined = from_db_ts(row.joined_at)
            meta = QLabel(f"{row.group_name} · {joined.date().isoformat() if joined else JOINED_UNAVAILABLE}")
            meta.setObjectName("Muted")
            texts.addWidget(name)
            texts.addWidget(meta)
            h.addLayout(texts, 1)
            h.addWidget(Badge(f"{row.relevance_score}%", score_color(row.relevance_score)))
            item.clicked.connect(lambda uid=row.user_id, gid=row.group_id: self.open_user.emit(uid, gid))
            self.recent_list.addWidget(item)

    def _fill_groups(self, crit: LeadCriteria) -> None:
        while self.groups_grid.count():
            item = self.groups_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        groups = [g for g in self.leads.group_stats(crit) if g.enabled]
        if not groups:
            self.groups_grid.addWidget(EmptyState(
                "groups", "No groups enabled",
                "Open the Groups page, discover the groups your account belongs to and enable the ones to monitor."),
                0, 0)
            return
        cols = self._group_columns()
        self._tile_cols = cols
        for c in range(3):
            self.groups_grid.setColumnStretch(c, 1 if c < cols else 0)
        for i, g in enumerate(groups):
            tile = ClickableFrame()
            tile.setCursor(Qt.CursorShape.PointingHandCursor)
            tile.setStyleSheet(
                f"ClickableFrame {{ background: {C.SURFACE}; border: 1px solid {C.BORDER_SOFT}; border-radius: 12px; }}"
                f"ClickableFrame:hover {{ border-color: {C.PRIMARY}; }}")
            t = QVBoxLayout(tile)
            t.setContentsMargins(14, 12, 14, 12)
            t.setSpacing(6)
            top = QHBoxLayout()
            name = QLabel(g.group_name)
            name.setStyleSheet("font-weight: 700; font-size: 13px; background: transparent; border: none;")
            top.addWidget(name, 1)
            label, color = GROUP_STATUS.get(g.status, (g.status.title(), C.MUTED))
            top.addWidget(Badge(label, color))
            t.addLayout(top)
            gid = QLabel(f"Group ID: {g.telegram_group_id}")
            gid.setObjectName("Muted")
            gid.setStyleSheet("background: transparent; border: none;")
            t.addWidget(gid)
            nums = QHBoxLayout()
            nums.setSpacing(18)
            for caption, value, col in (("Users", g.users, C.TEXT), ("Matches", g.matches, C.SUCCESS),
                                        ("New today", g.new_today, C.ACCENT)):
                box = QVBoxLayout()
                box.setSpacing(0)
                val = QLabel(f"{value:,}")
                val.setStyleSheet(f"font-size: 18px; font-weight: 700; color: {col}; background: transparent; border: none;")
                cap = QLabel(caption)
                cap.setObjectName("Help")
                cap.setStyleSheet("background: transparent; border: none;")
                box.addWidget(val)
                box.addWidget(cap)
                nums.addLayout(box)
            nums.addStretch(1)
            t.addLayout(nums)
            last = from_db_ts(g.last_scan_at)
            foot = QLabel(f"Last scan: {last.astimezone().strftime('%Y-%m-%d %H:%M') if last else 'never'}"
                          + (f" · {g.last_error}" if g.last_error else ""))
            foot.setObjectName("Help")
            foot.setWordWrap(True)
            foot.setStyleSheet("background: transparent; border: none;")
            t.addWidget(foot)
            tile.clicked.connect(lambda gid_=g.telegram_group_id: self.open_group.emit(gid_))
            self.groups_grid.addWidget(tile, i // cols, i % cols)

    # -- status ---------------------------------------------------------- #

    def set_status(self, status: dict) -> None:
        self._status = status
        self.pause_btn.setText("Pause scanning" if status.get("scanning_enabled") else "Resume scanning")
        from app.ui.theme import icon

        self.pause_btn.setIcon(icon("pause" if status.get("scanning_enabled") else "play", C.TEXT_2, 16))
        self._update_status_card()
        self._update_banner(None)

    def _update_status_card(self) -> None:
        st = self._status
        if not st:
            return
        state = st.get("state", W.STATE_STOPPED)
        color = STATE_COLORS.get(state, C.MUTED)
        self.status_dot.set_color(color, state in (W.STATE_SCANNING, W.STATE_CONNECTING, W.STATE_RECONNECTING))
        hint = st.get("detail") or ""
        if state in (W.STATE_IDLE,):
            nxt = st.get("next_scan")
            remaining = max(0, int(nxt - time.time())) if nxt else None
            hint = f"Last scan {relative(st.get('last_scan'))}" + (
                f" · next in {remaining}s" if remaining is not None else "")
        self.card_status.set_text(state, hint)

    def _update_banner(self, enabled_groups: int | None) -> None:
        state = self._status.get("state")
        if state == W.STATE_NEEDS_CREDENTIALS:
            self._show_banner("Connect your Telegram API credentials",
                              "Add your API ID and API hash from my.telegram.org in Settings to get started.",
                              "Open Settings", "settings")
        elif state == W.STATE_NEEDS_LOGIN:
            self._show_banner("Sign in to Telegram",
                              "Log in with your phone number and verification code to start monitoring.",
                              "Sign in", "settings")
        else:
            if enabled_groups is None:
                enabled_groups = self.leads.dashboard_stats(self.criteria()).enabled_groups
            if enabled_groups == 0 and state not in (W.STATE_CONNECTING, W.STATE_STOPPED, None):
                self._show_banner("Choose groups to monitor",
                                  "Enable one or more of your groups on the Groups page. Only groups your "
                                  "account already belongs to can be monitored.",
                                  "Open Groups", "groups")
            else:
                self.banner.hide()

    def _show_banner(self, title: str, text: str, button: str, action: str) -> None:
        self.banner_title.setText(title)
        self.banner_text.setText(text)
        self.banner_btn.setText(button)
        self.banner_btn.setProperty("action", action)
        self.banner.show()

    def _banner_action(self) -> None:
        if self.banner_btn.property("action") == "groups":
            self.go_groups.emit()
        else:
            self.go_settings.emit()

    def _toggle_scanning(self) -> None:
        self.scanning_toggled.emit(not self._status.get("scanning_enabled", True))
