"""Settings: API credentials (masked), session, scanning, lead criteria, groups, notifications, startup."""

from __future__ import annotations

from PySide6.QtCore import QDate, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QScrollArea,
    QSlider,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.config import ENV_PATH, Credentials, Paths, SettingsStore, mask_secret
from app.database.db import Database
from app.database.repositories import GroupRepository
from app.ui.theme import C, icon
from app.ui.widgets import Card, PageHeader, ToggleRow, make_button


class SecretField(QWidget):
    """Password-style field with a reveal toggle. Shows only the last characters when masked."""

    def __init__(self, placeholder: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit.setPlaceholderText(placeholder)
        row.addWidget(self.edit, 1)
        self.eye = QToolButton()
        self.eye.setCheckable(True)
        self.eye.setIcon(icon("eye", C.TEXT_2, 16))
        self.eye.setToolTip("Show / hide")
        self.eye.setCursor(Qt.CursorShape.PointingHandCursor)
        self.eye.toggled.connect(self._toggle)
        row.addWidget(self.eye)

    def _toggle(self, shown: bool) -> None:
        self.edit.setEchoMode(QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password)
        self.eye.setIcon(icon("eye_off" if shown else "eye", C.TEXT_2, 16))

    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, text: str) -> None:  # noqa: N802
        self.edit.setText(text)


def field(label: str, widget: QWidget, help_text: str = "") -> QWidget:
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(5)
    lab = QLabel(label)
    lab.setObjectName("FieldLabel")
    v.addWidget(lab)
    v.addWidget(widget)
    if help_text:
        h = QLabel(help_text)
        h.setObjectName("Help")
        h.setWordWrap(True)
        h.setOpenExternalLinks(True)
        v.addWidget(h)
    return w


