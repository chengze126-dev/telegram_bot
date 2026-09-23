"""Data access layer. All SQL lives here."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

from app.database.db import Database, now_ts, to_db_ts
from app.database.models import (
    DashboardStats,
    GroupStats,
    LeadCriteria,
    LeadFilter,
    LeadRow,
    RelevanceState,
    TelegramGroup,
    UserRecord,
)
from app.services.region import region_from_explicit_location

REGION_SQL = "COALESCE(u.manual_region, u.explicit_region, 'Unknown')"

MATCH_SQL = (
    "(COALESCE(r.relevance_score, 0) >= :min_score AND u.is_bot = 0 AND u.is_deleted = 0 "
    "AND m.left_at IS NULL AND (m.joined_at IS NULL OR m.joined_at < :joined_before))"
)

BUSINESS_SQL = (
    "(COALESCE(r.relevance_score, 0) >= :min_score AND u.is_bot = 0 AND u.is_deleted = 0 "
    "AND m.left_at IS NULL)"
)


def local_midnight_utc() -> str:
    """Start of the current local day, as a UTC DB timestamp."""
    local_midnight = datetime.combine(date.today(), time.min).astimezone()
    return to_db_ts(local_midnight)  # type: ignore[return-value]


def criteria_params(criteria: LeadCriteria) -> dict:
    return {
        "min_score": criteria.min_score,
        "joined_before": to_db_ts(datetime.combine(criteria.joined_before, time.min, tzinfo=timezone.utc)),
    }


def _row_to_group(row: sqlite3.Row) -> TelegramGroup:
    return TelegramGroup(
        id=row["id"],
        telegram_group_id=row["telegram_group_id"],
        group_name=row["group_name"],
        username=row["username"],
        group_type=row["group_type"],
        enabled=bool(row["enabled"]),
        explicit_location=row["explicit_location"],
        member_count=row["member_count"],
        status=row["status"],
        last_error=row["last_error"],
        last_scan_at=row["last_scan_at"],
        last_member_sync_at=row["last_member_sync_at"],
        last_message_id=row["last_message_id"],
    )


# --------------------------------------------------------------------------- #
# Groups
# --------------------------------------------------------------------------- #


class GroupRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, telegram_group_id: int, group_name: str, username: str | None, group_type: str,
               member_count: int | None, explicit_location: str | None = None,
               enabled: bool | None = None) -> None:
        ts = now_ts()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO telegram_groups (telegram_group_id, group_name, username, group_type, member_count,
                                             explicit_location, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_group_id) DO UPDATE SET
                    group_name = excluded.group_name,
                    username = excluded.username,
                    group_type = excluded.group_type,
                    member_count = COALESCE(excluded.member_count, telegram_groups.member_count),
                    explicit_location = COALESCE(excluded.explicit_location, telegram_groups.explicit_location),
                    enabled = CASE WHEN ? IS NULL THEN telegram_groups.enabled ELSE excluded.enabled END,
                    updated_at = excluded.updated_at
                """,
                (telegram_group_id, group_name, username, group_type, member_count, explicit_location,
                 int(bool(enabled)), ts, ts, enabled),
            )

    def get(self, telegram_group_id: int) -> TelegramGroup | None:
        row = self.db.conn().execute(
            "SELECT * FROM telegram_groups WHERE telegram_group_id = ?", (telegram_group_id,)
        ).fetchone()
        return _row_to_group(row) if row else None

    def list_all(self) -> list[TelegramGroup]:
        rows = self.db.conn().execute(
            "SELECT * FROM telegram_groups ORDER BY enabled DESC, group_name COLLATE NOCASE"
        ).fetchall()
        return [_row_to_group(r) for r in rows]

    def list_enabled(self) -> list[TelegramGroup]:
        rows = self.db.conn().execute(
            "SELECT * FROM telegram_groups WHERE enabled = 1 ORDER BY group_name COLLATE NOCASE"
        ).fetchall()
        return [_row_to_group(r) for r in rows]

    def enabled_ids(self) -> set[int]:
        rows = self.db.conn().execute("SELECT telegram_group_id FROM telegram_groups WHERE enabled = 1")
        return {r[0] for r in rows}

    def set_enabled(self, telegram_group_id: int, enabled: bool) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE telegram_groups SET enabled = ?, updated_at = ? WHERE telegram_group_id = ?",
                (int(enabled), now_ts(), telegram_group_id),
            )

    def set_status(self, telegram_group_id: int, status: str, error: str | None = None) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE telegram_groups SET status = ?, last_error = ?, updated_at = ? WHERE telegram_group_id = ?",
                (status, error, now_ts(), telegram_group_id),
            )

    def mark_scanned(self, telegram_group_id: int, last_message_id: int | None = None) -> None:
        ts = now_ts()
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE telegram_groups
                SET last_scan_at = ?, status = 'ok', last_error = NULL, updated_at = ?,
                    last_message_id = MAX(last_message_id, COALESCE(?, 0))
                WHERE telegram_group_id = ?
                """,
                (ts, ts, last_message_id, telegram_group_id),
            )

    def mark_member_sync(self, telegram_group_id: int, member_count: int | None) -> None:
        ts = now_ts()
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE telegram_groups
                SET last_member_sync_at = ?, member_count = COALESCE(?, member_count), updated_at = ?
                WHERE telegram_group_id = ?
                """,
                (ts, member_count, ts, telegram_group_id),
            )

    def delete(self, telegram_group_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM notifications WHERE telegram_group_id = ?", (telegram_group_id,))
            conn.execute("DELETE FROM processed_messages WHERE telegram_group_id = ?", (telegram_group_id,))
            conn.execute("DELETE FROM telegram_groups WHERE telegram_group_id = ?", (telegram_group_id,))
            conn.execute(
                "DELETE FROM telegram_users WHERE telegram_user_id NOT IN "
                "(SELECT DISTINCT telegram_user_id FROM group_memberships)"
            )


# --------------------------------------------------------------------------- #
# Users & memberships
# --------------------------------------------------------------------------- #


class UserRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, user: UserRecord, profile_topics: list[str]) -> bool:
        """Insert or update a user. Returns True if the avatar needs (re)downloading."""
        ts = now_ts()
        conn = self.db.conn()
        conn.execute(
            """
            INSERT INTO telegram_users (telegram_user_id, username, first_name, last_name, is_bot, is_deleted,
                                        has_photo, photo_id, profile_topics, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                is_bot = excluded.is_bot,
                is_deleted = excluded.is_deleted,
                has_photo = excluded.has_photo,
                photo_id = excluded.photo_id,
                profile_topics = excluded.profile_topics,
                updated_at = excluded.updated_at
            """,
            (user.telegram_user_id, user.username, user.first_name, user.last_name, int(user.is_bot),
             int(user.is_deleted), int(user.has_photo), user.photo_id, json.dumps(profile_topics), ts, ts),
        )
        row = conn.execute(
            "SELECT avatar_photo_id, avatar_path FROM telegram_users WHERE telegram_user_id = ?",
            (user.telegram_user_id,),
        ).fetchone()
        if not user.has_photo or user.photo_id is None:
            return False
        return row["avatar_path"] is None or row["avatar_photo_id"] != user.photo_id

    def get_profile_topics(self, telegram_user_id: int) -> list[str]:
        row = self.db.conn().execute(
            "SELECT profile_topics FROM telegram_users WHERE telegram_user_id = ?", (telegram_user_id,)
        ).fetchone()
        return json.loads(row[0]) if row and row[0] else []

    def get_avatar(self, telegram_user_id: int) -> tuple[str | None, int | None]:
        row = self.db.conn().execute(
            "SELECT avatar_path, avatar_photo_id FROM telegram_users WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def set_avatar(self, telegram_user_id: int, path: str | None, photo_id: int | None) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE telegram_users SET avatar_path = ?, avatar_photo_id = ?, updated_at = ? "
                "WHERE telegram_user_id = ?",
                (path, photo_id, now_ts(), telegram_user_id),
            )

    def set_manual_region(self, telegram_user_id: int, region: str | None) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE telegram_users SET manual_region = ?, updated_at = ? WHERE telegram_user_id = ?",
                (region, now_ts(), telegram_user_id),
            )

    def set_public_profile(self, telegram_user_id: int, about: str | None, explicit_location: str | None,
                           profile_topics: list[str]) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE telegram_users
                SET about = ?, explicit_location = ?, explicit_region = ?, profile_topics = ?,
                    profile_fetched_at = ?, updated_at = ?
                WHERE telegram_user_id = ?
                """,
                (about, explicit_location, region_from_explicit_location(explicit_location),
                 json.dumps(profile_topics), now_ts(), now_ts(), telegram_user_id),
            )

    def mark_deleted(self, telegram_user_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE telegram_users SET is_deleted = 1, updated_at = ? WHERE telegram_user_id = ?",
                (now_ts(), telegram_user_id),
            )

    def get_row(self, telegram_user_id: int) -> sqlite3.Row | None:
        return self.db.conn().execute(
            "SELECT * FROM telegram_users WHERE telegram_user_id = ?", (telegram_user_id,)
        ).fetchone()


class MembershipRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, telegram_user_id: int, telegram_group_id: int, seen_at: str,
               joined_at: str | None = None, joined_source: str | None = None) -> bool:
        """Record that a user was observed in a group. Returns True for a new membership.

        A real join date is only written when Telegram supplied one; an existing
        real date is never replaced by NULL.
        """
        conn = self.db.conn()
        cur = conn.execute(
            """
            INSERT INTO group_memberships (telegram_user_id, telegram_group_id, joined_at, joined_at_source,
                                           first_detected_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_user_id, telegram_group_id) DO NOTHING
            """,
            (telegram_user_id, telegram_group_id, joined_at, joined_source if joined_at else None, seen_at, seen_at),
        )
        if cur.rowcount:
            return True
        conn.execute(
            """
            UPDATE group_memberships
            SET last_seen_at = MAX(last_seen_at, ?),
                joined_at = COALESCE(?, joined_at),
                joined_at_source = CASE WHEN ? IS NOT NULL THEN ? ELSE joined_at_source END,
                left_at = CASE WHEN ? IS NOT NULL AND left_at IS NOT NULL AND ? > left_at THEN NULL ELSE left_at END
            WHERE telegram_user_id = ? AND telegram_group_id = ?
            """,
            (seen_at, joined_at, joined_at, joined_source, seen_at, seen_at, telegram_user_id, telegram_group_id),
        )
        return False

    def mark_left(self, telegram_user_id: int, telegram_group_id: int, at: str) -> None:
        self.db.conn().execute(
            "UPDATE group_memberships SET left_at = ? WHERE telegram_user_id = ? AND telegram_group_id = ?",
            (at, telegram_user_id, telegram_group_id),
        )

    def mark_missing_left(self, telegram_group_id: int, present_ids: Iterable[int], at: str) -> int:
        """After a *complete* member sync, mark memberships not present as left."""
        conn = self.db.conn()
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _present (uid INTEGER PRIMARY KEY)")
        conn.execute("DELETE FROM _present")
        conn.executemany("INSERT OR IGNORE INTO _present (uid) VALUES (?)", ((i,) for i in present_ids))
        cur = conn.execute(
            """
            UPDATE group_memberships SET left_at = ?
            WHERE telegram_group_id = ? AND left_at IS NULL
              AND telegram_user_id NOT IN (SELECT uid FROM _present)
            """,
            (at, telegram_group_id),
        )
        conn.execute("DELETE FROM _present")
        return cur.rowcount


class RelevanceRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, telegram_user_id: int, telegram_group_id: int) -> RelevanceState:
        row = self.db.conn().execute(
            "SELECT * FROM user_relevance WHERE telegram_user_id = ? AND telegram_group_id = ?",
            (telegram_user_id, telegram_group_id),
        ).fetchone()
        if row is None:
            return RelevanceState()
        return RelevanceState(
            topic_hits=json.loads(row["topic_hits"]),
            supporting_messages_count=row["supporting_messages_count"],
            evidence=json.loads(row["evidence"]),
            relevance_score=row["relevance_score"],
            detected_topics=json.loads(row["detected_topics"]),
        )

    def save(self, telegram_user_id: int, telegram_group_id: int, state: RelevanceState) -> None:
        self.db.conn().execute(
            """
            INSERT INTO user_relevance (telegram_user_id, telegram_group_id, relevance_score, detected_topics,
                                        topic_hits, supporting_messages_count, evidence, last_analyzed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_user_id, telegram_group_id) DO UPDATE SET
                relevance_score = excluded.relevance_score,
                detected_topics = excluded.detected_topics,
                topic_hits = excluded.topic_hits,
                supporting_messages_count = excluded.supporting_messages_count,
                evidence = excluded.evidence,
                last_analyzed_at = excluded.last_analyzed_at
            """,
            (telegram_user_id, telegram_group_id, state.relevance_score, json.dumps(state.detected_topics),
             json.dumps(state.topic_hits), state.supporting_messages_count, json.dumps(state.evidence), now_ts()),
        )

    def groups_for_user(self, telegram_user_id: int) -> list[int]:
        rows = self.db.conn().execute(
            "SELECT telegram_group_id FROM group_memberships WHERE telegram_user_id = ?", (telegram_user_id,)
        ).fetchall()
        return [r[0] for r in rows]


class ProcessedMessageRepository:
    """Remembers which (group, message) ids were analysed so none is counted twice."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def claim(self, telegram_group_id: int, message_id: int) -> bool:
        cur = self.db.conn().execute(
            "INSERT OR IGNORE INTO processed_messages (telegram_group_id, message_id, processed_at) VALUES (?, ?, ?)",
            (telegram_group_id, message_id, now_ts()),
        )
        return bool(cur.rowcount)

    def prune(self, keep_days: int = 30) -> int:
        cutoff = to_db_ts(datetime.now(timezone.utc) - timedelta(days=keep_days))
        with self.db.transaction() as conn:
            cur = conn.execute("DELETE FROM processed_messages WHERE processed_at < ?", (cutoff,))
            return cur.rowcount


class NotificationRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def try_record(self, telegram_user_id: int, telegram_group_id: int) -> bool:
        """Returns True only the first time a (user, group) pair is recorded."""
        cur = self.db.conn().execute(
            "INSERT OR IGNORE INTO notifications (telegram_user_id, telegram_group_id, notified_at) VALUES (?, ?, ?)",
            (telegram_user_id, telegram_group_id, now_ts()),
        )
        return bool(cur.rowcount)


# --------------------------------------------------------------------------- #
# Read models for the UI
# --------------------------------------------------------------------------- #

_LEAD_SELECT = f"""
    SELECT m.telegram_user_id AS user_id, u.username, u.first_name, u.last_name,
           m.telegram_group_id AS group_id, g.group_name,
           m.joined_at, m.joined_at_source, m.first_detected_at, m.last_seen_at, m.left_at,
           {REGION_SQL} AS region,
           COALESCE(r.relevance_score, 0) AS relevance_score,
           COALESCE(r.detected_topics, '[]') AS topics,
           COALESCE(r.supporting_messages_count, 0) AS supporting_messages_count,
           u.is_bot, u.is_deleted, u.has_photo, u.avatar_path,
           {MATCH_SQL} AS is_match,
           (m.first_detected_at >= :today) AS is_new_today
    FROM group_memberships m
    JOIN telegram_users u ON u.telegram_user_id = m.telegram_user_id
    JOIN telegram_groups g ON g.telegram_group_id = m.telegram_group_id
    LEFT JOIN user_relevance r ON r.telegram_user_id = m.telegram_user_id
                              AND r.telegram_group_id = m.telegram_group_id
"""


