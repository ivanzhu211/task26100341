"""催办提醒服务：由上层项目服务与定时任务调用。"""
from __future__ import annotations

import time
from typing import Callable, Optional

from .models import (
    MAX_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
    TIER_OFFSET_SECONDS,
    Item,
    Reminder,
    ReminderStatus,
    Tier,
    make_message_id,
)
from .store import Store

# 投递通道：send(message_id, recipient, payload)，返回False或抛异常视为失败
Channel = Callable[[str, str, dict], object]


def _target_tier(item: Item, now: float) -> Optional[Tier]:
    """当前已经到点的最高一档；未到任何一档返回None。正好相等算到点。"""
    due = [t for t in Tier if now >= item.due_at + TIER_OFFSET_SECONDS[t]]
    return max(due) if due else None


def _recipients(item: Item, tier: Tier) -> set[str]:
    """前两档只发球权方；到期升级同时发球权方和项目经理。"""
    if tier == Tier.ESCALATION:
        return {item.ball_owner, item.project_manager}
    return {item.ball_owner}


class ReminderService:
    """RFI/报审截止催办提醒模块。"""

    def __init__(self, db_path: str, channel: Channel) -> None:
        self._store = Store(db_path)
        self._channel = channel

    def upsert_item(
        self,
        item_id: str,
        due_at: float,
        ball_owner: str,
        project_manager: str,
    ) -> None:
        """创建或更新事项（改截止时间/转球权/换项目经理）。"""
        self._store.upsert_item(
            Item(item_id=item_id, due_at=due_at, ball_owner=ball_owner,
                 project_manager=project_manager, closed=False)
        )

    def close_item(self, item_id: str) -> None:
        """关闭事项。"""
        item = self._store.get_item(item_id)
        if item is None:
            return
        self._store.upsert_item(
            Item(item_id=item.item_id, due_at=item.due_at,
                 ball_owner=item.ball_owner,
                 project_manager=item.project_manager, closed=True)
        )

    def _is_valid(self, reminder: Reminder, item: Optional[Item], now: float) -> bool:
        """未送达提醒是否与事项当前状态对得上。"""
        if item is None or item.closed:
            return False
        if reminder.due_at != item.due_at:
            return False
        if _target_tier(item, now) != reminder.tier:
            return False
        return reminder.recipient in _recipients(item, reminder.tier)

    def _sweep(self, items: dict[str, Item], now: float) -> None:
        """作废跟当前情况对不上的未送达提醒（含等待重试与失败队列）。"""
        for r in self._store.reminders_by_status(
            ReminderStatus.PENDING, ReminderStatus.DEAD
        ):
            if not self._is_valid(r, items.get(r.item_id), now):
                self._store.void_reminder(r.message_id)

    def _generate(self, items: dict[str, Item], now: float) -> None:
        """为每个未关闭事项生成当前最高档的缺失提醒（同编号已存在则跳过）。"""
        for item in items.values():
            if item.closed:
                continue
            tier = _target_tier(item, now)
            if tier is None:
                continue
            for recipient in _recipients(item, tier):
                self._store.upsert_pending_reminder(
                    Reminder(
                        message_id=make_message_id(
                            item.item_id, item.due_at, tier, recipient
                        ),
                        item_id=item.item_id,
                        tier=tier,
                        due_at=item.due_at,
                        recipient=recipient,
                        status=ReminderStatus.PENDING,
                        attempts=0,
                        next_attempt_at=now,
                    )
                )

    def _deliver(self, now: float) -> None:
        """投递到点的待办提醒，失败按指数退避重试，3次失败进失败队列。"""
        for r in self._store.due_pending_reminders(now):
            payload = {
                "item_id": r.item_id,
                "tier": int(r.tier),
                "due_at": r.due_at,
            }
            try:
                result = self._channel(r.message_id, r.recipient, payload)
                ok = result is not False
            except Exception:
                ok = False
            if ok:
                self._store.mark_sent(r.message_id)
                continue
            attempts = r.attempts + 1
            if attempts >= MAX_ATTEMPTS:
                self._store.mark_attempt_failed(
                    r.message_id, attempts, now, ReminderStatus.DEAD
                )
            else:
                backoff = RETRY_BACKOFF_SECONDS[attempts - 1]
                self._store.mark_attempt_failed(
                    r.message_id, attempts, now + backoff, ReminderStatus.PENDING
                )

    def dispatch(self, now: float) -> None:
        """定时任务每分钟调用一次：作废旧提醒、生成新提醒、投递与重试。"""
        items = {i.item_id: i for i in self._store.all_items()}
        self._sweep(items, now)
        self._generate(items, now)
        self._deliver(now)

    def failed_queue(self, now: Optional[float] = None) -> list[Reminder]:
        """失败队列：只含仍然有效的提醒，供人工查看。"""
        if now is None:
            now = time.time()
        items = {i.item_id: i for i in self._store.all_items()}
        return [
            r
            for r in self._store.reminders_by_status(ReminderStatus.DEAD)
            if self._is_valid(r, items.get(r.item_id), now)
        ]

    def close(self) -> None:
        self._store.close()
