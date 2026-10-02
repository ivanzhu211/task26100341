"""随机穿插事件 + 独立推算模型（oracle）的对拍测试。

Oracle 不调用库里任何生命周期逻辑，只共享两样东西：
- 档位触发时刻的纯函数（highest_due_tier，属业务规则常量）
- 消息编号格式（make_message_id，属库的公开输出，通道按它去重）
通道行为由 (seed, message_id, 第几次尝试) 的哈希决定，两边可独立复算。
"""
import hashlib
import random

import pytest

from reminder_lib import ReminderService, Tier
from reminder_lib.models import highest_due_tier
from reminder_lib.service import BACKOFF_SECONDS, MAX_ATTEMPTS, make_message_id

T0 = 1_700_000_000
USERS = ["u1", "u2", "u3", "u4"]
PMS = ["pm1", "pm2", "pm3"]
FAIL_PCT = 30        # 每次通道调用 30% 报失败
ACTUAL_PCT = 50      # 报失败的调用里 50% 实际已送达


def _pct(seed, message_id, attempt, tag):
    digest = hashlib.md5(f"{seed}|{message_id}|{attempt}|{tag}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 100


def _fails(seed, message_id, attempt):
    return _pct(seed, message_id, attempt, "fail") < FAIL_PCT


def _actually_delivered(seed, message_id, attempt):
    return _pct(seed, message_id, attempt, "actual") < ACTUAL_PCT


class ScriptedChannel:
    """行为完全由哈希脚本决定的假通道，记录实际送达（用户最终看到的）。"""

    def __init__(self, seed):
        self.seed = seed
        self.attempt_counts = {}
        self.calls = []
        self.actual_deliveries = []

    def send(self, message):
        mid = message.message_id
        attempt = self.attempt_counts.get(mid, 0)
        self.attempt_counts[mid] = attempt + 1
        self.calls.append(message)
        if _fails(self.seed, mid, attempt):
            if _actually_delivered(self.seed, mid, attempt):
                self.actual_deliveries.append(mid)
            raise RuntimeError("channel error")
        self.actual_deliveries.append(mid)


def _recipients(tier, owner, pm):
    if tier == int(Tier.OVERDUE):
        return [owner] if pm == owner else [owner, pm]
    return [owner]


class Oracle:
    """按业务规则独立推算：只维护普通 dict，每个 tick 朴素地重算。"""

    def __init__(self, seed):
        self.seed = seed
        self.items = {}          # id -> (due, owner, pm, closed)
        self.delivered = set()   # 通道报告成功的 key
        self.pending = {}        # key -> [attempts, next_attempt_at]
        self.failed = set()      # 失败队列里的 key
        self.attempt_counts = {}
        self.user_visible = set()

    @staticmethod
    def _mid(key):
        item_id, recipient, tier, due = key
        return make_message_id(item_id, recipient, Tier(tier), due)

    def upsert(self, item_id, due, owner, pm, closed, now):
        self.items[item_id] = (due, owner, pm, closed)
        self._void(now)

    def _valid(self, key, now):
        item_id, recipient, tier, due = key
        if item_id not in self.items:
            return False
        item_due, owner, pm, closed = self.items[item_id]
        if closed or due != item_due:
            return False
        if recipient not in _recipients(tier, owner, pm):
            return False
        highest = highest_due_tier(item_due, now)
        return highest is not None and tier >= int(highest)

    def _void(self, now):
        for key in list(self.pending):
            if not self._valid(key, now):
                del self.pending[key]
        for key in list(self.failed):
            if not self._valid(key, now):
                self.failed.discard(key)

    def dispatch(self, now):
        """返回本拍预期发送的 message_id 列表（顺序敏感）。"""
        self._void(now)
        for item_id in sorted(self.items):
            due, owner, pm, closed = self.items[item_id]
            if closed:
                continue
            highest = highest_due_tier(due, now)
            if highest is None:
                continue
            for recipient in _recipients(int(highest), owner, pm):
                key = (item_id, recipient, int(highest), due)
                if key in self.delivered or key in self.pending or key in self.failed:
                    continue
                self.pending[key] = [0, now]
        sends = []
        due_keys = sorted(
            (k for k, (a, nxt) in self.pending.items() if nxt <= now),
            key=lambda k: (self.pending[k][1], self._mid(k)),
        )
        for key in due_keys:
            mid = self._mid(key)
            sends.append(mid)
            attempt = self.attempt_counts.get(mid, 0)
            self.attempt_counts[mid] = attempt + 1
            if _fails(self.seed, mid, attempt):
                if _actually_delivered(self.seed, mid, attempt):
                    self.user_visible.add(mid)
                attempts = self.pending[key][0] + 1
                if attempts >= MAX_ATTEMPTS:
                    del self.pending[key]
                    self.failed.add(key)
                else:
                    self.pending[key] = [attempts, now + BACKOFF_SECONDS[attempts]]
            else:
                self.user_visible.add(mid)
                del self.pending[key]
                self.delivered.add(key)
        return sends

    def failed_ids(self, now):
        return {self._mid(k) for k in self.failed if self._valid(k, now)}


def run_scenario(seed):
    rng = random.Random(seed)
    channel = ScriptedChannel(seed)
    service = ReminderService(":memory:", channel)
    oracle = Oracle(seed)
    open_items = []
    next_item = 0
    t = T0
    try:
        for _ in range(400):
            # 偶发调度停机：跳过若干小时不派发
            if rng.random() < 0.02:
                t += 3600 * rng.randint(1, 48)
                continue
            # 随机事件：建事项 / 改截止 / 转球权 / 换项目经理 / 关闭
            for _ in range(rng.randint(0, 2)):
                roll = rng.random()
                if (roll < 0.25 or not open_items) and next_item < 10:
                    item_id = f"I{next_item}"
                    next_item += 1
                    due = t + rng.randint(1800, 60 * 3600)
                    owner, pm = rng.choice(USERS), rng.choice(PMS)
                    kind = rng.choice(["RFI", "SUBMITTAL"])
                    service.upsert_item(item_id, kind, due, owner, pm, now=t)
                    oracle.upsert(item_id, due, owner, pm, False, t)
                    open_items.append(item_id)
                elif open_items:
                    item_id = rng.choice(open_items)
                    due, owner, pm, _ = oracle.items[item_id]
                    action = rng.random()
                    if action < 0.30:
                        due = t + rng.randint(-2 * 3600, 60 * 3600)
                    elif action < 0.60:
                        owner = rng.choice([u for u in USERS if u != owner] or [owner])
                    elif action < 0.80:
                        pm = rng.choice([p for p in PMS if p != pm] or [pm])
                    elif action < 0.90:
                        service.close_item(item_id, now=t)
                        oracle.upsert(item_id, due, owner, pm, True, t)
                        open_items.remove(item_id)
                        continue
                    else:
                        pass  # 无操作，纯粹消耗一个事件槽
                    service.upsert_item(item_id, "RFI", due, owner, pm, now=t)
                    oracle.upsert(item_id, due, owner, pm, False, t)
            # 每分钟一次的派发
            expected = oracle.dispatch(t)
            before = len(channel.calls)
            service.dispatch(now=t)
            actual = [m.message_id for m in channel.calls[before:]]
            assert actual == expected, f"seed={seed} t={t}\nactual={actual}\nexpected={expected}"
            # 每一拍失败队列都要一致（只含仍然有效的提醒）
            assert {f.message_id for f in service.list_failed(now=t)} == oracle.failed_ids(t), \
                f"seed={seed} t={t}"
            t += 60
        # 最终一致：用户看到的 + 失败队列
        assert set(channel.actual_deliveries) == oracle.user_visible
        final_failed = {f.message_id for f in service.list_failed(now=t)}
        assert final_failed == oracle.failed_ids(t)
    finally:
        service.close()


@pytest.mark.parametrize("seed", range(20))
def test_randomized_against_oracle(seed):
    run_scenario(seed)
