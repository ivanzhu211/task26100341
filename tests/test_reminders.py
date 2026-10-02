"""催办提醒模块的确定性单元测试。"""
import pytest

from reminder_lib import ReminderService, ReminderStatus, Tier
from reminder_lib.models import make_message_id

T0 = 1_700_000_000.0
HOUR = 3600.0


class FakeChannel:
    """记录成功投递；可按需配置失败。"""

    def __init__(self):
        self.delivered = []  # (message_id, recipient) 实际送达
        self.fail_ids = set()

    def __call__(self, message_id, recipient, payload):
        if message_id in self.fail_ids:
            return False
        self.delivered.append((message_id, recipient))
        return True

    def visible(self):
        """通道按编号去重后，用户实际看到的 (message_id, recipient)。"""
        seen = set()
        out = []
        for mid, r in self.delivered:
            if mid not in seen:
                seen.add(mid)
                out.append((mid, r))
        return out


@pytest.fixture()
def svc():
    ch = FakeChannel()
    service = ReminderService(":memory:", ch)
    yield service, ch
    service.close()


def make_item(svc, item_id="RFI-1", due_at=T0 + 100 * HOUR, owner="alice", pm="pm1"):
    svc.upsert_item(item_id, due_at=due_at, ball_owner=owner, project_manager=pm)


def test_no_reminder_before_first_tier(svc):
    svc, ch = svc
    make_item(svc)
    svc.dispatch(T0 + 51 * HOUR)
    assert ch.delivered == []


def test_tier_boundaries_equal_counts_as_due(svc):
    svc, ch = svc
    make_item(svc)
    due = T0 + 100 * HOUR
    svc.dispatch(due - 48 * HOUR)  # 正好相等算到点
    assert [r for _, r in ch.delivered] == ["alice"]
    svc.dispatch(due - 24 * HOUR)
    svc.dispatch(due)
    tiers = [m for m, _ in ch.delivered]
    assert len(tiers) == 4  # 48h、24h、到期x2（球权方+项目经理）


def test_only_highest_due_tier_lower_pending_voided(svc):
    svc, ch = svc
    make_item(svc)
    due = T0 + 100 * HOUR
    ch.fail_ids.add(make_message_id("RFI-1", due, Tier.BEFORE_48H, "alice"))
    svc.dispatch(due - 48 * HOUR)  # 48h档投递失败，进入退避
    svc.dispatch(due - 24 * HOUR)  # 已到24h档：低档不再发也不再重试
    assert [r for _, r in ch.delivered] == ["alice"]
    mid48 = make_message_id("RFI-1", due, Tier.BEFORE_48H, "alice")
    assert all(m != mid48 for m, _ in ch.delivered)


def test_long_gap_only_backfills_current_highest(svc):
    svc, ch = svc
    make_item(svc)
    due = T0 + 100 * HOUR
    svc.dispatch(due + 10 * HOUR)  # 调度停了一个周末，恢复后只补到期升级档
    assert sorted(r for _, r in ch.delivered) == ["alice", "pm1"]
    assert len(ch.delivered) == 2


def test_same_key_delivered_at_most_once(svc):
    svc, ch = svc
    make_item(svc)
    due = T0 + 100 * HOUR
    svc.dispatch(due - 48 * HOUR)
    for i in range(1, 10):  # 重复派发不重发
        svc.dispatch(due - 48 * HOUR + i * 60)
    assert len(ch.delivered) == 1


def test_due_changed_away_and_back_no_resend(svc):
    svc, ch = svc
    make_item(svc)
    due = T0 + 100 * HOUR
    svc.dispatch(due - 48 * HOUR)
    assert len(ch.delivered) == 1
    make_item(svc, due_at=due + 5 * HOUR)  # 改走
    svc.dispatch(due - 47 * HOUR)
    make_item(svc, due_at=due)  # 改回原值
    svc.dispatch(due - 46 * HOUR)
    svc.dispatch(due - 24 * HOUR + 1)
    # 48h档不重复；只有新的24h档
    assert len(ch.delivered) == 2


def test_ball_transfer_new_owner_gets_current_tier(svc):
    svc, ch = svc
    make_item(svc)
    due = T0 + 100 * HOUR
    svc.dispatch(due - 48 * HOUR)
    make_item(svc, owner="bob")  # 转球权
    svc.dispatch(due - 47 * HOUR)  # 新球权方马上收到当前已到点的48h档
    assert ch.delivered[-1] == (make_message_id("RFI-1", due, Tier.BEFORE_48H, "bob"), "bob")
    svc.dispatch(due - 24 * HOUR)
    assert ch.delivered[-1][1] == "bob"
    assert all(r != "alice" or m == make_message_id("RFI-1", due, Tier.BEFORE_48H, "alice")
               for m, r in ch.delivered)


def test_undelivered_voided_on_due_change(svc):
    svc, ch = svc
    make_item(svc)
    due = T0 + 100 * HOUR
    ch.fail_ids.add(make_message_id("RFI-1", due, Tier.BEFORE_48H, "alice"))
    svc.dispatch(due - 48 * HOUR)  # 失败，等待重试
    make_item(svc, due_at=due + 5 * HOUR)  # 改截止时间
    svc.dispatch(due - 47 * HOUR)  # 旧提醒作废，新截止未到点
    assert ch.delivered == []
    assert svc.failed_queue(due - 47 * HOUR) == []


