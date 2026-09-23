"""Main window: sidebar navigation, page stack, tray icon and worker wiring."""

from __future__ import annotations

import logging

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from app.config import APP_NAME, APP_VERSION, Paths, SettingsStore
from app.database.db import Database
from app.database.models import LeadCriteria
from app.database.repositories import LeadQueryRepository
from app.services import worker as W
from app.services.logging_service import QtLogHandler
from app.services.notifications import LeadNotification
from app.services.worker import TelegramWorker
from app.ui.dashboard import STATE_COLORS, DashboardPage
from app.ui.groups_page import GroupsPage
from app.ui.login_dialog import LoginDialog
from app.ui.logs_page import LogsPage
from app.ui.settings_page import SettingsPage
from app.ui.theme import C, app_icon, icon
from app.ui.users_page import UsersPage
from app.ui.widgets import AVATARS, PulseDot, Toast

log = logging.getLogger("app.ui")

NAV = [("dashboard", "Dashboard"), ("users", "Users"), ("groups", "Groups"), ("logs", "Logs"), ("settings", "Settings")]


class SidebarGroupItem(QWidget):
    def __init__(self, name: str, gid: int, users: int, matches: int) -> None:
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 7, 10, 7)
        v.setSpacing(1)
        top = QHBoxLayout()
        top.setSpacing(6)
        n = QLabel(name)
        n.setStyleSheet(f"font-weight: 600; font-size: 12px; color: {C.TEXT};")
        n.setMaximumWidth(150)
        top.addWidget(n, 1)
        m = QLabel(f"{matches:,}")
        m.setToolTip("Matches")
        m.setStyleSheet(f"background: rgba(34,197,94,40); color: {C.SUCCESS}; border-radius: 8px;"
                        "padding: 1px 7px; font-size: 10px; font-weight: 700;")
        top.addWidget(m)
        v.addLayout(top)
        meta = QLabel(f"{users:,} users · {gid}")
        meta.setStyleSheet(f"color: {C.MUTED}; font-size: 10px;")
        v.addWidget(meta)
        n.setText(n.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, 150))


