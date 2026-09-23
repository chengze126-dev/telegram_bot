"""Downloads small public profile photos for visual identification only.

Avatars are cached on disk and re-downloaded only when Telegram reports a new
photo id. No image analysis of any kind is performed.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from pathlib import Path

from telethon import TelegramClient, errors

from app.database.db import Database
from app.database.repositories import UserRepository
from app.telegram.rate_limit import FloodWaitTooLong, RateLimiter

log = logging.getLogger("app.avatars")


class AvatarService:
    def __init__(self, db: Database, avatars_dir: Path, limiter: RateLimiter) -> None:
        self.users = UserRepository(db)
        self.avatars_dir = avatars_dir
        self.limiter = limiter
        self._queue: "OrderedDict[int, None]" = OrderedDict()

    def enqueue(self, user_ids) -> None:
        for uid in user_ids:
            self._queue[uid] = None

    @property
    def pending(self) -> int:
        return len(self._queue)

    async def process(self, client: TelegramClient, budget: int) -> int:
        downloaded = 0
        while self._queue and downloaded < budget:
            uid, _ = self._queue.popitem(last=False)
            row = self.users.get_row(uid)
            if row is None or not row["has_photo"] or row["photo_id"] is None:
                continue
            photo_id = row["photo_id"]
            if row["avatar_photo_id"] == photo_id and row["avatar_path"] and Path(row["avatar_path"]).exists():
                continue
            target = self.avatars_dir / f"{uid}_{photo_id}.jpg"
            try:
                result = await self.limiter.call(
                    lambda: client.download_profile_photo(uid, file=str(target), download_big=False),
                    "downloading a profile photo",
                    max_flood_wait=60,
                )
            except FloodWaitTooLong as exc:
                log.warning("Avatar downloads paused: %s", exc)
                self._queue[uid] = None
                break
            except (ValueError, errors.RPCError, OSError) as exc:
                log.debug("Avatar for %s not available: %s", uid, exc)
                continue
            if not result:
                continue
            old = row["avatar_path"]
            if old and old != str(result):
                try:
                    Path(old).unlink(missing_ok=True)
                except OSError:
                    pass
            self.users.set_avatar(uid, str(result), photo_id)
            downloaded += 1
        if downloaded:
            log.info("Downloaded %d profile photos (%d queued)", downloaded, len(self._queue))
        return downloaded
