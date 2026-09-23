"""Background worker: runs Telethon on its own asyncio loop in a separate thread.

The Qt UI never blocks on network calls. It calls the public methods below,
which schedule coroutines on the worker loop, and receives results through Qt
signals (delivered to the UI thread via queued connections).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future
from datetime import datetime
from typing import Any, Coroutine

from PySide6.QtCore import QObject, Signal
from telethon import errors, functions, types

from app.config import Credentials, Paths, SettingsStore
from app.database.db import Database
from app.database.repositories import GroupRepository
from app.services.avatar_service import AvatarService
from app.services.ingest import IngestService
from app.services.notifications import NotificationService
from app.services.scanner import ScanService
from app.telegram.auth import AuthError, AuthFlow, PasswordRequired
from app.telegram.client import SESSION_INVALID_ERRORS, MissingCredentialsError, TelegramClientManager
from app.telegram.events import LiveEventRouter
from app.telegram.groups import GroupAccessError, discover_groups, resolve_group
from app.telegram.members import HistoryBatch
from app.telegram.rate_limit import FloodWaitTooLong, RateLimiter

log = logging.getLogger("app.worker")

# Connection / scan states shown in the UI.
STATE_STOPPED = "Stopped"
STATE_CONNECTING = "Connecting"
STATE_RECONNECTING = "Reconnecting"
STATE_NEEDS_LOGIN = "Login required"
STATE_NEEDS_CREDENTIALS = "API credentials required"
STATE_IDLE = "Idle"
STATE_SCANNING = "Scanning"
STATE_PAUSED = "Paused"
STATE_RATE_LIMITED = "Rate limited"
STATE_ERROR = "Error"


class TelegramWorker(QObject):
    # {"state", "detail", "account", "last_scan", "next_scan", "is_bot", "scanning_enabled"}
    status_changed = Signal(dict)
    # auth events: "authorized" | "code_sent" | "password_required" | "error" | "logged_out" | "unauthorized"
    auth_event = Signal(str, str)
    data_changed = Signal()
    groups_updated = Signal(str)  # human-readable result message
    operation_failed = Signal(str, str)  # operation, message
    lead_notification = Signal(object)  # LeadNotification
    profile_loaded = Signal(int, str)  # user id, message

    def __init__(self, db: Database, settings: SettingsStore, paths: Paths) -> None:
        super().__init__()
        self.db = db
        self.settings = settings
        self.paths = paths
        self.groups_repo = GroupRepository(db)
        self.manager = TelegramClientManager(paths.session_file)
        self.auth = AuthFlow(self.manager)
        self.limiter = RateLimiter(settings.get().api_min_interval)
        self.limiter.on_wait = self._on_rate_limit
        self.ingest = IngestService(db)
        self.notifier = NotificationService(db, settings, self.lead_notification.emit)
        self.avatars = AvatarService(db, paths.avatars, self.limiter)
        self.scanner = ScanService(db, settings, self.limiter, self.ingest, self.notifier, self.avatars,
                                   on_progress=lambda d: self._set_state(STATE_SCANNING, d))
        self.events: LiveEventRouter | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._wake: asyncio.Event | None = None
        self._stop: asyncio.Event | None = None
        self._scan_lock: asyncio.Lock | None = None
        self._authorized = False
        self._enabled_ids: set[int] = set()
        self._status: dict[str, Any] = {
            "state": STATE_STOPPED, "detail": "", "account": "", "last_scan": None, "next_scan": None,
            "is_bot": False, "scanning_enabled": settings.get().start_scanning_on_launch,
        }

    # ------------------------------------------------------------------ #
    # Thread / loop management
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="telegram-worker", daemon=True)
        self._thread.start()
        self._ready.wait(10)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._scan_lock = asyncio.Lock()
        self._ready.set()
        try:
            loop.run_until_complete(self._main())
        except Exception:
            log.exception("Worker loop crashed")
        finally:
            try:
                loop.run_until_complete(self.manager.disconnect())
            except Exception:
                pass
            loop.close()

    def _submit(self, coro: Coroutine, operation: str) -> Future | None:
        if self._loop is None or self._loop.is_closed():
            coro.close()
            return None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)

        def _done(f: Future) -> None:
            exc = f.exception()
            if exc is not None:
                log.error("%s failed: %s", operation, exc)
                self.operation_failed.emit(operation, str(exc))

        future.add_done_callback(_done)
        return future

    def shutdown(self, timeout: float = 5.0) -> None:
        if self._loop is None or self._stop is None:
            return
        self._loop.call_soon_threadsafe(self._stop.set)
        self._loop.call_soon_threadsafe(self._wake.set)
        if self._thread is not None:
            self._thread.join(timeout)

    # ------------------------------------------------------------------ #
    # Status helpers
    # ------------------------------------------------------------------ #

    def _set_state(self, state: str, detail: str = "", **extra: Any) -> None:
        self._status.update(state=state, detail=detail, **extra)
        self.status_changed.emit(dict(self._status))

    def status(self) -> dict:
        return dict(self._status)

    def _on_rate_limit(self, seconds: int, what: str) -> None:
        self._set_state(STATE_RATE_LIMITED, f"Waiting {seconds}s (Telegram rate limit while {what})")

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #

    async def _main(self) -> None:
        await self._connect()
        while not self._stop.is_set():
            interval = self.settings.get().scan_interval_seconds
            if self._authorized and self._status["scanning_enabled"]:
                await self._cycle()
                next_at = datetime.now().timestamp() + interval
                self._status["next_scan"] = next_at
                if self._status["state"] not in (STATE_NEEDS_LOGIN, STATE_RECONNECTING, STATE_ERROR, STATE_RATE_LIMITED):
                    self._set_state(STATE_IDLE, "Waiting for next scan")
            elif self._authorized:
                self._status["next_scan"] = None
                self._set_state(STATE_PAUSED, "Scanning is paused")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _connect(self) -> None:
        await self.manager.disconnect()
        if self.events is not None:
            self.events.unregister()
            self.events = None
        self._authorized = False
        creds = Credentials.load()
        try:
            client = self.manager.build(creds)
        except MissingCredentialsError as exc:
            self._set_state(STATE_NEEDS_CREDENTIALS, str(exc))
            self.auth_event.emit("unauthorized", str(exc))
            return
        self.events = LiveEventRouter(client, lambda: self._enabled_ids, self._on_live_batch)
        self._set_state(STATE_CONNECTING, "Connecting to Telegram")
        await self.manager.connect()
        log.info("Connected to Telegram")
        if await self.manager.is_authorized():
            await self._on_authorized()
        else:
            self._set_state(STATE_NEEDS_LOGIN, "Sign in with your phone number in Settings")
            self.auth_event.emit("unauthorized", "Not signed in")

    async def _on_authorized(self) -> None:
        self._authorized = True
        self._enabled_ids = self.groups_repo.enabled_ids()
        account = self.manager.account_label()
        self._set_state(STATE_IDLE, "Connected", account=account, is_bot=self.manager.is_bot)
        log.info("Signed in as %s%s", account, " (bot)" if self.manager.is_bot else "")
        self.auth_event.emit("authorized", account)
        if self.events is None and self.manager.client is not None:
            self.events = LiveEventRouter(self.manager.client, lambda: self._enabled_ids, self._on_live_batch)
        if self.events is not None:
            self.events.register()
        if not self.manager.is_bot and not self.groups_repo.list_all():
            # First login: list the groups this account belongs to so the user can pick.
            await self._discover()

    async def _cycle(self) -> None:
        async with self._scan_lock:
            client = self.manager.client
            if client is None:
                return
            try:
                if await self.manager.ensure_connected():
                    log.info("Reconnect event: connection restored")
                self._enabled_ids = self.groups_repo.enabled_ids()
                self._set_state(STATE_SCANNING, "Starting scan")
                await self.scanner.run_cycle(client, self.manager.is_bot)
                self._status["last_scan"] = datetime.now().timestamp()
                self._set_state(STATE_IDLE, "Scan complete")
            except SESSION_INVALID_ERRORS as exc:
                await self._handle_session_invalid(exc)
            except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
                log.warning("Network problem during scan (%s). Will reconnect.", exc)
                self._set_state(STATE_RECONNECTING, "Network problem — reconnecting")
                try:
                    await self.manager.disconnect()
                    await self.manager.connect()
                    log.info("Reconnect event: connection restored")
                    self._set_state(STATE_IDLE, "Reconnected")
                except Exception as rexc:
                    log.error("Reconnect failed: %s", rexc)
            except FloodWaitTooLong as exc:
                log.warning("%s — scan cycle cut short", exc)
                self._set_state(STATE_RATE_LIMITED, str(exc))
            except errors.RPCError as exc:
                log.error("Telegram API error during scan: %s", exc)
                self._set_state(STATE_ERROR, f"API error: {exc}")
            finally:
                self.data_changed.emit()

    async def _handle_session_invalid(self, exc: BaseException) -> None:
        log.error("Telegram session is no longer valid (%s). Please sign in again.", type(exc).__name__)
        self._authorized = False
        if self.events is not None:
            self.events.unregister()
        await self.manager.disconnect()
        self.manager.delete_session()
        self._set_state(STATE_NEEDS_LOGIN, "Session expired — sign in again", account="")
        self.auth_event.emit("unauthorized", "Your Telegram session expired or was revoked. Sign in again.")
        await self._connect()

    async def _on_live_batch(self, group_id: int, batch: HistoryBatch) -> None:
        try:
            result = self.scanner.apply_batch(group_id, batch)
            self.avatars.enqueue(result.avatar_needed)
            if result.touched:
                self.notifier.flush_pending()
                self.data_changed.emit()
        except Exception:
            log.exception("Failed to store a live update")

    # ------------------------------------------------------------------ #
    # Public API (called from the UI thread)
    # ------------------------------------------------------------------ #

    def scan_now(self) -> None:
        if self._loop and self._wake:
            self._status["scanning_enabled"] = True
            self._loop.call_soon_threadsafe(self._wake.set)

    def set_scanning_enabled(self, enabled: bool) -> None:
        self._status["scanning_enabled"] = enabled
        log.info("Scanning %s", "resumed" if enabled else "paused")
        if self._loop and self._wake:
            self._loop.call_soon_threadsafe(self._wake.set)
        self.status_changed.emit(dict(self._status))

    def settings_changed(self) -> None:
        self.limiter.min_interval = self.settings.get().api_min_interval
        self._enabled_ids = self.groups_repo.enabled_ids()
        if self._loop and self._wake:
            self._loop.call_soon_threadsafe(self._wake.set)

    def groups_changed(self) -> None:
        self._enabled_ids = self.groups_repo.enabled_ids()

    def reconnect(self) -> None:
        """Rebuild the client (e.g. after API credentials changed in Settings)."""

        async def _reconnect() -> None:
            async with self._scan_lock:
                await self._connect()
            self.scan_now()

        self._submit(_reconnect(), "Connect")

    def request_code(self, phone: str) -> None:
        self._submit(self._auth_step(self.auth.request_code(phone), "code_sent"), "Request code")

    def submit_code(self, code: str) -> None:
        self._submit(self._auth_step(self.auth.submit_code(code), None), "Verify code")

    def submit_password(self, password: str) -> None:
        self._submit(self._auth_step(self.auth.submit_password(password), None), "Verify password")

    def login_bot(self, token: str) -> None:
        self._submit(self._auth_step(self.auth.login_bot(token), None), "Bot login")

    async def _auth_step(self, coro: Coroutine, success_event: str | None) -> None:
        if self.manager.client is None:
            await self._connect()
            if self.manager.client is None:
                coro.close()
                self.auth_event.emit("error", "Configure your API ID and API hash first.")
                return
        try:
            await coro
        except PasswordRequired:
            self.auth_event.emit("password_required", "Two-step verification is enabled")
            return
        except AuthError as exc:
            self.auth_event.emit("error", str(exc))
            return
        except (ConnectionError, OSError) as exc:
            self.auth_event.emit("error", f"Network error: {exc}")
            return
        if success_event:
            self.auth_event.emit(success_event, "")
            return
        if await self.manager.is_authorized():
            await self._on_authorized()
            self.scan_now()

    def logout(self) -> None:
        async def _logout() -> None:
            if self.events is not None:
                self.events.unregister()
            await self.auth.logout()
            self._authorized = False
            log.info("Signed out and removed the local session")
            self._set_state(STATE_NEEDS_LOGIN, "Signed out", account="", is_bot=False)
            self.auth_event.emit("logged_out", "")
            await self._connect()

        self._submit(_logout(), "Sign out")

    def discover_groups(self) -> None:
        self._submit(self._discover(), "Discover groups")

    async def _discover(self) -> None:
        if not self._authorized or self.manager.client is None:
            self.groups_updated.emit("Sign in first to discover your groups.")
            return
        if self.manager.is_bot:
            self.groups_updated.emit("Bots cannot list their chats. Add groups by @username or ID.")
            return
        try:
            infos = await discover_groups(self.manager.client, self.limiter)
        except FloodWaitTooLong as exc:
            self.groups_updated.emit(str(exc))
            return
        for info in infos:
            self.groups_repo.upsert(info.telegram_group_id, info.group_name, info.username, info.group_type,
                                    info.member_count, info.explicit_location)
        log.info("Discovered %d groups/channels this account belongs to", len(infos))
        self.groups_updated.emit(f"Found {len(infos)} groups and channels. Enable the ones to monitor.")
        self.data_changed.emit()

    def add_group(self, reference: str) -> None:
        async def _add() -> None:
            if not self._authorized or self.manager.client is None:
                self.groups_updated.emit("Sign in first.")
                return
            try:
                info = await resolve_group(self.manager.client, self.limiter, reference, self.manager.is_bot)
            except (GroupAccessError, FloodWaitTooLong) as exc:
                self.groups_updated.emit(str(exc))
                return
            self.groups_repo.upsert(info.telegram_group_id, info.group_name, info.username, info.group_type,
                                    info.member_count, info.explicit_location, enabled=True)
            self._enabled_ids = self.groups_repo.enabled_ids()
            log.info("Group added: %s (%s)", info.group_name, info.telegram_group_id)
            self.groups_updated.emit(f"Added {info.group_name}. It will be scanned in the next cycle.")
            self.data_changed.emit()
            self.scan_now()

        self._submit(_add(), "Add group")

    def fetch_public_profile(self, user_id: int) -> None:
        """On-demand read of a user's public bio and explicit Telegram Business location."""

        async def _fetch() -> None:
            client = self.manager.client
            if not self._authorized or client is None:
                self.profile_loaded.emit(user_id, "Sign in first.")
                return
            try:
                entity = await client.get_input_entity(user_id)
                full = await self.limiter.call(
                    lambda: client(functions.users.GetFullUserRequest(entity)), "reading a public profile",
                    max_flood_wait=60,
                )
            except (ValueError, errors.RPCError, FloodWaitTooLong) as exc:
                self.profile_loaded.emit(user_id, f"Public profile not available: {exc}")
                return
            full_user = full.full_user
            about = (full_user.about or "").strip() or None
            location = None
            biz = getattr(full_user, "business_location", None)
            if isinstance(biz, types.BusinessLocation) and biz.address:
                location = biz.address.strip()
            self.ingest.apply_public_profile(user_id, about, location)
            self.profile_loaded.emit(user_id, "Public profile updated")
            self.notifier.flush_pending()
            self.data_changed.emit()

        self._submit(_fetch(), "Load public profile")
