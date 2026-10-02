"""SQLite持久化层。"""
from __future__ import annotations

import sqlite3

from .models import Item, Reminder, ReminderStatus, Tier

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id TEXT PRIMARY KEY,
    due_at REAL NOT NULL,
    ball_owner TEXT NOT NULL,
    project_manager TEXT NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reminders (
    message_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    tier INTEGER NOT NULL,
    due_at REAL NOT NULL,
    recipient TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reminders_item ON reminders(item_id);
CREATE INDEX IF NOT EXISTS idx_reminders_status ON reminders(status);
"""


def _row_to_item(row: sqlite3.Row) -> Item:
    return Item(
        item_id=row["item_id"],
        due_at=row["due_at"],
        ball_owner=row["ball_owner"],
        project_manager=row["project_manager"],
        closed=bool(row["closed"]),
    )


def _row_to_reminder(row: sqlite3.Row) -> Reminder:
    return Reminder(
        message_id=row["message_id"],
        item_id=row["item_id"],
        tier=Tier(row["tier"]),
        due_at=row["due_at"],
        recipient=row["recipient"],
        status=row["status"],
        attempts=row["attempts"],
        next_attempt_at=row["next_attempt_at"],
    )


class Store:
    """提醒与事项状态的SQLite存储。"""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- 事项 ----

    def upsert_item(self, item: Item) -> None:
        self._conn.execute(
            "INSERT INTO items (item_id, due_at, ball_owner, project_manager, closed)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(item_id) DO UPDATE SET"
            " due_at=excluded.due_at, ball_owner=excluded.ball_owner,"
            " project_manager=excluded.project_manager, closed=excluded.closed",
            (item.item_id, item.due_at, item.ball_owner, item.project_manager,
             int(item.closed)),
        )
        self._conn.commit()

    def get_item(self, item_id: str) -> Item | None:
        row = self._conn.execute(
            "SELECT * FROM items WHERE item_id = ?", (item_id,)
        ).fetchone()
        return _row_to_item(row) if row else None

    def all_items(self) -> list[Item]:
        rows = self._conn.execute("SELECT * FROM items").fetchall()
        return [_row_to_item(r) for r in rows]

    # ---- 提醒 ----

    def insert_reminder_if_absent(self, reminder: Reminder) -> bool:
        """已存在同message_id记录（任意状态）则不插入。返回是否插入。"""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO reminders"
            " (message_id, item_id, tier, due_at, recipient, status, attempts, next_attempt_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (reminder.message_id, reminder.item_id, int(reminder.tier),
             reminder.due_at, reminder.recipient, reminder.status,
             reminder.attempts, reminder.next_attempt_at),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def upsert_pending_reminder(self, reminder: Reminder) -> None:
        """不存在则插入；已作废（从未送达）的重新激活；其余状态保持不变。"""
        self._conn.execute(
            "INSERT INTO reminders"
            " (message_id, item_id, tier, due_at, recipient, status, attempts, next_attempt_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(message_id) DO UPDATE SET"
            " status = CASE WHEN reminders.status = ? THEN ? ELSE reminders.status END,"
            " attempts = CASE WHEN reminders.status = ? THEN 0 ELSE reminders.attempts END,"
            " next_attempt_at = CASE WHEN reminders.status = ?"
            "   THEN excluded.next_attempt_at ELSE reminders.next_attempt_at END",
            (reminder.message_id, reminder.item_id, int(reminder.tier),
             reminder.due_at, reminder.recipient, reminder.status,
             reminder.attempts, reminder.next_attempt_at,
             ReminderStatus.VOID, ReminderStatus.PENDING,
             ReminderStatus.VOID,
             ReminderStatus.VOID),
        )
        self._conn.commit()

    def get_reminder(self, message_id: str) -> Reminder | None:
        row = self._conn.execute(
            "SELECT * FROM reminders WHERE message_id = ?", (message_id,)
        ).fetchone()
        return _row_to_reminder(row) if row else None

    def reminders_by_status(self, *statuses: str) -> list[Reminder]:
        placeholders = ",".join("?" for _ in statuses)
        rows = self._conn.execute(
            f"SELECT * FROM reminders WHERE status IN ({placeholders})", statuses
        ).fetchall()
        return [_row_to_reminder(r) for r in rows]

    def due_pending_reminders(self, now: float) -> list[Reminder]:
        rows = self._conn.execute(
            "SELECT * FROM reminders WHERE status = ? AND next_attempt_at <= ?"
            " ORDER BY next_attempt_at",
            (ReminderStatus.PENDING, now),
        ).fetchall()
        return [_row_to_reminder(r) for r in rows]

    def mark_sent(self, message_id: str) -> None:
        self._conn.execute(
            "UPDATE reminders SET status = ? WHERE message_id = ?",
            (ReminderStatus.SENT, message_id),
        )
        self._conn.commit()

    def mark_attempt_failed(
        self, message_id: str, attempts: int, next_attempt_at: float, status: str
    ) -> None:
        self._conn.execute(
            "UPDATE reminders SET attempts = ?, next_attempt_at = ?, status = ?"
            " WHERE message_id = ?",
            (attempts, next_attempt_at, status, message_id),
        )
        self._conn.commit()

    def void_reminder(self, message_id: str) -> None:
        self._conn.execute(
            "UPDATE reminders SET status = ? WHERE message_id = ?",
            (ReminderStatus.VOID, message_id),
        )
        self._conn.commit()
