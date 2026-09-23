"""Telegram sign-in dialog: phone → verification code → optional 2FA password (or bot token)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QLineEdit,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.config import Credentials
from app.services.worker import TelegramWorker
from app.ui.theme import C, app_icon
from app.ui.widgets import make_button


class LoginDialog(QDialog):
    PAGE_PHONE, PAGE_CODE, PAGE_PASSWORD, PAGE_BOT, PAGE_DONE = range(5)

    def __init__(self, worker: TelegramWorker, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.worker = worker
        self.setWindowTitle("Sign in to Telegram")
        self.setModal(True)
        self.setMinimumWidth(460)
        creds = Credentials.load()

        v = QVBoxLayout(self)
        v.setContentsMargins(32, 28, 32, 24)
        v.setSpacing(14)
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(52, 52))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(logo)
        self.title = QLabel("Sign in to Telegram")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setStyleSheet("font-size: 19px; font-weight: 700;")
        v.addWidget(self.title)
        self.subtitle = QLabel("")
        self.subtitle.setObjectName("Muted")
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle.setWordWrap(True)
        v.addWidget(self.subtitle)

        self.stack = QStackedWidget()
        v.addWidget(self.stack)

        # Phone
        phone_page = QWidget()
        pl = QVBoxLayout(phone_page)
        pl.setContentsMargins(0, 8, 0, 0)
        self.phone = QLineEdit(creds.phone)
        self.phone.setPlaceholderText("+1 555 123 4567")
        self.phone.returnPressed.connect(self._send_code)
        pl.addWidget(self._label("Phone number (international format)"))
        pl.addWidget(self.phone)
        self.send_btn = make_button("Send verification code", None, kind="Primary")
        self.send_btn.clicked.connect(self._send_code)
        pl.addWidget(self.send_btn)
        bot_link = make_button("Use a bot token instead", None, kind="Ghost")
        bot_link.clicked.connect(lambda: self._go(self.PAGE_BOT))
        pl.addWidget(bot_link)
        self.stack.addWidget(phone_page)

        # Code
        code_page = QWidget()
        cl = QVBoxLayout(code_page)
        cl.setContentsMargins(0, 8, 0, 0)
        self.code = QLineEdit()
        self.code.setObjectName("CodeInput")
        self.code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code.setMaxLength(8)
        self.code.setPlaceholderText("•••••")
        self.code.returnPressed.connect(self._submit_code)
        cl.addWidget(self._label("Verification code"))
        cl.addWidget(self.code)
        self.code_btn = make_button("Verify", None, kind="Primary")
        self.code_btn.clicked.connect(self._submit_code)
        cl.addWidget(self.code_btn)
        back = make_button("Use a different number", None, kind="Ghost")
        back.clicked.connect(lambda: self._go(self.PAGE_PHONE))
        cl.addWidget(back)
        self.stack.addWidget(code_page)

        # Password (2FA)
        pw_page = QWidget()
        wl = QVBoxLayout(pw_page)
        wl.setContentsMargins(0, 8, 0, 0)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Two-step verification password")
        self.password.returnPressed.connect(self._submit_password)
        wl.addWidget(self._label("Cloud password"))
        wl.addWidget(self.password)
        self.pw_btn = make_button("Unlock", None, kind="Primary")
        self.pw_btn.clicked.connect(self._submit_password)
        wl.addWidget(self.pw_btn)
        self.stack.addWidget(pw_page)

        # Bot
        bot_page = QWidget()
        bl = QVBoxLayout(bot_page)
        bl.setContentsMargins(0, 8, 0, 0)
        self.bot_token = QLineEdit(creds.bot_token)
        self.bot_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.bot_token.setPlaceholderText("123456:ABC-DEF…")
        bl.addWidget(self._label("Bot token from @BotFather"))
        bl.addWidget(self.bot_token)
        hint = QLabel("Bots only see messages and joins that happen while the app runs (and only messages the "
                      "bot's privacy mode allows). Telegram does not let bots read chat history, and member lists "
                      "are often unavailable to them.")
        hint.setObjectName("Help")
        hint.setWordWrap(True)
        bl.addWidget(hint)
        self.bot_btn = make_button("Sign in as bot", None, kind="Primary")
        self.bot_btn.clicked.connect(self._login_bot)
        bl.addWidget(self.bot_btn)
        back2 = make_button("Use phone login instead", None, kind="Ghost")
        back2.clicked.connect(lambda: self._go(self.PAGE_PHONE))
        bl.addWidget(back2)
        self.stack.addWidget(bot_page)

        # Done
        done_page = QWidget()
        dl = QVBoxLayout(done_page)
        ok = make_button("Continue", None, kind="Primary")
        ok.clicked.connect(self.accept)
        dl.addWidget(ok)
        self.stack.addWidget(done_page)

        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error.setStyleSheet(f"color: {C.DANGER};")
        self.error.hide()
        v.addWidget(self.error)

        privacy = QLabel("Your session is stored locally in your user data folder with owner-only permissions. "
                         "Codes and passwords are sent only to Telegram and are never saved.")
        privacy.setObjectName("Help")
        privacy.setWordWrap(True)
        privacy.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(privacy)

        worker.auth_event.connect(self._on_auth_event)
        self._go(self.PAGE_PHONE)

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("FieldLabel")
        return label

    def _go(self, page: int) -> None:
        self.error.hide()
        self._busy(False)
        subtitles = {
            self.PAGE_PHONE: "We'll ask Telegram to send a login code to your Telegram app.",
            self.PAGE_CODE: "Enter the code Telegram just sent to your Telegram app (or by SMS).",
            self.PAGE_PASSWORD: "Your account has two-step verification enabled.",
            self.PAGE_BOT: "Sign in with a bot that has been added to your groups.",
            self.PAGE_DONE: "You're signed in. Scanning will start automatically.",
        }
        self.subtitle.setText(subtitles[page])
        self.stack.setCurrentIndex(page)
        focus = {self.PAGE_PHONE: self.phone, self.PAGE_CODE: self.code,
                 self.PAGE_PASSWORD: self.password, self.PAGE_BOT: self.bot_token}.get(page)
        if focus:
            focus.setFocus()

    def _busy(self, busy: bool) -> None:
        for btn in (self.send_btn, self.code_btn, self.pw_btn, self.bot_btn):
            btn.setEnabled(not busy)

    def _show_error(self, text: str) -> None:
        self._busy(False)
        self.error.setText(text)
        self.error.show()

    def _send_code(self) -> None:
        phone = self.phone.text().strip()
        if not phone:
            self._show_error("Enter your phone number.")
            return
        creds = Credentials.load()
        if creds.phone != phone:
            creds.phone = phone
            creds.save()
        self._busy(True)
        self.error.hide()
        self.worker.request_code(phone)

    def _submit_code(self) -> None:
        if not self.code.text().strip():
            return
        self._busy(True)
        self.error.hide()
        self.worker.submit_code(self.code.text())

    def _submit_password(self) -> None:
        pw = self.password.text()
        if not pw:
            return
        self._busy(True)
        self.error.hide()
        self.worker.submit_password(pw)
        self.password.clear()

    def _login_bot(self) -> None:
        token = self.bot_token.text().strip()
        if not token:
            self._show_error("Enter the bot token.")
            return
        creds = Credentials.load()
        creds.bot_token = token
        creds.save()
        self._busy(True)
        self.worker.login_bot(token)

    def _on_auth_event(self, kind: str, message: str) -> None:
        if kind == "code_sent":
            self._go(self.PAGE_CODE)
        elif kind == "password_required":
            self._go(self.PAGE_PASSWORD)
        elif kind == "authorized":
            self.title.setText(f"Signed in as {message}")
            self._go(self.PAGE_DONE)
        elif kind == "error":
            self._show_error(message)
