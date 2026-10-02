"""类型定义：提醒档位、消息、失败队列条目。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Optional, Protocol


class Tier(IntEnum):
    """提醒档位：截止前48小时、截止前24小时、到期升级。"""

    BEFORE_48H = 1
    BEFORE_24H = 2
    OVERDUE = 3


# 各档位相对截止时间的提前量（秒）；到期升级不提前。
TIER_LEAD_SECONDS = {
    Tier.BEFORE_48H: 48 * 3600,
    Tier.BEFORE_24H: 24 * 3600,
    Tier.OVERDUE: 0,
}


def trigger_time(tier: Tier, due_at: int) -> int:
    """档位到点时刻（epoch 秒）。到点判断为 now >= trigger，正好相等也算。"""
    return due_at - TIER_LEAD_SECONDS[tier]


def highest_due_tier(due_at: int, now: int) -> Optional[Tier]:
    """当前已经到点的最高一档；都未到点返回 None。"""
    tier: Optional[Tier] = None
    for candidate in Tier:
        if now >= trigger_time(candidate, due_at):
            tier = candidate
    return tier


@dataclass(frozen=True)
class Message:
    """通过通道投递的一条提醒。message_id 由通道按编号去重。"""

    message_id: str
    item_id: str
    recipient: str
    tier: Tier
    due_at: int  # 截止时间的 epoch 秒


@dataclass(frozen=True)
class FailedReminder:
    """失败队列条目：自动重试 3 次仍失败、且仍然有效的提醒。"""

    message_id: str
    item_id: str
    recipient: str
    tier: Tier
    due_at: int
    attempts: int


class Channel(Protocol):
    """调用方提供的投递通道。send 抛异常表示本次投递失败（但可能实际已送达）。"""

    def send(self, message: Message) -> None: ...
