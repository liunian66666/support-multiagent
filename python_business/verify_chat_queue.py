"""只读观测聊天队列：不写任何一行数据，用来对比 ``CHAT_TURN_WORKERS=1`` 与 ``=4``。

为什么单独写一个脚本：调 Worker 数之前必须有一份"客户实际等了多久"的基线，否则只是把
`4` 当成感觉合理的数字。这里只做 SELECT，采样 ``chat_turns``，跑完打印吞吐、等待时长和
两条不变式（同会话答复顺序、单轮重复领取率）。

用法：

    # 在 business 容器内（或任何能连上同一个 MySQL 的地方）
    python verify_chat_queue.py --seconds 60 --threshold 30

压测流量的产生方式：另开 K 个终端并行跑 ``python verify_realtime_live.py``（每个终端
是一个独立访客会话）。先跑一轮 ``CHAT_TURN_WORKERS=1`` 记录基线，再改成 4 重跑同样的
流量，比较两次的 ``oldest_queued`` P95 —— 它应该近似按倍数下降，直到撞到模型配额。

退出码：``oldest_queued`` 超过 ``--threshold`` 或有同会话乱序时返回 1。
"""

import argparse
import sys
import time

from business.chat import turns


ORDER_VIOLATIONS = (
    "SELECT COUNT(*) AS bad FROM chat_turns t "
    "JOIN chat_turns u ON u.tenant_id = t.tenant_id AND u.thread_id = t.thread_id "
    "AND u.id < t.id "
    "WHERE t.answer_message_id IS NOT NULL AND u.answer_message_id IS NOT NULL "
    "AND u.status = 'completed' AND t.status = 'completed' "
    "AND u.answer_message_id > t.answer_message_id"
)


def percentile(values, ratio):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[index]


def sample(seconds, interval):
    samples = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        started = time.monotonic()
        samples.append(turns.queue_stats())
        elapsed = time.monotonic() - started
        time.sleep(max(0.0, interval - elapsed))
    return samples


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读观测聊天队列")
    parser.add_argument("--seconds", type=float, default=60.0, help="采样时长")
    parser.add_argument("--interval", type=float, default=1.0, help="采样间隔")
    parser.add_argument("--threshold", type=float, default=30.0,
                        help="最老排队时长告警线（秒）")
    parser.add_argument("--window", type=int, default=10,
                        help="重复领取率的统计窗口（分钟）")
    args = parser.parse_args(argv)

    print("采样 %.0f 秒（每秒一次 SELECT，不写库）…" % args.seconds)
    samples = sample(args.seconds, args.interval)
    if not samples:
        print("没有采到样本")
        return 1

    ages = [item["oldest_queued_seconds"] for item in samples]
    depth = [item["queued"] for item in samples]
    running = [item["running"] for item in samples]
    stale = max(item["stale_running"] for item in samples)

    from business.infra.storage import one

    violations = int((one(ORDER_VIOLATIONS) or {}).get("bad") or 0)
    retried = int((one(
        "SELECT COUNT(*) AS n FROM chat_turns "
        "WHERE attempt_count > 1 AND created_at >= NOW() - INTERVAL %s MINUTE",
        (args.window,),
    ) or {}).get("n") or 0)
    finished = int((one(
        "SELECT COUNT(*) AS n FROM chat_turns "
        "WHERE status = 'completed' AND created_at >= NOW() - INTERVAL %s MINUTE",
        (args.window,),
    ) or {}).get("n") or 0)

    print("")
    print("窗口内完成轮次      : %d" % finished)
    print("排队深度  峰值/均值 : %d / %.1f" % (max(depth), sum(depth) / len(depth)))
    print("执行中    峰值/均值 : %d / %.1f" % (max(running), sum(running) / len(running)))
    print("最老排队  P50/P95/最大 : %.1fs / %.1fs / %.1fs"
          % (percentile(ages, 0.5), percentile(ages, 0.95), max(ages)))
    print("过期租约  最大值    : %d（应接近 0）" % stale)
    print("重复领取  %d 分钟    : %d 轮（占完成数 %.1f%%）"
          % (args.window, retried, (100.0 * retried / finished) if finished else 0.0))
    print("同会话乱序          : %d（必须为 0）" % violations)

    failed = False
    if max(ages) > args.threshold:
        print("[FAIL] 最老排队 %.1fs 超过阈值 %.1fs：需要加 Worker 或查 Agent 耗时"
              % (max(ages), args.threshold))
        failed = True
    if violations:
        print("[FAIL] 同会话出现乱序答复：串行约束被打破了（检查隔离级别与 NOT EXISTS 条件）")
        failed = True
    if stale:
        print("[FAIL] 有过期租约仍标着 running：心跳没生效或 Worker 数超过数据库承受能力")
        failed = True
    if not failed:
        print("[OK] 队列与时序都正常")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