def _to_lead(row: sqlite3.Row) -> LeadRow:
    return LeadRow(
        user_id=row["user_id"],
        username=row["username"],
        first_name=row["first_name"],
        last_name=row["last_name"],
        group_id=row["group_id"],
        group_name=row["group_name"],
        joined_at=row["joined_at"],
        joined_at_source=row["joined_at_source"],
        first_detected_at=row["first_detected_at"],
        last_seen_at=row["last_seen_at"],
        left_at=row["left_at"],
        region=row["region"],
        relevance_score=row["relevance_score"],
        topics=json.loads(row["topics"]),
        supporting_messages_count=row["supporting_messages_count"],
        is_bot=bool(row["is_bot"]),
        is_deleted=bool(row["is_deleted"]),
        has_photo=bool(row["has_photo"]),
        avatar_path=row["avatar_path"],
        is_match=bool(row["is_match"]),
        is_new_today=bool(row["is_new_today"]),
    )


class LeadQueryRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def query(self, flt: LeadFilter, criteria: LeadCriteria) -> list[LeadRow]:
        params = criteria_params(criteria)
        params["today"] = local_midnight_utc()
        where: list[str] = []
        if flt.group_id is not None:
            where.append("m.telegram_group_id = :group_id")
            params["group_id"] = flt.group_id
        if not flt.include_bots:
            where.append("u.is_bot = 0")
        if flt.region and flt.region != "All":
            where.append(f"{REGION_SQL} = :region")
            params["region"] = flt.region
        if flt.min_score > 0:
            where.append("COALESCE(r.relevance_score, 0) >= :flt_min_score")
            params["flt_min_score"] = flt.min_score
        if flt.topic:
            where.append("r.detected_topics LIKE :topic")
            params["topic"] = f'%"{flt.topic}"%'
        if flt.joined_before is not None:
            # Only applies where a real joined date exists; unknown dates stay visible.
            where.append("(m.joined_at IS NULL OR m.joined_at < :flt_joined_before)")
            params["flt_joined_before"] = to_db_ts(datetime.combine(flt.joined_before, time.min, tzinfo=timezone.utc))
        if flt.joined_known == "known":
            where.append("m.joined_at IS NOT NULL")
        elif flt.joined_known == "unknown":
            where.append("m.joined_at IS NULL")
        if flt.new_today:
            where.append("m.first_detected_at >= :today")
        if flt.has_avatar:
            where.append("u.has_photo = 1")
        if flt.matches_only:
            where.append(MATCH_SQL)
        term = flt.search.strip()
        if term:
            params["like"] = f"%{term.lstrip('@')}%"
            numeric = term.lstrip("-").isdigit()
            if numeric:
                params["num"] = int(term)
            clauses = {
                "username": "u.username LIKE :like",
                "name": "(u.first_name LIKE :like OR u.last_name LIKE :like "
                        "OR (COALESCE(u.first_name,'') || ' ' || COALESCE(u.last_name,'')) LIKE :like)",
                "group": "(g.group_name LIKE :like OR CAST(g.telegram_group_id AS TEXT) LIKE :like)",
                "topic": "r.detected_topics LIKE :like",
                "user_id": "m.telegram_user_id = :num" if numeric else "0",
            }
            if flt.search_field in clauses:
                where.append(clauses[flt.search_field])
            else:
                parts = [clauses[k] for k in ("username", "name", "group", "topic")]
                if numeric:
                    parts.append("m.telegram_user_id = :num")
                where.append("(" + " OR ".join(parts) + ")")
        sql = _LEAD_SELECT
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY relevance_score DESC, m.first_detected_at DESC LIMIT :limit"
        params["limit"] = flt.limit
        return [_to_lead(r) for r in self.db.conn().execute(sql, params).fetchall()]

    def lead(self, telegram_user_id: int, telegram_group_id: int, criteria: LeadCriteria) -> LeadRow | None:
        params = criteria_params(criteria)
        params.update(today=local_midnight_utc(), uid=telegram_user_id, gid=telegram_group_id)
        row = self.db.conn().execute(
            _LEAD_SELECT + " WHERE m.telegram_user_id = :uid AND m.telegram_group_id = :gid", params
        ).fetchone()
        return _to_lead(row) if row else None

    def pending_notifications(self, criteria: LeadCriteria, limit: int = 200) -> list[LeadRow]:
        params = criteria_params(criteria)
        params.update(today=local_midnight_utc(), limit=limit)
        sql = (
            _LEAD_SELECT
            + " LEFT JOIN notifications n ON n.telegram_user_id = m.telegram_user_id"
              " AND n.telegram_group_id = m.telegram_group_id"
            + f" WHERE n.id IS NULL AND g.enabled = 1 AND {MATCH_SQL}"
            + " ORDER BY relevance_score DESC LIMIT :limit"
        )
        return [_to_lead(r) for r in self.db.conn().execute(sql, params).fetchall()]

    def dashboard_stats(self, criteria: LeadCriteria) -> DashboardStats:
        params = criteria_params(criteria)
        params["today"] = local_midnight_utc()
        conn = self.db.conn()
        g = conn.execute("SELECT COUNT(*), COALESCE(SUM(enabled), 0) FROM telegram_groups").fetchone()
        row = conn.execute(
            f"""
            SELECT COUNT(DISTINCT m.telegram_user_id),
                   COUNT(DISTINCT CASE WHEN {MATCH_SQL} THEN m.telegram_user_id END),
                   COUNT(DISTINCT CASE WHEN m.first_detected_at >= :today THEN m.telegram_user_id END),
                   COUNT(DISTINCT CASE WHEN {BUSINESS_SQL} THEN m.telegram_user_id END)
            FROM group_memberships m
            JOIN telegram_users u ON u.telegram_user_id = m.telegram_user_id
            LEFT JOIN user_relevance r ON r.telegram_user_id = m.telegram_user_id
                                      AND r.telegram_group_id = m.telegram_group_id
            WHERE u.is_bot = 0
            """,
            params,
        ).fetchone()
        return DashboardStats(
            total_groups=g[0], enabled_groups=g[1], total_users=row[0],
            matching_users=row[1], new_today=row[2], business_matches=row[3],
        )

    def group_stats(self, criteria: LeadCriteria) -> list[GroupStats]:
        params = criteria_params(criteria)
        params["today"] = local_midnight_utc()
        rows = self.db.conn().execute(
            f"""
            SELECT g.*, COALESCE(s.users, 0) AS users, COALESCE(s.matches, 0) AS matches,
                   COALESCE(s.new_today, 0) AS new_today
            FROM telegram_groups g
            LEFT JOIN (
                SELECT m.telegram_group_id AS gid,
                       COUNT(*) AS users,
                       SUM(CASE WHEN {MATCH_SQL} THEN 1 ELSE 0 END) AS matches,
                       SUM(CASE WHEN m.first_detected_at >= :today THEN 1 ELSE 0 END) AS new_today
                FROM group_memberships m
                JOIN telegram_users u ON u.telegram_user_id = m.telegram_user_id
                LEFT JOIN user_relevance r ON r.telegram_user_id = m.telegram_user_id
                                          AND r.telegram_group_id = m.telegram_group_id
                WHERE u.is_bot = 0
                GROUP BY m.telegram_group_id
            ) s ON s.gid = g.telegram_group_id
            ORDER BY g.enabled DESC, g.group_name COLLATE NOCASE
            """,
            params,
        ).fetchall()
        return [
            GroupStats(
                telegram_group_id=r["telegram_group_id"], group_name=r["group_name"], group_type=r["group_type"],
                enabled=bool(r["enabled"]), status=r["status"], last_error=r["last_error"],
                last_scan_at=r["last_scan_at"], member_count=r["member_count"],
                explicit_location=r["explicit_location"], users=r["users"], matches=r["matches"],
                new_today=r["new_today"],
            )
            for r in rows
        ]

    def detections_per_day(self, days: int = 14) -> list[tuple[date, int]]:
        start = date.today() - timedelta(days=days - 1)
        start_utc = to_db_ts(datetime.combine(start, time.min).astimezone())
        rows = self.db.conn().execute(
            """
            SELECT date(m.first_detected_at, 'localtime') AS d, COUNT(*)
            FROM group_memberships m JOIN telegram_users u ON u.telegram_user_id = m.telegram_user_id
            WHERE m.first_detected_at >= ? AND u.is_bot = 0
            GROUP BY d
            """,
            (start_utc,),
        ).fetchall()
        counts = {r[0]: r[1] for r in rows}
        return [(start + timedelta(days=i), counts.get((start + timedelta(days=i)).isoformat(), 0))
                for i in range(days)]

    def recent_matches(self, criteria: LeadCriteria, limit: int = 8) -> list[LeadRow]:
        params = criteria_params(criteria)
        params.update(today=local_midnight_utc(), limit=limit)
        sql = _LEAD_SELECT + f" WHERE {MATCH_SQL} ORDER BY m.first_detected_at DESC, relevance_score DESC LIMIT :limit"
        return [_to_lead(r) for r in self.db.conn().execute(sql, params).fetchall()]

    def user_memberships(self, telegram_user_id: int, criteria: LeadCriteria) -> list[tuple[LeadRow, list[dict]]]:
        params = criteria_params(criteria)
        params.update(today=local_midnight_utc(), uid=telegram_user_id)
        sql = _LEAD_SELECT.replace(
            "AS is_new_today", "AS is_new_today, COALESCE(r.evidence, '[]') AS evidence"
        ) + " WHERE m.telegram_user_id = :uid ORDER BY relevance_score DESC"
        rows = self.db.conn().execute(sql, params).fetchall()
        return [(_to_lead(r), json.loads(r["evidence"])) for r in rows]

    def topics_in_use(self) -> list[str]:
        rows = self.db.conn().execute("SELECT DISTINCT detected_topics FROM user_relevance").fetchall()
        topics: set[str] = set()
        for r in rows:
            topics.update(json.loads(r[0]))
        return sorted(topics)
