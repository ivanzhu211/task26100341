# reminder-lib：RFI / 报审截止催办提醒库

纯 Python + SQLite 的催办提醒模块，供上层项目服务与定时任务直接调用，无接口层。
状态全部落在本地 SQLite 文件中，进程重启不丢。

## 业务规则

- 每条事项（RFI / 报审）有截止时间与当前球权方，提醒发给**发送那一刻**的球权方。
- 三档提醒：截止前 48 小时、截止前 24 小时（只发球权方）、到期升级（同时发球权方与项目经理）。
  到点判断含正好相等（`now >= 触发时刻`）。
- 一个收件人对一个事项只收**当前已到点的最高一档**；未送达的低档不再发、不再重试；
  调度停机恢复后也只补当前最高档。
- 同一收件人 + 同一事项 + 同一截止时间 + 同一档，最多送达一次（截止时间改走又改回也不重发）；
  球权转走后，新球权方立即收到当前已到点的那一档。
- 改截止时间 / 转球权 / 换项目经理 / 关闭事项后，与新情况对不上的未送达提醒（含等待重试的）全部作废。
- 每条提醒带确定性消息编号（`事项|收件人|档位|截止时间`），通道按编号去重；
  通道调用失败的那次可能实际已送达，用户仍只看到一条。
- 投递失败指数退避重试：隔 1 分钟、隔 2 分钟，共自动尝试 3 次；
  第 3 次仍失败进失败队列，队列里只留仍然有效的提醒。

## 用法

```python
from reminder_lib import ReminderService

# channel：调用方实现的投递通道，send(message) 抛异常即视为本次失败
service = ReminderService("reminders.db", channel)

# 项目服务：事项新建 / 改截止 / 转球权 / 换项目经理都走 upsert_item
service.upsert_item("RFI-001", "RFI", due_at, ball_owner="alice", project_manager="pm1")
service.close_item("RFI-001")          # 事项关闭

# 定时任务：每分钟调用一次
service.dispatch()

# 人工查看失败队列（只含仍然有效的提醒）
for failed in service.list_failed():
    print(failed.message_id, failed.recipient, failed.attempts)
```

所有时间参数接受 epoch 秒（`int`）或 `datetime`；不传 `now` 时取当前系统时间。

## 结构

- `reminder_lib/models.py` — 档位、触发时刻、消息与失败队列条目类型
- `reminder_lib/store.py` — SQLite 存取层（事项 / 提醒 / 送达历史三张表）
- `reminder_lib/service.py` — 变更作废、到点生成、发送与退避重试

## 测试

```bash
python3 -m pytest tests/ -q
```

- `tests/test_reminders.py` — 每条业务规则的确定性单元测试
- `tests/test_randomized.py` — 随机对拍：穿插改截止 / 转球权 / 换 PM / 关闭 / 通道失败 /
  调度停机，与独立推算模型逐拍比对发送序列、失败队列与最终用户可见集合（20 个种子）
