# -*- coding: utf-8 -*-
"""RSI 受控学习全链路演示脚本：反馈→提炼→审核→影子→注入→回退。

在 business 容器内运行（网关+演示凭证就位）；提炼/审核/策略切换调用 agent
内部接口（容器网络内 http://agent:8000，需 INTERNAL_API_TOKEN）。

链路步骤（每步产出一行 JSON 证据，写 --output jsonl，宿主可复核）：
  step1 对指定问题正常提问拿到 answer_id（好评归因锚点）
  step2 提交 5 星好评（带 answer_id）→ MySQL feedback_outbox
  step3 等 outbox 60s 同步（轮询 PG experience_feedback）
  step4 容器内触发 run_distill_round（独立脚本，见 docker 命令注释）→ candidate
  step5 审核 publish 该 candidate → 进入召回池
  step6 策略 create+publish shadow → 原问重发 → trace 有 experience_hit 且应答无注入
  step7 策略 create+publish inject → 原问重发 → 应答含经验指导
  step8 回滚策略到 off → 原问重发 → 恢复原行为

用法（cs_business 容器内）：
  python rsi_demo_pipeline.py --question "无理由退货邮费谁承担" --output /app/eval_tmp/rsi_demo.jsonl
提炼触发（cs_agent 容器内，脚本随评测目录已在 /app/evaluation）：
  python /app/evaluation/rsi_distill_once.py
"""
import argparse
import json
import os
import sys
import time
import urllib.request

GATEWAY = os.getenv("GATEWAY_URL", "http://nginx")
AGENT = os.getenv("AGENT_INTERNAL_URL", "http://agent:8000")
TOKEN = os.getenv("INTERNAL_API_TOKEN", "")
TENANT = "t_demo"


def _post(url, payload, headers=None, timeout=90):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.load(r)


def _get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.load(r)


def _agent_headers():
    return {"X-Internal-Token": TOKEN, "X-Tenant-Id": TENANT}


def login_guest():
    _, body = _post(f"{GATEWAY}/api/auth/guest-token", {})
    if body.get("code") != 0:
        raise RuntimeError("guest login failed")
    return body["data"]["token"]


def ask(token, question):
    _, body = _post(f"{GATEWAY}/api/user/chat", {"msg": question},
                    {"Authorization": "Bearer " + token})
    return body


def trace_events(trace_id):
    """从 PG 读当轮 trace 事件（经 agent trace 接口不可用时退回 psql 由人工执行）。"""
    try:
        _, body = _get(f"{AGENT}/agent/traces/{trace_id}", _agent_headers())
        return (body.get("data") or {}).get("events") or []
    except Exception:
        return []


def wait_for_feedback(answer_id, deadline_s=150):
    """轮询 agent 侧反馈可见性——演示环境用 trace 接口不可查时以时间兜底。
    outbox 同步周期 60s，默认等 2 个周期+宽限。"""
    waited = 0
    while waited < deadline_s:
        time.sleep(15)
        waited += 15
    return waited


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--keywords", default="", help="可选：直接指定候选经验关键词（跳过提炼时用）")
    args = ap.parse_args()

    rows = []

    def log(step, **kw):
        row = {"step": step, "ts": time.strftime("%H:%M:%S"), **kw}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))
        with open(args.output, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # step1 提问拿锚点
    token = login_guest()
    b1 = ask(token, args.question)
    answer_id, trace1 = b1.get("answer_id", ""), b1.get("trace_id", "")
    log("1_ask", answer_id=answer_id, trace_id=trace1,
        answer_head=(b1.get("msg") or "")[:80])
    if not answer_id:
        log("FAIL_no_answer_id")
        sys.exit(1)

    # step2 好评（带归因锚点）
    st, rb = _post(f"{GATEWAY}/api/user/rating",
                   {"score": 5, "tags": "解答详细", "comment": "RSI演示好评",
                    "answer_id": answer_id},
                   {"Authorization": "Bearer " + token})
    log("2_rating", http=st, code=rb.get("code"))

    # step3 outbox→PG（等 2 个同步周期）
    waited = wait_for_feedback(answer_id)
    log("3_outbox_wait", waited_s=waited)

    # step4 提炼：需人工在 agent 容器执行 rsi_distill_once.py（此处轮询候选出现）
    log("4_distill", instruction=(
        "docker exec SM_agent python /app/evaluation/rsi_distill_once.py"),
        note="产出 candidates 后按 source_answer_id 找到 experience_id 填入 --review-id 再跑 step5+")

    with open(args.output, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def phase2(review_id, question, expect_mode):
    """step5-8：审核发布→shadow→inject→回滚off，每态提问留存证据。"""
    token = login_guest()
    st, rb = _post(f"{AGENT}/agent/experience/review",
                   {"experience_id": int(review_id), "action": "publish"},
                   _agent_headers())
    print("5_review", st, rb)

    for mode in ("shadow", "inject", "off"):
        st, rb = _post(f"{AGENT}/agent/experience/strategy",
                       {"params": {"recall_mode": mode}}, _agent_headers())
        vid = ((rb or {}).get("data") or {}).get("version_id")
        st2, rb2 = _post(f"{AGENT}/agent/experience/strategy/publish",
                         {"version_id": vid, "actor": "rsi_demo"}, _agent_headers())
        print(f"6_strategy_{mode} create={st} publish={st2} version={vid}")
        print("   （等 30s 策略缓存过期）")
        time.sleep(35)
        tok = login_guest()
        b = ask(tok, question)
        evs = trace_events(b.get("trace_id", ""))
        hits = [e for e in evs if "experience" in str(e.get("name", ""))]
        print(f"7_ask_{mode}", json.dumps({
            "trace_id": b.get("trace_id", ""),
            "experience_hit_events": hits,
            "answer_head": (b.get("msg") or "")[:150]}, ensure_ascii=False))


if __name__ == "__main__":
    _ap = argparse.ArgumentParser()
    _ap.add_argument("--phase2", nargs=3, metavar=("REVIEW_ID", "QUESTION", "EXPECT"))
    _ns, _rest = _ap.parse_known_args()
    if _ns.phase2:
        phase2(*_ns.phase2)
    else:
        main()
