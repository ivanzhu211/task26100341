"""业务规则的确定性单元测试。"""
import pytest

from reminder_lib import ReminderService, Tier
from reminder_lib.service import make_message_id

T0 = 1_700_000_000
DUE = T0 + 100 * 3600  # 截止时间


class FakeChannel:
    """记录每次调用；可配置对哪些 message_id 抛异常。"""

    def __init__(self, fail_ids=()):
        self.fail_ids = set(fail_ids)
        self.calls = []  # 每次调用（含失败）

    def send(self, message):
        self.calls.append(message)
        if message.message_id in self.fail_ids:
            raise RuntimeError("channel error")

    @property
    def sent_ids(self):
        return [m.message_id for m in self.calls]


@pytest.fixture()
def svc(tmp_path):
    channel = FakeChannel()
    service = ReminderService(str(tmp_path / "test.db"), channel)
    yield service, channel
    service.close()


def make_item(svc, item_id="I1", due=DUE, owner="alice", pm="pm1", now=T0):
    svc.upsert_item(item_id, "RFI", due, owner, pm, now=now)


def test_tier_triggers_and_exact_equality(svc):
    svc, ch = svc
    make_item(svc)
    svc.dispatch(now=DUE - 48 * 3600 - 1)
    assert ch.calls == []
    svc.dispatch(now=DUE - 48 * 3600)  # 正好相等算到点
    assert [m.tier for m in ch.calls] == [Tier.BEFORE_48H]
    assert ch.calls[0].recipient == "alice"
    svc.dispatch(now=DUE - 24 * 3600)
    assert [m.tier for m in ch.calls] == [Tier.BEFORE_48H, Tier.BEFORE_24H]
    svc.dispatch(now=DUE)
    assert [m.tier for m in ch.calls] == [
        Tier.BEFORE_48H, Tier.BEFORE_24H, Tier.OVERDUE, Tier.OVERDUE,
    ]


def test_overdue_goes_to_owner_and_pm(svc):
    svc, ch = svc
    make_item(svc)
    svc.dispatch(now=DUE)
    recipients = sorted(m.recipient for m in ch.calls if m.tier == Tier.OVERDUE)
    assert recipients == ["alice", "pm1"]


def test_only_highest_due_tier_after_gap(svc):
    """调度停了一个周末，恢复后只补当前最高档，不补过时的低档。"""
    svc, ch = svc
    make_item(svc)
    svc.dispatch(now=DUE + 60)  # 直接跳到到期之后
    assert [m.tier for m in ch.calls] == [Tier.OVERDUE, Tier.OVERDUE]
    assert sorted(m.recipient for m in ch.calls) == ["alice", "pm1"]


def test_lower_tier_pending_dropped_when_higher_due(svc):
    """低档还没送达，高档到点后低档不再发也不再重试。"""
    svc, ch = svc
    make_item(svc)
    ch.fail_ids.add(make_message_id("I1", "alice", Tier.BEFORE_48H, DUE))
    svc.dispatch(now=DUE - 48 * 3600)  # 48h 档发送失败，进入重试
    assert len(ch.calls) == 1
    svc.dispatch(now=DUE - 24 * 3600)  # 24h 档到点
    tiers = [m.tier for m in ch.calls]
    assert tiers == [Tier.BEFORE_48H, Tier.BEFORE_24H]
    assert svc.list_failed(now=DUE - 24 * 3600) == []


def test_same_tier_same_due_sent_once(svc):
    svc, ch = svc
    make_item(svc)
    svc.dispatch(now=DUE - 48 * 3600)
    svc.dispatch(now=DUE - 47 * 3600)
    svc.dispatch(now=DUE - 46 * 3600)
    assert len(ch.calls) == 1


def test_due_changed_away_and_back_not_resent(svc):
    """截止时间改走又改回原值，已收过的档不再发。"""
    svc, ch = svc
    make_item(svc)
    svc.dispatch(now=DUE - 48 * 3600)
    assert len(ch.calls) == 1
    new_due = DUE + 10 * 3600
    svc.upsert_item("I1", "RFI", new_due, "alice", "pm1", now=DUE - 47 * 3600)
    svc.upsert_item("I1", "RFI", DUE, "alice", "pm1", now=DUE - 46 * 3600)
    svc.dispatch(now=DUE - 45 * 3600)
    assert len(ch.calls) == 1  # 没有重发


def test_ball_transfer_new_owner_gets_current_tier(svc):
    """球权转给新的人，新的人马上收到当前已到期的那一档。"""
    svc, ch = svc
    make_item(svc)
    svc.dispatch(now=DUE - 48 * 3600)  # alice 收到 48h 档
    svc.upsert_item("I1", "RFI", DUE, "bob", "pm1", now=DUE - 47 * 3600)
    svc.dispatch(now=DUE - 47 * 3600)
    assert ch.calls[-1].recipient == "bob"
    assert ch.calls[-1].tier == Tier.BEFORE_48H
    svc.dispatch(now=DUE - 24 * 3600)
    assert ch.calls[-1].recipient == "bob"
    assert ch.calls[-1].tier == Tier.BEFORE_24H
    assert [m.recipient for m in ch.calls] == ["alice", "bob", "bob"]


def test_pending_retry_voided_on_transfer(svc):
    """等待重试的提醒，球权转走后作废，不再重试。"""
    svc, ch = svc
    make_item(svc)
    mid = make_message_id("I1", "alice", Tier.BEFORE_48H, DUE)
    ch.fail_ids.add(mid)
    svc.dispatch(now=DUE - 48 * 3600)  # 失败，1 分钟后重试
    svc.upsert_item("I1", "RFI", DUE, "bob", "pm1", now=DUE - 48 * 3600 + 10)
    svc.dispatch(now=DUE - 48 * 3600 + 60)  # 原重试时刻
    assert [m.recipient for m in ch.calls] == ["alice", "bob"]


