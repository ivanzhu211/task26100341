"""SQLite 持久化层：事项、提醒、送达历史。只做存取，不含业务规则。"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id         TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    due_at          INTEGER NOT NULL,
    ball_owner      TEXT NOT NULL,
    project_manager TEXT NOT NULL,
    closed          INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reminders (
    message_id       TEXT PRIMARY KEY,
    item_id          TEXT NOT NULL,
    recipient        TEXT NOT NULL,
    tier             INTEGER NOT NULL,
    due_at           INTEGER NOT NULL,
    state            TEXT NOT NULL,          -- pending / delivered / failed / voided
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminders_pending
    ON reminders(state, next_attempt_at);
CREATE TABLE IF NOT EXISTS deliveries (
    message_id   TEXT PRIMARY KEY,
    delivered_at INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ---- 事项 ----

    def upsert_item(
        self,
        item_id: str,
        kind: str,
        due_at: int,
        ball_owner: str,
        project_manager: str,
        closed: bool,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO items(item_id, kind, due_at, ball_owner, project_manager, closed)"
                " VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(item_id) DO UPDATE SET kind=excluded.kind,"
                " due_at=excluded.due_at, ball_owner=excluded.ball_owner,"
                " project_manager=excluded.project_manager, closed=excluded.closed",
                (item_id, kind, due_at, ball_owner, project_manager, int(closed)),
            )

    def get_item(self, item_id: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM items WHERE item_id = ?", (item_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_items(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM items").fetchall()
        return [dict(r) for r in rows]

    # ---- 提醒 ----

    def get_reminder(self, message_id: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM reminders WHERE message_id = ?", (message_id,)
        ).fetchone()
        return dict(row) if row else None

    def insert_reminder(
        self,
        message_id: str,
        item_id: str,
        recipient: str,
        tier: int,
        due_at: int,
        now: int,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO reminders(message_id, item_id, recipient, tier, due_at,"
                " state, attempts, next_attempt_at, updated_at)"
                " VALUES(?,?,?,?,?, 'pending', 0, ?, ?)",
                (message_id, item_id, recipient, tier, due_at, now, now),
            )

    def revive_reminder(self, message_id: str, now: int) -> None:
        """把作废的提醒重新置为待派发（attempts 清零）。"""
        with self._conn:
            self._conn.execute(
                "UPDATE reminders SET state='pending', attempts=0,"
                " next_attempt_at=?, updated_at=? WHERE message_id=?",
                (now, now, message_id),
            )

    def due_reminders(self, now: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM reminders WHERE state='pending' AND next_attempt_at <= ?"
            " ORDER BY next_attempt_at, message_id",
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]

    def undelivered_reminders(self) -> list[dict[str, Any]]:
        """所有未送达的提醒（待派发 + 失败队列）。"""
        rows = self._conn.execute(
            "SELECT * FROM reminders WHERE state IN ('pending','failed')"
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_delivered(self, message_id: str, now: int) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE reminders SET state='delivered', updated_at=? WHERE message_id=?",
                (now, message_id),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO deliveries(message_id, delivered_at) VALUES(?,?)",
                (message_id, now),
            )

    def mark_retry(self, message_id: str, attempts: int, next_attempt_at: int, now: int) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE reminders SET attempts=?, next_attempt_at=?, updated_at=?"
                " WHERE message_id=?",
                (attempts, next_attempt_at, now, message_id),
            )

    def mark_failed(self, message_id: str, attempts: int, now: int) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE reminders SET state='failed', attempts=?, updated_at=?"
                " WHERE message_id=?",
                (attempts, now, message_id),
            )

    def mark_void(self, message_id: str, now: int) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE reminders SET state='voided', updated_at=? WHERE message_id=?",
                (now, message_id),
            )

    # ---- 送达历史 ----

    def has_delivery(self, message_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM deliveries WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    def list_failed(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM reminders WHERE state='failed' ORDER BY message_id"
        ).fetchall()
        return [dict(r) for r in rows]
