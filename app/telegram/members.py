"""Converting Telegram participants and visible group messages into records.

Only fields that Telegram exposes to the authenticated account are read.
Phone numbers are deliberately never read, even when the API returns them
(e.g. for contacts). Join dates are only taken from Telegram's own
membership records or join service messages — never estimated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from telethon import TelegramClient, types

from app.database.db import to_db_ts
from app.database.models import JOINED_SOURCE_PARTICIPANT, MessageRecord, UserRecord
from app.telegram.rate_limit import RateLimiter

log = logging.getLogger("app.telegram.members")

# Participant types whose ``date`` is the date the user joined / was added.
# (For channel admins/creators the date means "promoted", so it is NOT used.)
_JOIN_DATE_PARTICIPANTS = (
    types.ChannelParticipant,
    types.ChannelParticipantSelf,
    types.ChatParticipant,
    types.ChatParticipantAdmin,
)


def user_to_record(user: types.User) -> UserRecord:
    photo = getattr(user, "photo", None)
    has_photo = isinstance(photo, types.UserProfilePhoto)
    return UserRecord(
        telegram_user_id=user.id,
        username=getattr(user, "username", None) or _first_active_username(user),
        first_name=getattr(user, "first_name", None),
        last_name=getattr(user, "last_name", None),
        is_bot=bool(getattr(user, "bot", False)),
        is_deleted=bool(getattr(user, "deleted", False)),
        has_photo=has_photo,
        photo_id=photo.photo_id if has_photo else None,
    )


def _first_active_username(user) -> str | None:
    for item in getattr(user, "usernames", None) or []:
        if getattr(item, "active", False):
            return item.username
    return None


def participant_joined_at(participant) -> str | None:
    if isinstance(participant, _JOIN_DATE_PARTICIPANTS):
        date = getattr(participant, "date", None)
        if date is not None:
            return to_db_ts(date)
    return None


@dataclass
class MemberSnapshot:
    members: list[tuple[UserRecord, str | None, str | None]]
    total: int | None

    @property
    def complete(self) -> bool:
        return self.total is not None and len(self.members) >= self.total


async def fetch_members(client: TelegramClient, limiter: RateLimiter, entity, limit: int,
                        group_name: str) -> MemberSnapshot:
    """Fetch the member list Telegram makes visible to this account (paged by Telethon)."""

    async def collect() -> MemberSnapshot:
        members: list[tuple[UserRecord, str | None, str | None]] = []
        iterator = client.iter_participants(entity, limit=limit)
        async for user in iterator:
            if not isinstance(user, types.User):
                continue
            joined = participant_joined_at(getattr(user, "participant", None))
            members.append((user_to_record(user), joined, JOINED_SOURCE_PARTICIPANT if joined else None))
        return MemberSnapshot(members=members, total=getattr(iterator, "total", None))

    return await limiter.call(collect, f"fetching members of {group_name}")


@dataclass
class HistoryBatch:
    messages: list[MessageRecord] = field(default_factory=list)
    joins: list[tuple[UserRecord, str]] = field(default_factory=list)
    leaves: list[tuple[int, str]] = field(default_factory=list)
    present: list[UserRecord] = field(default_factory=list)  # seen without a join timestamp
    max_id: int = 0
    hit_limit: bool = False


def extract_message(msg, group_id: int, batch: HistoryBatch) -> None:
    """Fold one Telethon message into ``batch``."""
    batch.max_id = max(batch.max_id, msg.id)
    sender = getattr(msg, "sender", None)
    action = getattr(msg, "action", None)
    if action is not None:
        ts = to_db_ts(msg.date)
        if isinstance(action, types.MessageActionChatAddUser):
            for added in getattr(msg, "action_entities", None) or []:
                if isinstance(added, types.User):
                    batch.joins.append((user_to_record(added), ts))
        elif isinstance(action, (types.MessageActionChatJoinedByLink, types.MessageActionChatJoinedByRequest)):
            if isinstance(sender, types.User):
                batch.joins.append((user_to_record(sender), ts))
        elif isinstance(action, types.MessageActionChatDeleteUser):
            batch.leaves.append((action.user_id, ts))
        return
    if not isinstance(sender, types.User):
        return  # anonymous admins / linked channel posts have no user
    batch.messages.append(
        MessageRecord(
            telegram_group_id=group_id,
            message_id=msg.id,
            sender=user_to_record(sender),
            date=msg.date,
            text=getattr(msg, "message", None) or "",
        )
    )


async def fetch_history(client: TelegramClient, limiter: RateLimiter, entity, group_id: int, group_name: str,
                        last_message_id: int, backfill: int, max_messages: int) -> HistoryBatch:
    """Fetch only messages newer than ``last_message_id`` (incremental scan)."""
    first_scan = last_message_id <= 0
    limit = backfill if first_scan else max_messages
    if limit <= 0:
        # No backfill requested: just learn the latest message id so future scans are incremental.
        limit = 1

    async def collect() -> HistoryBatch:
        batch = HistoryBatch()
        count = 0
        kwargs = {"limit": limit}
        if not first_scan:
            kwargs["min_id"] = last_message_id
        async for msg in client.iter_messages(entity, **kwargs):
            count += 1
            if first_scan and backfill <= 0:
                batch.max_id = max(batch.max_id, msg.id)
                continue
            extract_message(msg, group_id, batch)
        batch.hit_limit = count >= limit and not first_scan
        # Process oldest first so evidence and joins are applied chronologically.
        batch.messages.reverse()
        batch.joins.reverse()
        batch.leaves.reverse()
        return batch

    return await limiter.call(collect, f"reading new messages in {group_name}")
