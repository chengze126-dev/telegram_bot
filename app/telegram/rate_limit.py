"""Client-side pacing and FloodWait handling for Telegram API calls."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, TypeVar

from telethon import errors

log = logging.getLogger("app.telegram.ratelimit")

T = TypeVar("T")


class FloodWaitTooLong(Exception):
    """Raised when Telegram asks us to wait longer than we are willing to block."""

    def __init__(self, seconds: int, what: str) -> None:
        super().__init__(f"Telegram rate limit: must wait {seconds}s before {what}")
        self.seconds = seconds


class RateLimiter:
    """Ensures a minimum interval between API calls issued by this app."""

    def __init__(self, min_interval: float = 0.35) -> None:
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = asyncio.Lock()
        self.on_wait: Callable[[int, str], None] | None = None

    async def wait(self) -> None:
        async with self._lock:
            delay = self._last + self.min_interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()

    async def call(self, factory: Callable[[], Awaitable[T]], what: str,
                   max_flood_wait: int = 900, retries: int = 3) -> T:
        """Run ``factory()`` with pacing, sleeping through FloodWait errors.

        FloodWaits above ``max_flood_wait`` seconds are raised as
        :class:`FloodWaitTooLong` so the caller can skip the work this cycle
        instead of freezing the whole scanner.
        """
        attempt = 0
        while True:
            await self.wait()
            try:
                return await factory()
            except errors.FloodWaitError as exc:
                attempt += 1
                seconds = int(getattr(exc, "seconds", 0) or 0)
                if seconds > max_flood_wait or attempt > retries:
                    raise FloodWaitTooLong(seconds, what) from exc
                log.warning("Rate limit (FloodWait) while %s: waiting %ss", what, seconds)
                if self.on_wait:
                    self.on_wait(seconds, what)
                await asyncio.sleep(seconds + 1)
            except errors.SlowModeWaitError as exc:
                raise FloodWaitTooLong(int(exc.seconds), what) from exc
