"""验收测试：随机穿插改截止时间、转球权、换项目经理、关闭、通道失败和派发，
用户最终看到的提醒和失败队列必须与按规则独立推算的结果完全一致。"""
import random

import pytest

from reminder_lib import ReminderService
from reminder_lib.models import (
    RETRY_BACKOFF_SECONDS,
    TIER_OFFSET_SECONDS,
    Tier,
    make_message_id,
)

PENDING, SENT, DEAD, VOID = "pending", "sent", "dead", "void"
PEOPLE = ["alice", "bob", "carol", "dave"]
PMS = ["pm1", "pm2", "pm3"]


class RefModel:
    """按业务规则独立推算的参考模型。"""

    def __init__(self):
        self.items = {}  # id -> dict(due, owner, pm, closed)
        self.rems = {}   # (item_id, due, tier, recipient) -> [status, attempts, next_try]
        self.delivered = []  # 实际送达（含报告失败但已送达的）

    # ---- 与库无关的规则推算 ----

    @staticmethod
    def _target(item, now):
        due = [t for t in Tier if now >= item["due"] + TIER_OFFSET_SECONDS[t]]
        return max(due) if due else None

    @staticmethod
    def _recips(item, tier):
        if tier == Tier.ESCALATION:
            return {item["owner"], item["pm"]}
        return {item["owner"]}

    def _valid(self, key, now):
        item_id, due, tier, rec = key
        item = self.items.get(item_id)
        if item is None or item["closed"]:
            return False
        if item["due"] != due:
            return False
        if self._target(item, now) != tier:
            return False
        return rec in self._recips(item, tier)

    # ---- 事件 ----

    def upsert(self, item_id, due, owner, pm):
        self.items[item_id] = {"due": due, "owner": owner, "pm": pm, "closed": False}

    def close(self, item_id):
        if item_id in self.items:
            self.items[item_id]["closed"] = True

    def dispatch(self, now, channel):
        for key, r in self.rems.items():
            if r[0] in (PENDING, DEAD) and not self._valid(key, now):
                r[0] = VOID
        for item_id, item in self.items.items():
            if item["closed"]:
                continue
            tier = self._target(item, now)
            if tier is None:
                continue
            for rec in self._recips(item, tier):
                key = (item_id, item["due"], tier, rec)
                if key not in self.rems or self.rems[key][0] == VOID:
                    self.rems[key] = [PENDING, 0, now]
        for key in sorted(self.rems):
            r = self.rems[key]
            if r[0] != PENDING or r[2] > now:
                continue
            mid = make_message_id(key[0], key[1], key[2], key[3])
            reported_ok, actually = channel(mid)
            if actually:
                self.delivered.append((mid, key[3]))
            if reported_ok:
                r[0] = SENT
            else:
                r[1] += 1
                if r[1] >= 3:
                    r[0] = DEAD
                else:
                    r[2] = now + RETRY_BACKOFF_SECONDS[r[1] - 1]

    def failed_queue(self, now):
        return {
            make_message_id(k[0], k[1], k[2], k[3])
            for k, r in self.rems.items()
            if r[0] == DEAD and self._valid(k, now)
        }

    def visible(self):
        seen, out = set(), set()
        for mid, rec in self.delivered:
            if mid not in seen:
                seen.add(mid)
                out.add((mid, rec))
        return out


class ScriptedChannel:
    """通道行为按 (message_id, 第几次调用) 预生成，库与参考模型看到相同结果。
    报告失败的调用也有一定概率实际已经送达。"""

    def __init__(self, seed, fail_rate=0.3, hidden_delivery_rate=0.3):
        self.seed = seed
        self.fail_rate = fail_rate
        self.hidden_delivery_rate = hidden_delivery_rate
        self.calls = {"lib": {}, "ref": {}}
        self.lib_delivered = []

    def outcome(self, mid, side):
        counts = self.calls[side]
        idx = counts.get(mid, 0)
        counts[mid] = idx + 1
        rng = random.Random(f"{self.seed}|{mid}|{idx}|{self.fail_rate}")
        reported_ok = rng.random() >= self.fail_rate
        actually = reported_ok or rng.random() < self.hidden_delivery_rate
        return reported_ok, actually

    def for_lib(self, mid, recipient, payload):
        reported_ok, actually = self.outcome(mid, "lib")
        if actually:
            self.lib_delivered.append((mid, recipient))
        return reported_ok

    def for_ref(self, mid):
        return self.outcome(mid, "ref")

    def visible(self):
        seen, out = set(), set()
        for mid, rec in self.lib_delivered:
            if mid not in seen:
                seen.add(mid)
                out.add((mid, rec))
        return out


def run_randomized(seed):
    rng = random.Random(seed)
    now = 1_700_000_000.0
    channel = ScriptedChannel(seed)
    svc = ReminderService(":memory:", channel.for_lib)
    ref = RefModel()
    item_ids = [f"RFI-{i}" for i in range(4)] + [f"SUB-{i}" for i in range(4)]

    for step in range(400):
        # 时间前进：通常几分钟，偶尔停一个周末
        now += rng.choice([0, 30, 60, 60, 120, 300, 3600, 7200, 200_000])
        op = rng.random()
        if op < 0.45:
            item_id = rng.choice(item_ids)
            due = now + rng.uniform(-60 * 3600, 120 * 3600)
            owner = rng.choice(PEOPLE)
            pm = rng.choice(PMS)
            svc.upsert_item(item_id, due_at=due, ball_owner=owner, project_manager=pm)
            ref.upsert(item_id, due, owner, pm)
        elif op < 0.55:
            item_id = rng.choice(item_ids)
            svc.close_item(item_id)
            ref.close(item_id)
        elif op < 0.65:
            channel.fail_rate = rng.choice([0.0, 0.2, 0.5, 0.9])
        svc.dispatch(now)
        ref.dispatch(now, channel.for_ref)

    # 收尾：把通道修好，再派发足够多次，让所有有效提醒落定
    channel.fail_rate = 0.0
    for _ in range(5):
        now += 3600
        svc.dispatch(now)
        ref.dispatch(now, channel.for_ref)

    assert channel.visible() == ref.visible(), "用户最终看到的提醒不一致"
    assert (
        {r.message_id for r in svc.failed_queue(now)} == ref.failed_queue(now)
    ), "失败队列不一致"
    svc.close()


@pytest.mark.parametrize("seed", range(30))
def test_randomized_against_reference(seed):
    run_randomized(seed)
