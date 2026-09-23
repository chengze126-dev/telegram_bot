"""Telethon client lifecycle: creation, secure session storage, reconnects."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from telethon import TelegramClient, errors

from app.config import APP_NAME, APP_VERSION, Credentials

log = logging.getLogger("app.telegram.client")

CONNECT_TIMEOUT = 45

# Errors that mean the stored session can no longer be used and the user must log in again.
SESSION_INVALID_ERRORS: tuple[type[BaseException], ...] = (
    errors.AuthKeyUnregisteredError,
    errors.AuthKeyInvalidError,
    errors.SessionRevokedError,
    errors.SessionExpiredError,
    errors.UserDeactivatedError,
    errors.UserDeactivatedBanError,
    errors.AuthKeyDuplicatedError,
)


class MissingCredentialsError(RuntimeError):
    pass


def _harden_session_file(session_base: Path) -> None:
    """Restrict the Telethon session file to the current OS user."""
    if os.name != "posix":
        return
    for suffix in (".session", ".session-journal"):
        path = session_base.with_name(session_base.name + suffix)
        if path.exists():
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass


class TelegramClientManager:
    """Owns the single :class:`TelegramClient` used by the background worker."""

    def __init__(self, session_base: Path) -> None:
        self.session_base = session_base
        self.client: TelegramClient | None = None
        self.credentials: Credentials | None = None
        self.me = None
        self.is_bot = False
        self._connect_lock: asyncio.Lock | None = None

    def build(self, credentials: Credentials) -> TelegramClient:
        if not credentials.complete:
            raise MissingCredentialsError(
                "TELEGRAM_API_ID and TELEGRAM_API_HASH are not configured. Add them in Settings or .env."
            )
        self.credentials = credentials
        self.client = TelegramClient(
            str(self.session_base),
            credentials.api_id,
            credentials.api_hash,
            device_model=APP_NAME,
            app_version=APP_VERSION,
            system_version="Desktop",
            connection_retries=5,
            retry_delay=3,
            auto_reconnect=True,
            request_retries=3,
            flood_sleep_threshold=120,
            receive_updates=True,
        )
        return self.client

    @property
    def connected(self) -> bool:
        return bool(self.client and self.client.is_connected())

    async def connect(self, max_attempts: int = 0) -> None:
        """Connect with exponential backoff. ``max_attempts=0`` retries forever."""
        assert self.client is not None
        if self._connect_lock is None:
            self._connect_lock = asyncio.Lock()
        async with self._connect_lock:
            await self._connect_with_backoff(max_attempts)

    async def _connect_with_backoff(self, max_attempts: int) -> None:
        assert self.client is not None
        delay = 2
        attempt = 0
        while not self.client.is_connected():
            attempt += 1
            try:
                # Telethon's handshake has no overall deadline; a half-open connection
                # would otherwise hang here forever.
                await asyncio.wait_for(self.client.connect(), timeout=CONNECT_TIMEOUT)
                _harden_session_file(self.session_base)
                return
            except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
                if isinstance(exc, asyncio.TimeoutError):
                    exc = TimeoutError(f"no response from Telegram within {CONNECT_TIMEOUT}s")
                    await self.disconnect()
                if max_attempts and attempt >= max_attempts:
                    raise
                log.warning("Connection to Telegram failed (%s). Retrying in %ss", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)

    async def ensure_connected(self) -> bool:
        """Reconnect if the connection dropped. Returns True if a reconnect happened."""
        if self.client is None:
            return False
        if self.client.is_connected():
            return False
        log.info("Reconnecting to Telegram…")
        await self.connect()
        log.info("Reconnected to Telegram")
        return True

    async def is_authorized(self) -> bool:
        if not self.client:
            return False
        try:
            authorized = await self.client.is_user_authorized()
        except SESSION_INVALID_ERRORS:
            return False
        if authorized:
            self.me = await self.client.get_me()
            self.is_bot = bool(getattr(self.me, "bot", False))
            _harden_session_file(self.session_base)
        return authorized

    def account_label(self) -> str:
        if not self.me:
            return ""
        if getattr(self.me, "username", None):
            return f"@{self.me.username}"
        name = " ".join(p for p in (self.me.first_name, self.me.last_name) if p)
        return name or str(self.me.id)

    async def disconnect(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception as exc:  # disconnect must never crash shutdown
                log.debug("Error during disconnect: %s", exc)

    def delete_session(self) -> None:
        for suffix in (".session", ".session-journal"):
            path = self.session_base.with_name(self.session_base.name + suffix)
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("Could not delete session file %s: %s", path, exc)