def test_due_change_voids_pending(svc):
    svc, ch = svc
    make_item(svc)
    mid = make_message_id("I1", "alice", Tier.BEFORE_48H, DUE)
    ch.fail_ids.add(mid)
    svc.dispatch(now=DUE - 48 * 3600)
    new_due = DUE + 5 * 3600
    svc.upsert_item("I1", "RFI", new_due, "alice", "pm1", now=DUE - 48 * 3600 + 5)
    svc.dispatch(now=DUE - 48 * 3600 + 60)
    assert len(ch.calls) == 1  # 旧截止时间的提醒不再重试


def test_close_voids_everything(svc):
    svc, ch = svc
    make_item(svc)
    mid = make_message_id("I1", "alice", Tier.BEFORE_48H, DUE)
    ch.fail_ids.add(mid)
    svc.dispatch(now=DUE - 48 * 3600)
    svc.close_item("I1", now=DUE - 48 * 3600 + 5)
    svc.dispatch(now=DUE - 48 * 3600 + 60)
    svc.dispatch(now=DUE)
    assert len(ch.calls) == 1
    assert svc.list_failed(now=DUE) == []


def test_retry_backoff_and_failed_queue(svc):
    """失败后 1 分钟、2分钟重试，共 3 次，第 3 次失败进失败队列。"""
    svc, ch = svc
    make_item(svc)
    mid = make_message_id("I1", "alice", Tier.BEFORE_48H, DUE)
    ch.fail_ids.add(mid)
    t = DUE - 48 * 3600
    svc.dispatch(now=t)            # 第 1 次失败
    svc.dispatch(now=t + 59)       # 未到重试时间
    assert len(ch.calls) == 1
    svc.dispatch(now=t + 60)       # 第 2 次失败
    assert len(ch.calls) == 2
    svc.dispatch(now=t + 60 + 119)
    assert len(ch.calls) == 2
    svc.dispatch(now=t + 60 + 120)  # 第 3 次失败
    assert len(ch.calls) == 3
    failed = svc.list_failed(now=t + 60 + 120)
    assert [f.message_id for f in failed] == [mid]
    assert failed[0].attempts == 3
    svc.dispatch(now=t + 60 + 120 + 3600)  # 不再自动重试
    assert len(ch.calls) == 3


def test_failed_queue_drops_invalid(svc):
    """失败队列里只留仍然有效的提醒。"""
    svc, ch = svc
    make_item(svc)
    mid = make_message_id("I1", "alice", Tier.BEFORE_48H, DUE)
    ch.fail_ids.add(mid)
    t = DUE - 48 * 3600
    for dt in (0, 60, 180):
        svc.dispatch(now=t + dt)
    assert len(svc.list_failed(now=t + 180)) == 1
    svc.upsert_item("I1", "RFI", DUE + 3600, "alice", "pm1", now=t + 200)
    assert svc.list_failed(now=t + 200) == []


def test_pm_change_voids_old_pm_overdue(svc):
    """项目经理换人后，旧 PM 的未送达升级提醒作废，新 PM 收到。"""
    svc, ch = svc
    make_item(svc)
    mid = make_message_id("I1", "pm1", Tier.OVERDUE, DUE)
    ch.fail_ids.add(mid)
    svc.dispatch(now=DUE)  # alice 收到，pm1 失败待重试
    svc.upsert_item("I1", "RFI", DUE, "alice", "pm2", now=DUE + 10)
    svc.dispatch(now=DUE + 60)
    pms = [m.recipient for m in ch.calls if m.tier == Tier.OVERDUE and m.recipient != "alice"]
    assert pms == ["pm1", "pm2"]


def test_failed_but_actually_delivered_deduped_by_channel(svc):
    """失败的那次可能实际已送达：通道按编号去重，用户只看到一条。"""
    svc, ch = svc
    make_item(svc)
    mid = make_message_id("I1", "alice", Tier.BEFORE_48H, DUE)
    ch.fail_ids.add(mid)
    t = DUE - 48 * 3600
    svc.dispatch(now=t)        # 失败（假设实际已送达）
    ch.fail_ids.discard(mid)
    svc.dispatch(now=t + 60)   # 库重试，通道应收到同一编号
    assert ch.sent_ids == [mid, mid]
    assert len(set(ch.sent_ids)) == 1  # 通道按编号去重后用户只见一条


def test_state_persists_across_restarts(tmp_path):
    """状态存在 SQLite 文件里：重启服务后已送达的提醒不重发，失败队列还在。"""
    db = str(tmp_path / "persist.db")
    mid = make_message_id("I1", "alice", Tier.BEFORE_48H, DUE)

    ch1 = FakeChannel(fail_ids={mid})
    s1 = ReminderService(db, ch1)
    s1.upsert_item("I1", "RFI", DUE, "alice", "pm1", now=T0)
    t = DUE - 48 * 3600
    for dt in (0, 60, 180):
        s1.dispatch(now=t + dt)
    s1.upsert_item("I2", "RFI", DUE, "bob", "pm1", now=t + 180)
    s1.dispatch(now=t + 180)  # bob 的 48h 档送达
    s1.close()

    ch2 = FakeChannel()
    s2 = ReminderService(db, ch2)
    s2.dispatch(now=t + 240)
    assert ch2.calls == []  # 已送达的不重发，失败的不自动重试
    assert [f.message_id for f in s2.list_failed(now=t + 240)] == [mid]
    s2.close()
