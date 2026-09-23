"""Versioned SQLite schema migrations (tracked with ``PRAGMA user_version``)."""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger("app.database")

MIGRATIONS: dict[int, list[str]] = {
    1: [
        """
        CREATE TABLE IF NOT EXISTS telegram_groups (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_group_id     INTEGER NOT NULL UNIQUE,
            group_name            TEXT    NOT NULL,
            username              TEXT,
            group_type            TEXT    NOT NULL DEFAULT 'supergroup',
            enabled               INTEGER NOT NULL DEFAULT 0,
            explicit_location     TEXT,
            member_count          INTEGER,
            status                TEXT    NOT NULL DEFAULT 'pending',
            last_error            TEXT,
            last_scan_at          TEXT,
            last_member_sync_at   TEXT,
            last_message_id       INTEGER NOT NULL DEFAULT 0,
            created_at            TEXT    NOT NULL,
            updated_at            TEXT    NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS telegram_users (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_user_id   INTEGER NOT NULL UNIQUE,
            username           TEXT,
            first_name         TEXT,
            last_name          TEXT,
            is_bot             INTEGER NOT NULL DEFAULT 0,
            is_deleted         INTEGER NOT NULL DEFAULT 0,
            has_photo          INTEGER NOT NULL DEFAULT 0,
            photo_id           INTEGER,
            avatar_photo_id    INTEGER,
            avatar_path        TEXT,
            about              TEXT,
            profile_topics     TEXT    NOT NULL DEFAULT '[]',
            profile_fetched_at TEXT,
            explicit_location  TEXT,
            explicit_region    TEXT,
            manual_region      TEXT,
            created_at         TEXT    NOT NULL,
            updated_at         TEXT    NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS group_memberships (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_user_id   INTEGER NOT NULL REFERENCES telegram_users(telegram_user_id) ON DELETE CASCADE,
            telegram_group_id  INTEGER NOT NULL REFERENCES telegram_groups(telegram_group_id) ON DELETE CASCADE,
            joined_at          TEXT,
            joined_at_source   TEXT,
            first_detected_at  TEXT    NOT NULL,
            last_seen_at       TEXT    NOT NULL,
            left_at            TEXT,
            UNIQUE (telegram_user_id, telegram_group_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS user_relevance (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_user_id            INTEGER NOT NULL,
            telegram_group_id           INTEGER NOT NULL,
            relevance_score             INTEGER NOT NULL DEFAULT 0,
            detected_topics             TEXT    NOT NULL DEFAULT '[]',
            topic_hits                  TEXT    NOT NULL DEFAULT '{}',
            supporting_messages_count   INTEGER NOT NULL DEFAULT 0,
            evidence                    TEXT    NOT NULL DEFAULT '[]',
            last_analyzed_at            TEXT    NOT NULL,
            UNIQUE (telegram_user_id, telegram_group_id),
            FOREIGN KEY (telegram_user_id, telegram_group_id)
                REFERENCES group_memberships(telegram_user_id, telegram_group_id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_user_id   INTEGER NOT NULL,
            telegram_group_id  INTEGER NOT NULL,
            notified_at        TEXT    NOT NULL,
            UNIQUE (telegram_user_id, telegram_group_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS processed_messages (
            telegram_group_id  INTEGER NOT NULL,
            message_id         INTEGER NOT NULL,
            processed_at       TEXT    NOT NULL,
            PRIMARY KEY (telegram_group_id, message_id)
        ) WITHOUT ROWID
        """,
        "CREATE INDEX IF NOT EXISTS ix_groups_enabled ON telegram_groups(enabled)",
        "CREATE INDEX IF NOT EXISTS ix_users_username ON telegram_users(username COLLATE NOCASE)",
        "CREATE INDEX IF NOT EXISTS ix_members_group ON group_memberships(telegram_group_id)",
        "CREATE INDEX IF NOT EXISTS ix_members_user ON group_memberships(telegram_user_id)",
        "CREATE INDEX IF NOT EXISTS ix_members_first_detected ON group_memberships(first_detected_at)",
        "CREATE INDEX IF NOT EXISTS ix_members_joined ON group_memberships(joined_at)",
        "CREATE INDEX IF NOT EXISTS ix_relevance_group_score ON user_relevance(telegram_group_id, relevance_score)",
        "CREATE INDEX IF NOT EXISTS ix_processed_at ON processed_messages(processed_at)",
    ],
}

SCHEMA_VERSION = max(MIGRATIONS)


def apply_migrations(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version in sorted(v for v in MIGRATIONS if v > current):
        log.info("Applying database migration v%d", version)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in MIGRATIONS[version]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
