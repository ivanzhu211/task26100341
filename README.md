# task26100341

RFI / 报审截止催办提醒模块。纯 Python 库，状态存本地 SQLite 文件，
由上层项目服务和定时任务直接调用，无接口层、无外部服务依赖。

## 结构

- `reminder_lib/models.py` — 档位（48h / 24h / 到期升级）、状态、消息编号等类型定义
- `reminder_lib/store.py` — SQLite 持久化（事项表 + 提醒表）
- `reminder_lib/service.py` — `ReminderService`：事项变更、每分钟派发、失败队列
- `tests/test_reminders.py` — 业务规则单元测试
- `tests/test_randomized.py` — 验收测试：随机事件流对照独立参考模型

## 用法

```python
from reminder_lib import ReminderService

# channel(message_id, recipient, payload) -> 成功返回真值，失败返回 False 或抛异常
svc = ReminderService("reminders.db", channel)

# 项目服务在事项创建/改截止时间/转球权/换项目经理时调用（同一条upsert）
svc.upsert_item("RFI-001", due_at=1735689600.0, ball_owner="alice", project_manager="pm1")
svc.close_item("RFI-001")          # 事项关闭

# 定时任务每分钟调用一次
svc.dispatch(now=time.time())

# 人工查看失败队列（只含仍然有效的提醒）
for r in svc.failed_queue():
    print(r.message_id, r.recipient, r.attempts)
```

## 规则要点

- 提醒发给发送那一刻的球权方；到期升级档同时发项目经理。到点含正好相等。
- 一个收件人对一个事项只收当前已到点的最高一档；低档未送达即作废，不补发。
- 消息编号由（事项, 截止时间, 档位, 收件人）确定生成，通道按编号去重；
  截止时间改走又改回，已收过的不重发；未送达被作废的重新有效时会复活补发。
- 改截止时间 / 转球权 / 换项目经理 / 关闭后，对不上的未送达提醒（含待重试、
  失败队列）全部作废；新球权方在下一次派发立即收到当前到点档。
- 投递失败指数退避：1 分钟、2 分钟，共自动尝试 3 次，第 3 次失败进失败队列。

## 测试

```bash
python3 -m pytest tests/ -q
```