class MainWindow(QMainWindow):
    def __init__(self, db: Database, settings: SettingsStore, paths: Paths, worker: TelegramWorker,
                 log_handler: QtLogHandler) -> None:
        super().__init__()
        self.db = db
        self.settings = settings
        self.paths = paths
        self.worker = worker
        self.leads = LeadQueryRepository(db)
        self._quitting = False
        self._tray_hint_shown = False
        self._toasts: list[Toast] = []
        self._status: dict = worker.status()

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(1480, 920)
        self.setMinimumSize(980, 640)

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(central)

        root.addWidget(self._build_sidebar())

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self.dashboard = DashboardPage(db, settings)
        self.users = UsersPage(db, settings, paths)
        self.groups = GroupsPage(db, settings)
        self.logs = LogsPage(log_handler, paths)
        self.settings_page = SettingsPage(db, settings, paths)
        self.pages = {"dashboard": self.dashboard, "users": self.users, "groups": self.groups,
                      "logs": self.logs, "settings": self.settings_page}
        for page in self.pages.values():
            self.stack.addWidget(page)

        self._fade_effect = QGraphicsOpacityEffect(self.stack)
        self._fade_effect.setOpacity(1.0)
        self._fade_effect.setEnabled(False)
        self.stack.setGraphicsEffect(self._fade_effect)
        self._fade = QPropertyAnimation(self._fade_effect, b"opacity", self)
        self._fade.setDuration(180)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.finished.connect(self._fade_done)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(900)
        self._refresh_timer.timeout.connect(self.refresh_all)

        self._build_tray()
        self._wire()
        self.navigate("dashboard")
        self.refresh_all()
        self._on_status(self._status)

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    def _build_sidebar(self) -> QFrame:
        side = QFrame()
        side.setObjectName("Sidebar")
        side.setFixedWidth(248)
        v = QVBoxLayout(side)
        v.setContentsMargins(14, 18, 14, 14)
        v.setSpacing(6)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(34, 34))
        brand.addWidget(logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        t = QLabel(APP_NAME)
        t.setObjectName("BrandName")
        s = QLabel(f"Lead intelligence · v{APP_VERSION}")
        s.setObjectName("BrandSub")
        names.addWidget(t)
        names.addWidget(s)
        brand.addLayout(names, 1)
        v.addLayout(brand)
        v.addSpacing(16)

        section = QLabel("MENU")
        section.setObjectName("SidebarSection")
        v.addWidget(section)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: dict[str, QPushButton] = {}
        for key, label in NAV:
            btn = QPushButton(f"  {label}")
            btn.setObjectName("NavButton")
            btn.setCheckable(True)
            btn.setIcon(icon(key, C.MUTED, 18, active_color=C.TEXT))
            btn.setIconSize(QSize(18, 18))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, k=key: self.navigate(k))
            self.nav_group.addButton(btn)
            self.nav_buttons[key] = btn
            v.addWidget(btn)

        v.addSpacing(14)
        gs = QLabel("MONITORED GROUPS")
        gs.setObjectName("SidebarSection")
        v.addWidget(gs)
        self.group_list = QListWidget()
        self.group_list.setObjectName("SidebarGroups")
        self.group_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.group_list.setCursor(Qt.CursorShape.PointingHandCursor)
        self.group_list.itemClicked.connect(self._sidebar_group_clicked)
        v.addWidget(self.group_list, 1)

        account = QFrame()
        account.setObjectName("AccountCard")
        ah = QHBoxLayout(account)
        ah.setContentsMargins(12, 10, 12, 10)
        ah.setSpacing(8)
        self.side_dot = PulseDot(C.MUTED, 8)
        ah.addWidget(self.side_dot)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        self.side_state = QLabel("Starting…")
        self.side_state.setStyleSheet("font-weight: 600; font-size: 12px;")
        self.side_account = QLabel("")
        self.side_account.setStyleSheet(f"color: {C.MUTED}; font-size: 11px;")
        texts.addWidget(self.side_state)
        texts.addWidget(self.side_account)
        ah.addLayout(texts, 1)
        v.addWidget(account)
        return side

    def _build_tray(self) -> None:
        self.tray: QSystemTrayIcon | None = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.info("System tray not available; using in-app notifications")
            return
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        menu.addAction("Open dashboard", self.show_window)
        menu.addAction("Scan now", self.worker.scan_now)
        self.tray_pause = QAction("Pause scanning", menu)
        self.tray_pause.triggered.connect(lambda: self.worker.set_scanning_enabled(
            not self._status.get("scanning_enabled", True)))
        menu.addAction(self.tray_pause)
        menu.addSeparator()
        menu.addAction("Quit", self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.messageClicked.connect(self.show_window)
        self.tray.show()

    def _wire(self) -> None:
        w = self.worker
        w.status_changed.connect(self._on_status)
        w.auth_event.connect(self._on_auth_event)
        w.data_changed.connect(self.schedule_refresh)
        w.groups_updated.connect(self._on_groups_updated)
        w.operation_failed.connect(lambda op, msg: self.toast(f"{op} failed", msg, C.DANGER))
        w.lead_notification.connect(self.notify)
        w.profile_loaded.connect(self._on_profile_loaded)

        d = self.dashboard
        d.scan_requested.connect(w.scan_now)
        d.scanning_toggled.connect(w.set_scanning_enabled)
        d.open_group.connect(self.open_group)
        d.open_user.connect(self.open_user)
        d.open_users.connect(self._open_users_preset)
        d.go_settings.connect(lambda: self.navigate("settings"))
        d.go_groups.connect(lambda: self.navigate("groups"))

        u = self.users
        u.scan_requested.connect(w.scan_now)
        u.profile_requested.connect(w.fetch_public_profile)
        u.toast.connect(lambda t, b: self.toast(t, b, C.SUCCESS))

        g = self.groups
        g.discover_requested.connect(w.discover_groups)
        g.add_requested.connect(w.add_group)
        g.groups_changed.connect(self._groups_changed)
        g.scan_requested.connect(w.scan_now)
        g.open_group.connect(self.open_group)

        s = self.settings_page
        s.settings_saved.connect(self._settings_saved)
        s.credentials_saved.connect(self._credentials_saved)
        s.login_requested.connect(self.open_login)
        s.logout_requested.connect(w.logout)
        s.groups_changed.connect(self._groups_changed)
        s.test_notification.connect(lambda: self.notify(LeadNotification(
            "New Telegram Lead Found", "JohnDev\nGroup: Remote Developers\nRelevance: 87%\nJoined: 2024-06-15")))

        QShortcut(QKeySequence("F5"), self, activated=w.scan_now)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._focus_search)
        QShortcut(QKeySequence("Esc"), self, activated=self._escape)
        for i, (key, _) in enumerate(NAV, start=1):
            QShortcut(QKeySequence(f"Ctrl+{i}"), self, activated=lambda k=key: self.navigate(k))

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #

    def navigate(self, key: str) -> None:
        page = self.pages[key]
        self.nav_buttons[key].setChecked(True)
        if self.stack.currentWidget() is page:
            return
        self._pending_page = page
        self._fade_effect.setEnabled(True)
        self._fade.stop()
        self._fade.setStartValue(self._fade_effect.opacity())
        self._fade.setEndValue(0.25)
        self._fade.setDuration(90)
        self._fade_phase = "out"
        self._fade.start()

    def _fade_done(self) -> None:
        if getattr(self, "_fade_phase", "") != "out":
            # Effects render off-screen; disable once the transition is over to keep tables fast.
            self._fade_effect.setEnabled(False)
            return
        page = self._pending_page
        self._refresh_page(page)
        self.stack.setCurrentWidget(page)
        self._fade_phase = "in"
        self._fade.setStartValue(0.25)
        self._fade.setEndValue(1.0)
        self._fade.setDuration(180)
        self._fade.start()

    def open_group(self, group_id) -> None:
        self.users.reload_groups()
        self.users.show_group(group_id)
        self.navigate("users")

    def open_user(self, user_id: int, group_id: int) -> None:
        self.navigate("users")
        self.users.open_user(user_id, group_id)

    def _open_users_preset(self, preset: str) -> None:
        self.users.reload_groups()
        self.users.apply_preset(preset)
        self.navigate("users")

    def _sidebar_group_clicked(self, item: QListWidgetItem) -> None:
        self.open_group(item.data(Qt.ItemDataRole.UserRole))

    def _focus_search(self) -> None:
        self.navigate("users")
        self.users.search.setFocus()
        self.users.search.selectAll()

    def _escape(self) -> None:
        if self.stack.currentWidget() is self.users and self.users.drawer.is_open:
            self.users.drawer.close_drawer()

    # ------------------------------------------------------------------ #
    # Refresh
    # ------------------------------------------------------------------ #

    def schedule_refresh(self) -> None:
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def refresh_all(self) -> None:
        AVATARS.invalidate()
        self._refresh_sidebar_groups()
        self._refresh_page(self.stack.currentWidget())

    def _refresh_page(self, page) -> None:
        try:
            if page is self.dashboard:
                self.dashboard.refresh()
            elif page is self.users:
                self.users.reload_groups()
                self.users.refresh()
            elif page is self.groups:
                self.groups.refresh()
            elif page is self.settings_page:
                self.settings_page.reload_groups()
        except Exception:
            log.exception("Failed to refresh the view")

    def _refresh_sidebar_groups(self) -> None:
        s = self.settings.get()
        stats = [g for g in self.leads.group_stats(LeadCriteria(s.min_relevance, s.joined_before_date)) if g.enabled]
        self.group_list.clear()
        all_item = QListWidgetItem()
        all_item.setData(Qt.ItemDataRole.UserRole, None)
        total_users = sum(g.users for g in stats)
        total_matches = sum(g.matches for g in stats)
        widget = SidebarGroupItem("All Groups", 0, total_users, total_matches)
        widget.findChildren(QLabel)[-1].setText(f"{total_users:,} users · {len(stats)} groups")
        all_item.setSizeHint(widget.sizeHint())
        self.group_list.addItem(all_item)
        self.group_list.setItemWidget(all_item, widget)
        for g in stats:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, g.telegram_group_id)
            item.setToolTip(f"{g.group_name}\nGroup ID: {g.telegram_group_id}\nUsers: {g.users:,}\n"
                            f"Matches: {g.matches:,}")
            widget = SidebarGroupItem(g.group_name, g.telegram_group_id, g.users, g.matches)
            item.setSizeHint(widget.sizeHint())
            self.group_list.addItem(item)
            self.group_list.setItemWidget(item, widget)

    # ------------------------------------------------------------------ #
    # Worker events
    # ------------------------------------------------------------------ #

    def _on_status(self, status: dict) -> None:
        self._status = status
        state = status.get("state", W.STATE_STOPPED)
        color = STATE_COLORS.get(state, C.MUTED)
        self.side_dot.set_color(color, state in (W.STATE_SCANNING, W.STATE_CONNECTING, W.STATE_RECONNECTING))
        self.side_state.setText(state)
        account = status.get("account") or ""
        self.side_account.setText(account or (status.get("detail") or "")[:40])
        self.dashboard.set_status(status)
        signed_in = bool(account) and state not in (W.STATE_NEEDS_LOGIN, W.STATE_NEEDS_CREDENTIALS)
        self.settings_page.set_session(signed_in, account, state)
        if self.tray:
            self.tray.setToolTip(f"{APP_NAME} — {state}")
            self.tray_pause.setText("Pause scanning" if status.get("scanning_enabled") else "Resume scanning")

    def _on_auth_event(self, kind: str, message: str) -> None:
        if kind == "authorized":
            self.toast("Connected", f"Signed in as {message}", C.SUCCESS)
            self.schedule_refresh()
        elif kind == "logged_out":
            self.toast("Signed out", "The local Telegram session was removed.", C.WARNING)
        elif kind == "unauthorized" and message and "expired" in message.lower():
            self.toast("Session expired", message, C.DANGER)

    def _on_groups_updated(self, message: str) -> None:
        self.groups.show_message(message)
        self.toast("Groups", message, C.PRIMARY)
        self.schedule_refresh()

    def _on_profile_loaded(self, user_id: int, message: str) -> None:
        self.toast("Public profile", message, C.PRIMARY)
        if self.users.drawer.user_id == user_id:
            self.users.drawer.refresh()

    def _groups_changed(self) -> None:
        self.worker.groups_changed()
        self.schedule_refresh()

    def _settings_saved(self) -> None:
        self.worker.settings_changed()
        self.toast("Settings saved", "New settings apply from the next scan cycle.", C.SUCCESS)
        self.refresh_all()

    def _credentials_saved(self) -> None:
        self.toast("Credentials saved", "Reconnecting to Telegram…", C.SUCCESS)
        self.worker.reconnect()

    def open_login(self) -> None:
        if self._status.get("state") == W.STATE_NEEDS_CREDENTIALS:
            QMessageBox.information(self, "API credentials required",
                                    "Enter your API ID and API hash, then click “Save credentials & reconnect”.")
            self.navigate("settings")
            return
        dialog = LoginDialog(self.worker, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.exec()

    # ------------------------------------------------------------------ #
    # Notifications
    # ------------------------------------------------------------------ #

    def notify(self, note: LeadNotification) -> None:
        if self.tray is not None and self.tray.supportsMessages():
            self.tray.showMessage(note.title, note.body, app_icon(), 8000)
        if self.isVisible() or self.tray is None:
            self.toast(note.title, note.body.replace("\n", " · "), C.SUCCESS)

    def toast(self, title: str, body: str, accent: str = C.PRIMARY) -> None:
        if not self.isVisible():
            return
        t = Toast(self.centralWidget(), title, body, accent)
        self._toasts = [x for x in self._toasts if _alive(x)]
        offset = sum(x.height() + 10 for x in self._toasts)
        self._toasts.append(t)
        t.destroyed.connect(lambda _=None, x=t: self._toasts.remove(x) if x in self._toasts else None)
        t.show_at(min(offset, 400))

    # ------------------------------------------------------------------ #
    # Window / tray behaviour
    # ------------------------------------------------------------------ #

    def _tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self.show_window()

    def show_window(self) -> None:
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.raise_()
        self.activateWindow()
        self.schedule_refresh()

    def quit_app(self) -> None:
        self._quitting = True
        self.close()
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if not self._quitting and self.tray is not None and self.settings.get().close_to_tray:
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self.tray.showMessage(APP_NAME, "Still monitoring in the background. Use the tray icon to quit.",
                                      app_icon(), 4000)
                self._tray_hint_shown = True
            return
        self._quitting = True
        event.accept()
        QApplication.quit()


def _alive(widget) -> bool:
    try:
        widget.isVisible()
        return True
    except RuntimeError:
        return False
