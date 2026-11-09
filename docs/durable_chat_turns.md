# 客户聊天轮次持久化

客户 WebSocket 收到消息后，以 `request_id` 作为轮次 ID，把用户消息与 `chat_turns` 任务放在同一个 MySQL 事务中提交。连接断开不会取消任务。`business-maintenance` 领取任务后，使用同一个轮次 ID 调用 Agent；回答和完成状态同事务提交，之后才尝试向在线页面推送。推送失败时，重连仍能从 MySQL 恢复。

## 升级与回退

1. 先备份现有 MySQL 数据库，再执行 `deploy/init/mysql/migrate_chat_turns.sql`。新库由 `init.sql` 自动建表；旧库只增加新表，不修改原有消息。
2. 部署 `business`、`business-maintenance` 和 `nginx` 的新镜像。保留旧会话表和原有 Agent；本次不迁移或重放历史消息。
3. 回退代码前先停止维护进程。未完成的 `chat_turns` 留在 MySQL，重新部署本版本后可继续领取；旧页面能读到已提交的消息，但不会显示轮次状态。

`business-maintenance` 可通过 `CHAT_TURN_POLL_SECONDS` 调整轮询间隔，默认 1 秒。Worker 租约为 120 秒，Agent 请求超时为 65 秒；Worker 中断后，租约到期再领取，最多尝试 3 次。失败状态留在表中，由客户页面发 `retry` 事件显式重试。请保持至少一个维护进程运行。

客户端对同一条提问使用稳定 UUID `request_id`；断线重发仍用原 ID。服务端对 ID、客户身份、问题文本和消息类型做冲突检查。同一会话中较早的未完成任务阻挡后续任务；租约令牌阻止旧 Worker 在重启或超时后写入重复回答。客户消息和轮次查询均由服务端会话身份限定租户、客户、会话。

## 并发与容量

受理是异步的（WS 立刻回 `queued`，消息与轮次同事务落库），但**执行是有限的**：吞吐上限
= Worker 数 ÷ 单轮处理时长。单 Worker 时，10 个客户同时提问，第 10 个要等前面 9 轮跑完。

`business-maintenance` 现在按 `CHAT_TURN_WORKERS`（默认 4，上限 16）起线程。四个约束一起
生效，缺一个就会乱：

| 项 | 机制 | 位置 |
|---|---|---|
| 不同会话并行 | 每个线程各自 `claim_next()`，`FOR UPDATE SKIP LOCKED` | `business/chat/turns.py` |
| 同一会话串行 | `NOT EXISTS (older.id < t.id AND older.status IN ('queued','running'))` | 同上。方向是**保守**的：看不到未提交的"完成"，只会更早地挡住后一轮，不会放行 |
| 长任务不被误回收 | 处理期间每 `CHAT_TURN_HEARTBEAT_SECONDS`（默认 30）续租一次 | `worker.LeaseKeeper` + `turns.heartbeat` |
| 恢复扫描不空转 | Redis 短租约选主，只有一个线程执行 `recover_due()` | `worker._recovery_pass` |

几个环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `CHAT_TURN_WORKERS` | `4` | 维护进程内的聊天 Worker 数，上限 16 |
| `CHAT_TURN_LEASE_SECONDS` | `120` | 租约时长；有心跳后不需要给太大 |
| `CHAT_TURN_HEARTBEAT_SECONDS` | `30` | 续租间隔，必须明显小于租约；`0` 关闭心跳 |
| `CHAT_TURN_RECLAIM_SECONDS` | `10` | 僵尸租约扫描降频（多线程共用进程时只让一个线程扫） |
| `CHAT_TURN_POLL_SECONDS` | `1` | 空转轮询间隔（实际 sleep 带 ±20% 抖动） |
| `MYSQL_POOL_SIZE` | `8` | 连接池上限；`0` 退回"每次新建" |
| `MYSQL_POOL_TIMEOUT` | `5` | 池满时的等待秒数，超时后临时新建（软上限，不是硬闸） |

**并发数不是越大越好。** 真正的上限在 Agent 侧的模型供应商配额：线程开过头，只是把
"在 MySQL 里排队"换成"在 provider 的 429 里排队"。按 `并发 ≈ 到达率 × 平均处理时长`
估：50 活跃客户、5 分钟一问 → 2 个；500 活跃 → 8 个 + 横向扩容器。

## 观测

`/metrics` 新增 5 行（采集失败时**不输出**，而不是输出 0）：

| 指标 | 含义 |
|---|---|
| `cs_chat_turns_queued` | 等待中的轮次 |
| `cs_chat_turns_running` | 正被 Worker 持有着的轮次 |
| `cs_chat_turns_stale_running` | 租约已过期但还标着 running 的轮次（应该接近 0） |
| `cs_chat_turns_retried` | 排队/执行中、被领取过多次的轮次（租约给短了的信号） |
| `cs_chat_turn_oldest_queued_seconds` | **最老的排队时长 = 客户实际等了多久，唯一值得告警的一行** |

连接池另有 `cs_mysql_pool_open` / `cs_mysql_pool_free` / `cs_mysql_pool_overflow_total`。
溢出计数持续上涨说明 `MYSQL_POOL_SIZE` 小于实际并发。

Worker 也会每 60 秒打一行 `chat queue worker=N queued= running= stale= retried= oldest_queued=`，
队列为空时不打。


## 验证

离线：`python -m pytest python_business/tests`。

真实 MySQL：`docker compose run --rm --no-deps -e CHAT_TURN_DB_TEST=1 business python -m unittest discover -s tests -p test_durable_turns.py -v`。该测试创建独立的测试会话并清理自己的轮次和消息，不使用真实客户订单。

浏览器：以演示买家登录，在 `/chat/pc.html?customer_id=buyer1` 发送订单查询后立即关闭标签页；重新打开同一地址，应显示一条用户消息、一条回答。核对 `chat_turns.status=completed`，`user_message_id`、`answer_message_id` 均有值。处理期间重启维护进程后，待租约到期应重领或明确失败。

并发验收（改 `CHAT_TURN_WORKERS` 之前先留基线）：

1. 另开 K 个终端并行跑 `python verify_realtime_live.py`，制造 K 个会话的流量；
2. 同时跑 `python verify_chat_queue.py --seconds 60`，它只做 SELECT，输出排队时长
   P50/P95、峰值深度、过期租约、重复领取率、同会话乱序；
3. 先用 `CHAT_TURN_WORKERS=1` 跑一轮，再改成 4 重跑同样流量。`oldest_queued` 的 P95 应
   近似按倍数下降，直到撞上模型配额；`同会话乱序` 必须恒为 0。

`python verify_chat_queue.py` 在有积压、有乱序、有过期租约时退出码为 1，可以直接接进流水线。
