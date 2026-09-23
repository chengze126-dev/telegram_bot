"""Live update handlers (new messages, joins, leaves) for monitored groups only.

Handlers ignore private chats and every group that is not enabled in the app,
so no direct-message content is ever processed.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from telethon import TelegramClient, events, types

from app.database.db import now_ts, to_db_ts
from app.telegram.members import HistoryBatch, extract_message, user_to_record

log = logging.getLogger("app.telegram.events")

BatchCallback = Callable[[int, HistoryBatch], Awaitable[None]]


class LiveEventRouter:
    def __init__(self, client: TelegramClient, enabled_ids: Callable[[], set[int]],
                 on_batch: BatchCallback) -> None:
        self.client = client
        self.enabled_ids = enabled_ids
        self.on_batch = on_batch
        self._registered = False

    def register(self) -> None:
        if self._registered:
            return
        self.client.add_event_handler(self._on_message, events.NewMessage())
        self.client.add_event_handler(self._on_action, events.ChatAction())
        self._registered = True
        log.info("Live updates enabled for monitored groups")

    def unregister(self) -> None:
        if not self._registered:
            return
        self.client.remove_event_handler(self._on_message)
        self.client.remove_event_handler(self._on_action)
        self._registered = False

    def _accept(self, event) -> int | None:
        if event.is_private:
            return None
        chat_id = event.chat_id
        if chat_id is None or chat_id not in self.enabled_ids():
            return None
        return chat_id

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        chat_id = self._accept(event)
        if chat_id is None:
            return
        try:
            msg = event.message
            if msg.sender is None and msg.sender_id and msg.sender_id > 0:
                await event.get_sender()
            batch = HistoryBatch()
            extract_message(msg, chat_id, batch)
            if batch.messages:
                await self.on_batch(chat_id, batch)
        except Exception:
            log.exception("Failed to process a live message")

    async def _on_action(self, event: events.ChatAction.Event) -> None:
        chat_id = self._accept(event)
        if chat_id is None:
            return
        try:
            date = None
            if event.action_message is not None:
                date = event.action_message.date
            elif getattr(event.original_update, "date", None) is not None:
                date = event.original_update.date
            batch = HistoryBatch()
            if event.user_joined or event.user_added:
                users = await event.get_users()
                for user in users or []:
                    if isinstance(user, types.User):
                        if date is not None:
                            batch.joins.append((user_to_record(user), to_db_ts(date)))
                        else:
                            # No timestamp from Telegram: record as detected, join date unknown.
                            batch.present.append(user_to_record(user))
            elif event.user_left or event.user_kicked:
                ts = to_db_ts(date) if date is not None else now_ts()
                for uid in event.user_ids or []:
                    batch.leaves.append((uid, ts))
            if batch.joins or batch.leaves or batch.present:
                await self.on_batch(chat_id, batch)
        except Exception:
            log.exception("Failed to process a membership update")

