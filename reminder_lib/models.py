"""类型定义：提醒档位、状态、事项与提醒记录。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import IntEnum


class Tier(IntEnum):
    """提醒档位。数值越大优先级越高。"""

    BEFORE_48H = 1  # 截止前48小时
    BEFORE_24H = 2  # 截止前24小时
    ESCALATION = 3  # 到期升级


class ReminderStatus:
    PENDING = "pending"  # 待投递/等待重试
    SENT = "sent"        # 已送达
    DEAD = "dead"        # 3次自动尝试均失败，进失败队列
    VOID = "void"        # 已作废


# 各档位触发时刻相对截止时间的偏移（秒）
TIER_OFFSET_SECONDS = {
    Tier.BEFORE_48H: -48 * 3600,
    Tier.BEFORE_24H: -24 * 3600,
    Tier.ESCALATION: 0,
}

# 指数退避：第1次失败后1分钟，第2次失败后2分钟，共自动尝试3次
RETRY_BACKOFF_SECONDS = [60, 120]
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class Item:
    """一条RFI/报审事项的当前状态。"""

    item_id: str
    due_at: float          # 截止时间，epoch秒
    ball_owner: str        # 当前球权方
    project_manager: str   # 项目经理
    closed: bool = False


@dataclass(frozen=True)
class Reminder:
    """一条提醒记录。"""

    message_id: str
    item_id: str
    tier: Tier
    due_at: float          # 生成时的截止时间快照
    recipient: str
    status: str
    attempts: int
    next_attempt_at: float


def make_message_id(item_id: str, due_at: float, tier: Tier, recipient: str) -> str:
    """消息编号：对（事项, 截止时间, 档位, 收件人）确定，供通道去重。"""
    raw = f"{item_id}|{due_at!r}|{int(tier)}|{recipient}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()
