"""业务层：事项变更、定时派发、失败队列。

时间一律用 epoch 秒（int）。公开方法接受 int 或带时区的 datetime。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional, Union

from reminder_lib.models import (
    Channel,
    FailedReminder,
    Message,
    Tier,
    highest_due_tier,
)
from reminder_lib.store import Store

MAX_ATTEMPTS = 3
# 第 1 次失败后隔 1 分钟，第 2 次失败后隔 2 分钟；第 3 次失败进失败队列。
BACKOFF_SECONDS = {1: 60, 2: 120}

TimeLike = Union[int, float, datetime]


def _to_epoch(value: TimeLike) -> int:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    return int(value)


def make_message_id(item_id: str, recipient: str, tier: Tier, due_at: int) -> str:
    """消息编号：同一收件人、同一事项、同一截止时间的同一档，编号恒定。"""
    return f"{item_id}|{recipient}|t{int(tier)}|{due_at}"


def recipients_for(tier: Tier, item: dict[str, Any]) -> list[str]:
    """该档位的发送对象：前两档只发球权方，到期升级同时发球权方和项目经理。"""
    owner = item["ball_owner"]
    if tier == Tier.OVERDUE:
        pm = item["project_manager"]
        return [owner] if pm == owner else [owner, pm]
    return [owner]


class ReminderService:
    def __init__(self, db_path: str, channel: Channel) -> None:
        self._store = Store(db_path)
        self._channel = channel

    def close(self) -> None:
        self._store.close()

    # ---- 事项变更 ----

    def upsert_item(
        self,
        item_id: str,
        kind: str,
        due_at: TimeLike,
        ball_owner: str,
        project_manager: str,
        now: Optional[TimeLike] = None,
    ) -> None:
        """新建或更新事项（改截止时间、转球权、换项目经理都走这里）。"""
        self._store.upsert_item(
            item_id, kind, _to_epoch(due_at), ball_owner, project_manager, False
        )
        self._void_obsolete(self._now(now))

    def close_item(self, item_id: str, now: Optional[TimeLike] = None) -> None:
        item = self._store.get_item(item_id)
        if item is None:
            return
        self._store.upsert_item(
            item_id,
            item["kind"],
            item["due_at"],
            item["ball_owner"],
            item["project_manager"],
            True,
        )
        self._void_obsolete(self._now(now))

    # ---- 定时派发（每分钟调用一次） ----

    def dispatch(self, now: Optional[TimeLike] = None) -> None:
        epoch = self._now(now)
        self._void_obsolete(epoch)
        self._generate(epoch)
        self._send_due(epoch)

    # ---- 失败队列 ----

    def list_failed(self, now: Optional[TimeLike] = None) -> list[FailedReminder]:
        """失败队列：只含仍然有效的提醒。"""
        epoch = self._now(now)
        result = []
        for row in self._store.list_failed():
            item = self._store.get_item(row["item_id"])
            if not self._is_valid(row, item, epoch):
                continue
            result.append(
                FailedReminder(
                    message_id=row["message_id"],
                    item_id=row["item_id"],
                    recipient=row["recipient"],
                    tier=Tier(row["tier"]),
                    due_at=row["due_at"],
                    attempts=row["attempts"],
                )
            )
        return result

    # ---- 内部 ----

    @staticmethod
    def _now(now: Optional[TimeLike]) -> int:
        return _to_epoch(now) if now is not None else int(time.time())

    def _is_valid(
        self, reminder: dict[str, Any], item: Optional[dict[str, Any]], now: int
    ) -> bool:
        """未送达提醒是否仍与事项的当前情况对得上。"""
        if item is None or item["closed"]:
            return False
        if reminder["due_at"] != item["due_at"]:
            return False
        tier = Tier(reminder["tier"])
        if reminder["recipient"] not in recipients_for(tier, item):
            return False
        highest = highest_due_tier(item["due_at"], now)
        if highest is None or tier < highest:
            return False
        return True

    def _void_obsolete(self, now: int) -> None:
        for reminder in self._store.undelivered_reminders():
            item = self._store.get_item(reminder["item_id"])
            if not self._is_valid(reminder, item, now):
                self._store.mark_void(reminder["message_id"], now)

    def _generate(self, now: int) -> None:
        """为每个未关闭事项生成当前到点最高档的提醒（更低档不补）。"""
        for item in self._store.list_items():
            if item["closed"]:
                continue
            highest = highest_due_tier(item["due_at"], now)
            if highest is None:
                continue
            for recipient in recipients_for(highest, item):
                message_id = make_message_id(
                    item["item_id"], recipient, highest, item["due_at"]
                )
                if self._store.has_delivery(message_id):
                    continue
                existing = self._store.get_reminder(message_id)
                if existing is None:
                    self._store.insert_reminder(
                        message_id,
                        item["item_id"],
                        recipient,
                        int(highest),
                        item["due_at"],
                        now,
                    )
                elif existing["state"] == "voided":
                    self._store.revive_reminder(message_id, now)

    def _send_due(self, now: int) -> None:
        for reminder in self._store.due_reminders(now):
            message = Message(
                message_id=reminder["message_id"],
                item_id=reminder["item_id"],
                recipient=reminder["recipient"],
                tier=Tier(reminder["tier"]),
                due_at=reminder["due_at"],
            )
            try:
                self._channel.send(message)
            except Exception:
                attempts = reminder["attempts"] + 1
                if attempts >= MAX_ATTEMPTS:
                    self._store.mark_failed(reminder["message_id"], attempts, now)
                else:
                    self._store.mark_retry(
                        reminder["message_id"],
                        attempts,
                        now + BACKOFF_SECONDS[attempts],
                        now,
                    )
            else:
                self._store.mark_delivered(reminder["message_id"], now)