def test_close_item_voids_everything_undelivered(svc):
    svc, ch = svc
    make_item(svc)
    due = T0 + 100 * HOUR
    ch.fail_ids.add(make_message_id("RFI-1", due, Tier.BEFORE_48H, "alice"))
    svc.dispatch(due - 48 * HOUR)
    svc.close_item("RFI-1")
    svc.dispatch(due - 47 * HOUR)
    svc.dispatch(due + HOUR)
    assert ch.delivered == []
    assert svc.failed_queue(due + HOUR) == []


def test_retry_backoff_and_dead_queue(svc):
    svc, ch = svc
    make_item(svc)
    due = T0 + 100 * HOUR
    t = due - 48 * HOUR
    mid = make_message_id("RFI-1", due, Tier.BEFORE_48H, "alice")
    ch.fail_ids.add(mid)
    svc.dispatch(t)            # 第1次失败 → 1分钟后重试
    svc.dispatch(t + 30)       # 未到重试时间
    svc.dispatch(t + 60)       # 第2次失败 → 2分钟后
    svc.dispatch(t + 120)      # 未到
    svc.dispatch(t + 180)      # 第3次失败 → 进失败队列
    assert ch.delivered == []
    dead = svc.failed_queue(t + 180)
    assert [r.message_id for r in dead] == [mid]
    assert dead[0].attempts == 3
    svc.dispatch(t + 10_000)   # 失败队列不再自动重试
    assert ch.delivered == []


def test_failed_but_actually_delivered_deduped_by_channel(svc):
    svc, ch = svc
    make_item(svc)
    due = T0 + 100 * HOUR
    t = due - 48 * HOUR
    mid = make_message_id("RFI-1", due, Tier.BEFORE_48H, "alice")

    def flaky(message_id, recipient, payload):
        ch.delivered.append((message_id, recipient))  # 实际送到了
        return False                                   # 但报告失败

    svc2 = ReminderService(":memory:", flaky)
    svc2.upsert_item("RFI-1", due_at=due, ball_owner="alice", project_manager="pm1")
    svc2.dispatch(t)
    svc2.dispatch(t + 60)  # 重试又送一次，同编号
    assert ch.delivered.count((mid, "alice")) == 2
    assert ch.visible() == [(mid, "alice")]  # 用户只看到一条
    svc2.close()


def test_failed_queue_only_keeps_valid(svc):
    svc, ch = svc
    make_item(svc)
    due = T0 + 100 * HOUR
    t = due - 48 * HOUR
    mid = make_message_id("RFI-1", due, Tier.BEFORE_48H, "alice")
    ch.fail_ids.add(mid)
    svc.dispatch(t)
    svc.dispatch(t + 60)
    svc.dispatch(t + 180)  # 3次失败进失败队列
    assert len(svc.failed_queue(t + 180)) == 1
    make_item(svc, owner="bob")  # 球权转走，失败队列里的旧提醒失效
    assert svc.failed_queue(t + 181) == []


def test_pm_change_voids_escalation_to_old_pm(svc):
    svc, ch = svc
    make_item(svc)
    due = T0 + 100 * HOUR
    mid_pm = make_message_id("RFI-1", due, Tier.ESCALATION, "pm1")
    ch.fail_ids.add(mid_pm)
    svc.dispatch(due)  # 到期升级：alice成功，pm1失败待重试
    assert [r for _, r in ch.delivered] == ["alice"]
    make_item(svc, pm="pm2")  # 换项目经理
    svc.dispatch(due + 60)
    # 旧PM的未送达提醒作废，新PM收到升级提醒
    assert ch.delivered[-1] == (make_message_id("RFI-1", due, Tier.ESCALATION, "pm2"), "pm2")
    assert all(r != "pm1" for _, r in ch.delivered)


def test_voided_undelivered_revived_when_valid_again(svc):
    svc, ch = svc
    make_item(svc)
    due = T0 + 100 * HOUR
    t = due - 48 * HOUR
    mid = make_message_id("RFI-1", due, Tier.BEFORE_48H, "alice")
    ch.fail_ids.add(mid)
    svc.dispatch(t)                    # 失败待重试
    make_item(svc, due_at=due + 5 * HOUR)
    svc.dispatch(t + 30)               # 改走 → 作废
    make_item(svc, due_at=due)         # 改回 → 从未送达的提醒应重新激活
    ch.fail_ids.discard(mid)
    svc.dispatch(t + 60)
    assert ch.delivered == [(mid, "alice")]


def test_state_survives_restart(tmp_path):
    db = str(tmp_path / "reminders.db")
    ch1 = FakeChannel()
    svc = ReminderService(db, ch1)
    due = T0 + 100 * HOUR
    t = due - 48 * HOUR
    mid = make_message_id("RFI-1", due, Tier.BEFORE_48H, "alice")
    svc.upsert_item("RFI-1", due_at=due, ball_owner="alice", project_manager="pm1")
    ch1.fail_ids.add(mid)
    svc.dispatch(t)  # 失败一次后调度机器停机
    svc.close()

    ch2 = FakeChannel()  # 恢复后通道正常
    svc2 = ReminderService(db, ch2)
    svc2.dispatch(t + 200_000)  # 停了一个周末，只补当前最高档（到期升级）
    assert sorted(r for _, r in ch2.delivered) == ["alice", "pm1"]
    svc2.close()
