"""Lead notifications with (user_id, group_id) de-duplication."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from app.config import SettingsStore
from app.database.db import Database, from_db_ts
from app.database.models import JOINED_UNAVAILABLE, LeadCriteria, LeadRow
from app.database.repositories import LeadQueryRepository, NotificationRepository

log = logging.getLogger("app.notifications")

MAX_INDIVIDUAL_PER_BATCH = 3


@dataclass
class LeadNotification:
    title: str
    body: str
    user_id: int | None = None
    group_id: int | None = None


def format_joined(joined_at: str | None) -> str:
    dt = from_db_ts(joined_at)
    return f"Joined: {dt.date().isoformat()}" if dt else JOINED_UNAVAILABLE


def format_lead(lead: LeadRow) -> LeadNotification:
    name = lead.username or " ".join(p for p in (lead.first_name, lead.last_name) if p) or str(lead.user_id)
    body = "\n".join([
        name,
        f"Group: {lead.group_name}",
        f"Relevance: {lead.relevance_score}%",
        format_joined(lead.joined_at),
    ])
    return LeadNotification("New Telegram Lead Found", body, lead.user_id, lead.group_id)


class NotificationService:
    def __init__(self, db: Database, settings: SettingsStore,
                 emit: Callable[[LeadNotification], None]) -> None:
        self.settings = settings
        self.emit = emit
        self.leads = LeadQueryRepository(db)
        self.records = NotificationRepository(db)
        self.db = db

    def criteria(self) -> LeadCriteria:
        s = self.settings.get()
        return LeadCriteria(min_score=s.min_relevance, joined_before=s.joined_before_date)

    def flush_pending(self) -> int:
        """Notify about every matching (user, group) pair not notified before."""
        if not self.settings.get().notifications_enabled:
            return 0
        pending = self.leads.pending_notifications(self.criteria())
        fresh: list[LeadRow] = []
        with self.db.transaction():
            for lead in pending:
                if self.records.try_record(lead.user_id, lead.group_id):
                    fresh.append(lead)
        if not fresh:
            return 0
        for lead in fresh[:MAX_INDIVIDUAL_PER_BATCH]:
            self.emit(format_lead(lead))
        rest = len(fresh) - MAX_INDIVIDUAL_PER_BATCH
        if rest > 0:
            groups = sorted({lead.group_name for lead in fresh[MAX_INDIVIDUAL_PER_BATCH:]})
            shown = ", ".join(groups[:3]) + ("…" if len(groups) > 3 else "")
            self.emit(LeadNotification(
                "More Telegram Leads Found",
                f"{rest} more matching users\nGroups: {shown}",
            ))
        log.info("New matching users found: %d (notifications sent)", len(fresh))
        return len(fresh)
