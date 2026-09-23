"""Discovery and resolution of groups/channels the account already belongs to.

Only dialogs visible to the authenticated account are listed. Private
one-to-one chats are never enumerated or processed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from telethon import TelegramClient, errors, functions, types, utils

from app.telegram.rate_limit import RateLimiter

log = logging.getLogger("app.telegram.groups")

_LINK_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.(?:me|dog)/(?P<path>[^?#]+)", re.IGNORECASE)


class GroupAccessError(Exception):
    """The group cannot be used with this account (not joined, private, banned, ...)."""


@dataclass
class GroupInfo:
    telegram_group_id: int
    group_name: str
    username: str | None
    group_type: str  # supergroup | group | channel | gigagroup
    member_count: int | None
    explicit_location: str | None = None
    has_geo: bool = False


def group_type_of(entity) -> str | None:
    if isinstance(entity, types.Chat):
        return "group"
    if isinstance(entity, types.Channel):
        if getattr(entity, "gigagroup", False):
            return "gigagroup"
        if entity.megagroup:
            return "supergroup"
        return "channel"
    return None


def info_from_entity(entity) -> GroupInfo | None:
    kind = group_type_of(entity)
    if kind is None:
        return None
    if getattr(entity, "left", False) or getattr(entity, "deactivated", False):
        return None
    return GroupInfo(
        telegram_group_id=utils.get_peer_id(entity),
        group_name=getattr(entity, "title", None) or "Untitled group",
        username=getattr(entity, "username", None),
        group_type=kind,
        member_count=getattr(entity, "participants_count", None),
        has_geo=bool(getattr(entity, "has_geo", False)),
    )


async def discover_groups(client: TelegramClient, limiter: RateLimiter) -> list[GroupInfo]:
    """List groups/channels the authenticated account is a member of."""

    async def collect() -> list[GroupInfo]:
        found: list[GroupInfo] = []
        async for dialog in client.iter_dialogs(ignore_migrated=True):
            if not (dialog.is_group or dialog.is_channel):
                continue  # private chats are never touched
            info = info_from_entity(dialog.entity)
            if info is not None:
                found.append(info)
        return found

    groups = await limiter.call(collect, "listing your groups")
    for info in groups:
        info.explicit_location = await fetch_group_location(client, limiter, info)
    return groups


async def fetch_group_location(client: TelegramClient, limiter: RateLimiter, info: GroupInfo) -> str | None:
    """Return the explicit location of a location-based group, if Telegram exposes one."""
    if not info.has_geo or info.group_type not in ("supergroup", "gigagroup"):
        return None
    try:
        entity = await client.get_input_entity(info.telegram_group_id)
        full = await limiter.call(
            lambda: client(functions.channels.GetFullChannelRequest(entity)), f"reading {info.group_name}"
        )
    except (errors.RPCError, ValueError) as exc:
        log.debug("No metadata for %s: %s", info.group_name, exc)
        return None
    except Exception as exc:  # FloodWaitTooLong etc. – location is optional metadata
        log.debug("Skipped metadata for %s: %s", info.group_name, exc)
        return None
    location = getattr(full.full_chat, "location", None)
    if isinstance(location, types.ChannelLocation) and location.address:
        return location.address
    return None


def _parse_reference(reference: str) -> int | str:
    ref = reference.strip()
    if not ref:
        raise GroupAccessError("Enter a group @username, t.me link or numeric group ID.")
    if re.fullmatch(r"-?\d+", ref):
        return int(ref)
    match = _LINK_RE.match(ref)
    if match:
        path = match.group("path").strip("/")
        if path.startswith("+") or path.startswith("joinchat/"):
            raise GroupAccessError(
                "Invite links are not followed automatically. Join the group in Telegram first, "
                "then use 'Discover groups' or add it by @username / ID."
            )
        if path.startswith("c/"):
            parts = path.split("/")
            if len(parts) >= 2 and parts[1].isdigit():
                return int(f"-100{parts[1]}")
        return path.split("/")[0]
    return ref.lstrip("@")


async def resolve_group(client: TelegramClient, limiter: RateLimiter, reference: str,
                        is_bot: bool) -> GroupInfo:
    """Resolve a user-supplied group reference to a group the account can access."""
    target = _parse_reference(reference)
    try:
        entity = await limiter.call(lambda: client.get_entity(target), "resolving the group")
    except ValueError as exc:
        if isinstance(target, int) and not is_bot:
            # The entity may simply not be cached yet; refresh dialogs once.
            await limiter.call(lambda: client.get_dialogs(limit=None), "refreshing dialogs")
            try:
                entity = await client.get_entity(target)
            except ValueError:
                raise GroupAccessError("This account is not a member of that group.") from exc
        else:
            raise GroupAccessError("Group not found or not accessible to this account.") from exc
    except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError) as exc:
        raise GroupAccessError("No public group with that username exists.") from exc
    except (errors.ChannelPrivateError, errors.ChannelInvalidError, errors.ChatIdInvalidError,
            errors.PeerIdInvalidError) as exc:
        raise GroupAccessError("That group is private or not accessible to this account.") from exc

    if group_type_of(entity) is None:
        raise GroupAccessError("That reference is a user, not a group or channel.")
    if getattr(entity, "left", False) and not is_bot:
        raise GroupAccessError(
            "This account is not a member of that group. Join it in Telegram first so that "
            "monitoring stays within groups you are authorized to access."
        )
    info = info_from_entity(entity)
    if info is None:
        raise GroupAccessError("That group has been deactivated.")
    info.explicit_location = await fetch_group_location(client, limiter, info)
    return info


async def get_group_entity(client: TelegramClient, limiter: RateLimiter, telegram_group_id: int, is_bot: bool):
    """Return an input entity for a monitored group, refreshing the entity cache if needed."""
    try:
        return await client.get_input_entity(telegram_group_id)
    except ValueError:
        if is_bot:
            raise GroupAccessError("The bot cannot resolve this group. Make sure the bot was added to it.")
        await limiter.call(lambda: client.get_dialogs(limit=None), "refreshing dialogs")
        try:
            return await client.get_input_entity(telegram_group_id)
        except ValueError as exc:
            raise GroupAccessError("This account is no longer a member of the group.") from exc
