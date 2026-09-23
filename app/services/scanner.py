"""Periodic, incremental scanning of the enabled groups.

Every cycle (default 60s) each enabled group gets:
  * an incremental history read (only messages newer than the last seen id),
  * a member-list sync only when it is due (default every 30 minutes),
so the full member list is not downloaded every minute. Live updates between
cycles are handled by :mod:`app.telegram.events`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from telethon import TelegramClient, errors

from app.config import SettingsStore
from app.database.db import Database, from_db_ts, utcnow
from app.database.models import TelegramGroup
from app.database.repositories import GroupRepository, ProcessedMessageRepository
from app.services.avatar_service import AvatarService
from app.services.ingest import IngestResult, IngestService
from app.services.notifications import NotificationService
from app.telegram.client import SESSION_INVALID_ERRORS
from app.telegram.groups import GroupAccessError, get_group_entity
from app.telegram.members import HistoryBatch, fetch_history, fetch_members
from app.telegram.rate_limit import FloodWaitTooLong, RateLimiter

log = logging.getLogger("app.scanner")

# Errors meaning the account can no longer read the group.
INACCESSIBLE_ERRORS: tuple[type[BaseException], ...] = (
    errors.ChannelPrivateError,
    errors.ChannelInvalidError,
    errors.ChatForbiddenError,
    errors.ChatIdInvalidError,
    errors.PeerIdInvalidError,
    errors.UserBannedInChannelError,
)


@dataclass
class CycleSummary:
    groups_scanned: int = 0
    groups_failed: int = 0
    users_processed: int = 0
    new_users: int = 0
    messages_analyzed: int = 0
    new_matches: int = 0
    duration: float = 0.0


class ScanService:
    def __init__(self, db: Database, settings: SettingsStore, limiter: RateLimiter,
                 ingest: IngestService, notifier: NotificationService, avatars: AvatarService,
                 on_progress: Callable[[str], None] | None = None) -> None:
        self.db = db
        self.settings = settings
        self.limiter = limiter
        self.ingest = ingest
        self.notifier = notifier
        self.avatars = avatars
        self.groups = GroupRepository(db)
        self.processed = ProcessedMessageRepository(db)
        self.on_progress = on_progress or (lambda _msg: None)
        self._cycles = 0

    # ------------------------------------------------------------------ #

    async def run_cycle(self, client: TelegramClient, is_bot: bool) -> CycleSummary:
        started = time.monotonic()
        summary = CycleSummary()
        groups = self.groups.list_enabled()
        if not groups:
            log.info("No groups enabled for monitoring. Enable groups on the Groups page.")
            return summary
        log.info("Scanning started: %d group(s)", len(groups))
        total = IngestResult()
        for index, group in enumerate(groups, start=1):
            self.on_progress(f"Scanning {group.group_name} ({index}/{len(groups)})")
            try:
                result = await self.scan_group(client, group, is_bot)
            except SESSION_INVALID_ERRORS:
                raise
            except (ConnectionError, OSError):
                raise
            except Exception as exc:  # one broken group must never stop the others
                summary.groups_failed += 1
                log.exception("Unexpected error while scanning %s: %s", group.group_name, exc)
                self.groups.set_status(group.telegram_group_id, "error", str(exc)[:300])
                continue
            if result is None:
                summary.groups_failed += 1
                continue
            summary.groups_scanned += 1
            total.merge(result)
        self.avatars.enqueue(total.avatar_needed)
        s = self.settings.get()
        if s.avatar_downloads_per_cycle:
            self.on_progress("Updating profile photos")
            try:
                await self.avatars.process(client, s.avatar_downloads_per_cycle)
            except (ConnectionError, OSError, *SESSION_INVALID_ERRORS):
                raise
            except Exception as exc:  # avatars are cosmetic; never fail the cycle over them
                log.warning("Profile photo update skipped: %s", exc)
        summary.new_matches = self.notifier.flush_pending()
        summary.users_processed = total.users_processed
        summary.new_users = total.new_memberships
        summary.messages_analyzed = total.messages_analyzed
        summary.duration = time.monotonic() - started
        self._cycles += 1
        if self._cycles % 60 == 0:
            self.processed.prune()
        log.info(
            "Scan finished in %.1fs: %d group(s) scanned, %d failed, %d users processed, %d new users, "
            "%d messages analysed, %d new matches",
            summary.duration, summary.groups_scanned, summary.groups_failed, summary.users_processed,
            summary.new_users, summary.messages_analyzed, summary.new_matches,
        )
        return summary

    def _member_sync_due(self, group: TelegramGroup) -> bool:
        last = from_db_ts(group.last_member_sync_at)
        if last is None:
            return True
        return utcnow() - last >= timedelta(minutes=self.settings.get().member_sync_minutes)

    async def scan_group(self, client: TelegramClient, group: TelegramGroup, is_bot: bool) -> IngestResult | None:
        gid = group.telegram_group_id
        s = self.settings.get()
        result = IngestResult()
        try:
            entity = await get_group_entity(client, self.limiter, gid, is_bot)
        except GroupAccessError as exc:
            self._mark_inaccessible(group, str(exc))
            return None

        # 1) Member list — only when due, never every minute.
        if group.group_type != "channel" and self._member_sync_due(group):
            try:
                snapshot = await fetch_members(client, self.limiter, entity, s.max_members_per_sync, group.group_name)
            except errors.ChatAdminRequiredError:
                log.info("%s: member list is restricted to admins; relying on visible messages", group.group_name)
                self.groups.mark_member_sync(gid, None)
            except errors.BotMethodInvalidError:
                log.info("%s: bots cannot list members; relying on live updates", group.group_name)
                self.groups.mark_member_sync(gid, None)
            except FloodWaitTooLong as exc:
                log.warning("%s: %s — member sync postponed", group.group_name, exc)
            except INACCESSIBLE_ERRORS as exc:
                self._mark_inaccessible(group, _describe(exc))
                return None
            else:
                members_result = self.ingest.ingest_members(gid, snapshot.members)
                result.merge(members_result)
                if snapshot.complete:
                    left = self.ingest.mark_missing_left(gid, {u.telegram_user_id for u, _, _ in snapshot.members})
                    if left:
                        log.info("%s: %d member(s) no longer in the group", group.group_name, left)
                self.groups.mark_member_sync(gid, snapshot.total)
                log.info(
                    "%s: member sync processed %d users (%d new)%s",
                    group.group_name, len(snapshot.members), members_result.new_memberships,
                    "" if snapshot.complete else " — Telegram returned a partial list",
                )

        # 2) Incremental history — only messages newer than the stored id.
        last_id = group.last_message_id
        try:
            batch = await fetch_history(
                client, self.limiter, entity, gid, group.group_name, last_id,
                s.history_backfill, s.max_messages_per_scan,
            )
        except errors.BotMethodInvalidError:
            batch = None  # bots cannot read history; live updates cover them
        except (errors.ChatAdminRequiredError, *INACCESSIBLE_ERRORS) as exc:
            self._mark_inaccessible(group, _describe(exc))
            return None
        except FloodWaitTooLong as exc:
            log.warning("%s: %s — history scan postponed", group.group_name, exc)
            batch = None
        if batch is not None:
            result.merge(self.apply_batch(gid, batch))
            if batch.hit_limit:
                log.warning("%s: more than %d new messages since last scan; oldest ones skipped",
                            group.group_name, s.max_messages_per_scan)
        self.groups.mark_scanned(gid, batch.max_id if batch else None)
        log.info(
            "Group scanned: %s (%s) — %d users processed, %d new, %d messages analysed",
            group.group_name, gid, result.users_processed, result.new_memberships, result.messages_analyzed,
        )
        return result

    def apply_batch(self, group_id: int, batch: HistoryBatch) -> IngestResult:
        result = IngestResult()
        for user, ts in batch.joins:
            result.merge(self.ingest.ingest_join(group_id, user, ts))
        if batch.present:
            result.merge(self.ingest.ingest_members(group_id, [(u, None, None) for u in batch.present]))
        if batch.messages:
            result.merge(self.ingest.ingest_messages(batch.messages))
        for uid, ts in batch.leaves:
            self.ingest.ingest_leave(group_id, uid, ts)
        return result

    def _mark_inaccessible(self, group: TelegramGroup, reason: str) -> None:
        log.error("Group inaccessible: %s (%s) — %s", group.group_name, group.telegram_group_id, reason)
        self.groups.set_status(group.telegram_group_id, "inaccessible", reason)


def _describe(exc: BaseException) -> str:
    if isinstance(exc, errors.ChatAdminRequiredError):
        return "Admin rights are required to read this chat (broadcast channels only expose members to admins)."
    if isinstance(exc, errors.ChannelPrivateError):
        return "The group is private or this account was removed from it."
    if isinstance(exc, errors.UserBannedInChannelError):
        return "This account is banned from the group."
    return f"{type(exc).__name__}: {exc}"
