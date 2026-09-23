"""CSV export of the currently filtered results."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

from app.database.db import from_db_ts
from app.database.models import JOINED_UNAVAILABLE, LeadRow

CSV_COLUMNS = [
    "User ID", "Username", "First Name", "Last Name", "Group ID", "Group Name", "Joined Date",
    "First Detected Date", "Last Seen", "Region", "Relevance Score", "Topics",
]


def _fmt(ts: str | None) -> str:
    dt = from_db_ts(ts)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M") if dt else ""


def _safe(value: str | None) -> str:
    """Neutralise spreadsheet formula injection for text cells."""
    text = value or ""
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def export_csv(rows: Iterable[LeadRow], path: Path | str) -> int:
    count = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_COLUMNS)
        for row in rows:
            joined = from_db_ts(row.joined_at)
            writer.writerow([
                row.user_id,
                _safe(row.username),
                _safe(row.first_name),
                _safe(row.last_name),
                row.group_id,
                _safe(row.group_name),
                joined.date().isoformat() if joined else JOINED_UNAVAILABLE,
                _fmt(row.first_detected_at),
                _fmt(row.last_seen_at),
                row.region,
                row.relevance_score,
                "; ".join(row.topics),
            ])
            count += 1
    return count
