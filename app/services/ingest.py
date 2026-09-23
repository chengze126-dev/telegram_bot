"""Synchronous ingestion of Telegram observations into SQLite.

This layer is independent of Telethon so that it can be unit-tested. The
scanner converts API objects into :mod:`app.database.models` records and
hands them here. Every write is keyed by (telegram_user_id, telegram_group_id)
so users from different groups are never mixed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.database.db import Database, now_ts
from app.database.models import JOINED_SOURCE_EVENT, MessageRecord, UserRecord
from app.database.repositories import (
    MembershipRepository,
    ProcessedMessageRepository,
    RelevanceRepository,
    UserRepository,
)
from app.services.relevance import RelevanceClassifier

log = logging.getLogger("app.ingest")


@dataclass
class IngestResult:
    new_memberships: int = 0
    users_processed: int = 0
    messages_analyzed: int = 0
    relevant_messages: int = 0
    avatar_needed: set[int] = field(default_factory=set)
    touched: set[tuple[int, int]] = field(default_factory=set)

    def merge(self, other: "IngestResult") -> None:
        self.new_memberships += other.new_memberships
        self.users_processed += other.users_processed
        self.messages_analyzed += other.messages_analyzed
        self.relevant_messages += other.relevant_messages
        self.avatar_needed |= other.avatar_needed
        self.touched |= other.touched


class IngestService:
    def __init__(self, db: Database, classifier: RelevanceClassifier | None = None) -> None:
        self.db = db
        self.classifier = classifier or RelevanceClassifier()
        self.users = UserRepository(db)
        self.memberships = MembershipRepository(db)
        self.relevance = RelevanceRepository(db)
        self.processed = ProcessedMessageRepository(db)

    # -- users ------------------------------------------------------------ #

    def _profile_topics(self, user: UserRecord) -> list[str]:
        existing = self.users.get_row(user.telegram_user_id)
        about = existing["about"] if existing is not None else None
        return self.classifier.profile_topics(user.username, user.first_name, user.last_name, about)

    def _observe(self, user: UserRecord, group_id: int, seen_at: str, result: IngestResult,
                 joined_at: str | None = None, joined_source: str | None = None) -> list[str]:
        profile_topics = self._profile_topics(user)
        if self.users.upsert(user, profile_topics):
            result.avatar_needed.add(user.telegram_user_id)
        if self.memberships.upsert(user.telegram_user_id, group_id, seen_at, joined_at, joined_source):
            result.new_memberships += 1
            # Seed a relevance row so profile-only signals are visible immediately.
            state = self.relevance.get(user.telegram_user_id, group_id)
            self.classifier.recompute(state, profile_topics)
            self.relevance.save(user.telegram_user_id, group_id, state)
        result.users_processed += 1
        result.touched.add((user.telegram_user_id, group_id))
        return profile_topics

    def ingest_members(self, group_id: int, members: list[tuple[UserRecord, str | None, str | None]]) -> IngestResult:
        """Members from a participant list: (user, joined_at or None, joined_source or None)."""
        result = IngestResult()
        seen_at = now_ts()
        with self.db.transaction():
            for user, joined_at, source in members:
                self._observe(user, group_id, seen_at, result, joined_at, source)
        return result

    def ingest_join(self, group_id: int, user: UserRecord, joined_at: str) -> IngestResult:
        """A join/add event carrying a real Telegram timestamp."""
        result = IngestResult()
        with self.db.transaction():
            self._observe(user, group_id, now_ts(), result, joined_at, JOINED_SOURCE_EVENT)
        return result

    def ingest_leave(self, group_id: int, telegram_user_id: int, at: str) -> None:
        with self.db.transaction():
            self.memberships.mark_left(telegram_user_id, group_id, at)

    def mark_missing_left(self, group_id: int, present_ids: set[int]) -> int:
        with self.db.transaction():
            return self.memberships.mark_missing_left(group_id, present_ids, now_ts())

    # -- messages --------------------------------------------------------- #

    def ingest_messages(self, messages: list[MessageRecord]) -> IngestResult:
        result = IngestResult()
        seen_at = now_ts()
        with self.db.transaction():
            for msg in messages:
                if msg.sender.is_deleted:
                    continue
                if not self.processed.claim(msg.telegram_group_id, msg.message_id):
                    continue
                sender = msg.sender
                profile_topics = self._observe(sender, msg.telegram_group_id, seen_at, result)
                if sender.is_bot or not msg.text:
                    continue
                state = self.relevance.get(sender.telegram_user_id, msg.telegram_group_id)
                if self.classifier.apply_message(state, msg.text, msg.date, msg.message_id, profile_topics):
                    result.relevant_messages += 1
                self.relevance.save(sender.telegram_user_id, msg.telegram_group_id, state)
                result.messages_analyzed += 1
        return result

    # -- profile ------------------------------------------------------------ #

    def apply_public_profile(self, telegram_user_id: int, about: str | None, explicit_location: str | None) -> None:
        """Store public bio/location fetched on demand and re-score all of the user's groups."""
        row = self.users.get_row(telegram_user_id)
        if row is None:
            return
        topics = self.classifier.profile_topics(row["username"], row["first_name"], row["last_name"], about)
        self.users.set_public_profile(telegram_user_id, about, explicit_location, topics)
        with self.db.transaction():
            for group_id in self.relevance.groups_for_user(telegram_user_id):
                state = self.relevance.get(telegram_user_id, group_id)
                self.classifier.recompute(state, topics)
                self.relevance.save(telegram_user_id, group_id, state)