class SettingsPage(QWidget):
    settings_saved = Signal()
    credentials_saved = Signal()
    login_requested = Signal()
    logout_requested = Signal()
    groups_changed = Signal()
    test_notification = Signal()

    def __init__(self, db: Database, settings: SettingsStore, paths: Paths, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.db = db
        self.settings = settings
        self.paths = paths
        self.groups = GroupRepository(db)

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
        v.setSpacing(16)

        header = PageHeader("Settings", "Credentials, scanning behaviour, lead criteria and notifications")
        self.save_btn = make_button("Save settings", "check", kind="Primary")
        self.save_btn.clicked.connect(self.save)
        header.add_action(self.save_btn)
        v.addWidget(header)

        self.columns_host = QWidget()
        columns = QHBoxLayout(self.columns_host)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(16)
        self.col_left = QVBoxLayout()
        self.col_right = QVBoxLayout()
        for col in (self.col_left, self.col_right):
            col.setSpacing(16)
            columns.addLayout(col, 1)
        v.addWidget(self.columns_host)
        v.addStretch(1)

        # --- Telegram API -------------------------------------------------
        api = Card("Telegram API", "Create credentials at my.telegram.org → API development tools")
        self.api_id = SecretField("e.g. 1234567")
        self.api_hash = SecretField("32-character hash")
        self.phone = QLineEdit()
        self.phone.setPlaceholderText("+15551234567")
        self.bot_token = SecretField("Optional — only for bot mode")
        api.body.addWidget(field("API ID", self.api_id))
        api.body.addWidget(field("API Hash", self.api_hash))
        api.body.addWidget(field("Phone number", self.phone, "Used only to request the Telegram login code."))
        api.body.addWidget(field("Bot token (optional)", self.bot_token))
        self.creds_state = QLabel("")
        self.creds_state.setObjectName("Help")
        self.creds_state.setWordWrap(True)
        api.body.addWidget(self.creds_state)
        save_creds = make_button("Save credentials && reconnect", "shield")
        save_creds.clicked.connect(self.save_credentials)
        api.body.addWidget(save_creds, 0, Qt.AlignmentFlag.AlignLeft)

        # --- Session ------------------------------------------------------
        session = Card("Session", "Your Telegram login session for this computer")
        self.session_state = QLabel("Checking…")
        self.session_state.setStyleSheet("font-weight: 600;")
        session.body.addWidget(self.session_state)
        path_label = QLabel(f"Stored at {paths.sessions} (owner-only permissions)")
        path_label.setObjectName("Help")
        path_label.setWordWrap(True)
        session.body.addWidget(path_label)
        btns = QHBoxLayout()
        self.login_btn = make_button("Sign in", "login", kind="Primary")
        self.login_btn.clicked.connect(self.login_requested)
        self.logout_btn = make_button("Sign out", "logout", kind="Danger")
        self.logout_btn.clicked.connect(self._confirm_logout)
        btns.addWidget(self.login_btn)
        btns.addWidget(self.logout_btn)
        btns.addStretch(1)
        session.body.addLayout(btns)

        # --- Scanning -----------------------------------------------------
        scanning = Card("Scanning", "How often and how much the background worker reads")
        self.interval = self._spin(15, 3600, " s")
        self.member_sync = self._spin(5, 1440, " min")
        self.backfill = self._spin(0, 5000, " messages")
        self.avatars = self._spin(0, 200, " per cycle")
        scanning.body.addWidget(field("Polling interval", self.interval,
                                      "Incremental scan of new messages. Default 60 seconds."))
        scanning.body.addWidget(field("Member list refresh", self.member_sync,
                                      "The full member list is re-read only this often to avoid rate limits."))
        scanning.body.addWidget(field("History backfill on first scan", self.backfill,
                                      "Recent visible messages analysed when a group is first enabled."))
        scanning.body.addWidget(field("Profile photo downloads", self.avatars))

        # --- Lead criteria -------------------------------------------------
        criteria = Card("Lead criteria", "What counts as a matching user")
        slider_row = QHBoxLayout()
        self.min_score = QSlider(Qt.Orientation.Horizontal)
        self.min_score.setRange(0, 100)
        self.min_score_label = QLabel("50%")
        self.min_score_label.setMinimumWidth(44)
        self.min_score_label.setStyleSheet(f"font-weight: 700; color: {C.PRIMARY};")
        self.min_score.valueChanged.connect(lambda val: self.min_score_label.setText(f"{val}%"))
        slider_row.addWidget(self.min_score, 1)
        slider_row.addWidget(self.min_score_label)
        slider_host = QWidget()
        slider_host.setLayout(slider_row)
        criteria.body.addWidget(field("Minimum relevance score", slider_host))
        self.joined_before = QDateEdit()
        self.joined_before.setCalendarPopup(True)
        self.joined_before.setDisplayFormat("yyyy-MM-dd")
        criteria.body.addWidget(field(
            "Joined before", self.joined_before,
            "Applied only when Telegram provides a real join date. Users with an unknown join date are "
            "shown as “Joined date unavailable” and are not excluded."))

        # --- Monitored groups ----------------------------------------------
        groups = Card("Monitored groups", "Tick the groups the scanner should read")
        self.group_list = QListWidget()
        self.group_list.setMinimumHeight(170)
        self.group_list.setStyleSheet(
            f"QListWidget {{ background: {C.SURFACE}; border: 1px solid {C.BORDER}; border-radius: 9px; padding: 4px; }}"
            f"QListWidget::item {{ padding: 6px; border-radius: 6px; }}"
            f"QListWidget::item:hover {{ background: {C.CARD_HOVER}; }}"
        )
        self.group_list.itemChanged.connect(self._group_item_changed)
        groups.body.addWidget(self.group_list)

        # --- Notifications & startup ----------------------------------------
        notif = Card("Notifications & startup")
        self.notify = ToggleRow("Desktop notifications", "Native notification when a new matching user is found. "
                                "Sent once per user and group.")
        notif.body.addWidget(self.notify)
        test = make_button("Send test notification", "bell", kind="Ghost")
        test.clicked.connect(self.test_notification)
        notif.body.addWidget(test, 0, Qt.AlignmentFlag.AlignLeft)
        self.start_scan = ToggleRow("Start scanning on launch", "Begin the 1-minute polling cycle immediately.")
        self.start_min = ToggleRow("Start minimized to tray", "Launch quietly in the system tray.")
        self.close_tray = ToggleRow("Keep running in tray when closed", "Closing the window keeps monitoring.")
        for w in (self.start_scan, self.start_min, self.close_tray):
            notif.body.addWidget(w)

        # --- Data & privacy ----------------------------------------------
        data = Card("Data & privacy")
        loc = QLabel(f"Local data folder:\n{paths.root}")
        loc.setObjectName("Muted")
        loc.setWordWrap(True)
        loc.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        data.body.addWidget(loc)
        open_btn = make_button("Open data folder", "folder", kind="Ghost")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.root))))
        data.body.addWidget(open_btn, 0, Qt.AlignmentFlag.AlignLeft)
        policy = QLabel(
            "• Only groups your account belongs to are processed; private chats are never read.\n"
            "• Phone numbers are never read or stored. Region comes only from explicit data or your tag.\n"
            "• No image analysis or inference of sensitive attributes is performed.\n"
            "• Evidence is limited to three short, redacted snippets per user and group.")
        policy.setObjectName("Help")
        policy.setWordWrap(True)
        data.body.addWidget(policy)

        self._left = [api, session, groups]
        self._right = [scanning, criteria, notif, data]
        self._cols = 0
        self._relayout()
        self.load()

    # ------------------------------------------------------------------ #

    def _spin(self, lo: int, hi: int, suffix: str) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(lo, hi)
        spin.setSuffix(suffix)
        spin.setMinimumWidth(160)
        return spin

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._relayout()

    def _relayout(self) -> None:
        cols = 2 if self.width() >= 1000 else 1
        if cols == self._cols:
            return
        self._cols = cols
        for col in (self.col_left, self.col_right):
            while col.count():
                col.takeAt(0)
        if cols == 2:
            for w in self._left:
                self.col_left.addWidget(w)
            for w in self._right:
                self.col_right.addWidget(w)
        else:
            for w in self._left + self._right:
                self.col_left.addWidget(w)
        self.col_left.addStretch(1)
        self.col_right.addStretch(1)

    def load(self) -> None:
        s = self.settings.get()
        creds = Credentials.load()
        self.api_id.setText(str(creds.api_id or ""))
        self.api_hash.setText(creds.api_hash)
        self.phone.setText(creds.phone)
        self.bot_token.setText(creds.bot_token)
        self._update_creds_state(creds)
        self.interval.setValue(s.scan_interval_seconds)
        self.member_sync.setValue(s.member_sync_minutes)
        self.backfill.setValue(s.history_backfill)
        self.avatars.setValue(s.avatar_downloads_per_cycle)
        self.min_score.setValue(s.min_relevance)
        jb = s.joined_before_date
        self.joined_before.setDate(QDate(jb.year, jb.month, jb.day))
        self.notify.setChecked(s.notifications_enabled)
        self.start_scan.setChecked(s.start_scanning_on_launch)
        self.start_min.setChecked(s.start_minimized)
        self.close_tray.setChecked(s.close_to_tray)
        self.reload_groups()

    def _update_creds_state(self, creds: Credentials) -> None:
        if creds.complete:
            self.creds_state.setText(
                f"Configured · API ID {mask_secret(str(creds.api_id), 3)} · hash {mask_secret(creds.api_hash)} "
                f"· saved in {ENV_PATH.name}")
        else:
            self.creds_state.setText("Not configured yet. Values are stored in your local .env file only.")

    def reload_groups(self) -> None:
        self.group_list.blockSignals(True)
        self.group_list.clear()
        for g in self.groups.list_all():
            item = QListWidgetItem(f"{g.group_name}   ·   {g.telegram_group_id}")
            item.setData(Qt.ItemDataRole.UserRole, g.telegram_group_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if g.enabled else Qt.CheckState.Unchecked)
            self.group_list.addItem(item)
        if self.group_list.count() == 0:
            placeholder = QListWidgetItem("No groups yet — use Groups → Discover my groups")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.group_list.addItem(placeholder)
        self.group_list.blockSignals(False)

    def _group_item_changed(self, item: QListWidgetItem) -> None:
        gid = item.data(Qt.ItemDataRole.UserRole)
        if gid is None:
            return
        self.groups.set_enabled(gid, item.checkState() == Qt.CheckState.Checked)
        self.groups_changed.emit()

    def set_session(self, signed_in: bool, account: str, state: str) -> None:
        if signed_in:
            self.session_state.setText(f"● Signed in as {account}")
            self.session_state.setStyleSheet(f"font-weight: 600; color: {C.SUCCESS};")
        else:
            self.session_state.setText(f"● {state or 'Not signed in'}")
            self.session_state.setStyleSheet(f"font-weight: 600; color: {C.WARNING};")
        self.login_btn.setVisible(not signed_in)
        self.logout_btn.setVisible(signed_in)

    def save(self) -> None:
        jb = self.joined_before.date()
        self.settings.update(
            scan_interval_seconds=self.interval.value(),
            member_sync_minutes=self.member_sync.value(),
            history_backfill=self.backfill.value(),
            avatar_downloads_per_cycle=self.avatars.value(),
            min_relevance=self.min_score.value(),
            joined_before=f"{jb.year():04d}-{jb.month():02d}-{jb.day():02d}",
            notifications_enabled=self.notify.isChecked(),
            start_scanning_on_launch=self.start_scan.isChecked(),
            start_minimized=self.start_min.isChecked(),
            close_to_tray=self.close_tray.isChecked(),
        )
        self.settings_saved.emit()

    def save_credentials(self) -> None:
        raw_id = self.api_id.text()
        if raw_id and not raw_id.isdigit():
            QMessageBox.warning(self, "Invalid API ID", "The API ID must be a number.")
            return
        api_hash = self.api_hash.text()
        if api_hash and len(api_hash) != 32:
            answer = QMessageBox.question(self, "Unusual API hash",
                                          "Telegram API hashes are normally 32 characters. Save anyway?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        creds = Credentials(api_id=int(raw_id) if raw_id else None, api_hash=api_hash,
                            phone=self.phone.text().strip(), bot_token=self.bot_token.text())
        try:
            creds.save()
        except OSError as exc:
            QMessageBox.critical(self, "Could not save", f"Writing {ENV_PATH} failed:\n{exc}")
            return
        self._update_creds_state(creds)
        self.credentials_saved.emit()

    def _confirm_logout(self) -> None:
        answer = QMessageBox.question(self, "Sign out",
                                      "Sign out of Telegram and delete the local session? Collected data is kept.")
        if answer == QMessageBox.StandardButton.Yes:
            self.logout_requested.emit()
