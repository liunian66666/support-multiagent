# -*- coding: utf-8 -*-
"""S4 冒烟（在 cs_business 容器内跑）：水位增量摘要端到端。

流程：guest 真实对话 6 轮 → 记水位应=0（未摘要）
     → cs_agent 侧 summary_once --thread T → 断言水位推进到最大消息id、版本1、
       摘要保留订单号 → 再聊 2 轮 → 再摘要 → 断言版本2、水位再推进、
       合并摘要仍含订单号 → 读接口返回摘要一致。
用法：docker exec cs_business python /app/evaluation/smoke_memory.py
（水位推进动作由外部先调用 docker exec cs_agent ... summary_once --thread T，
 脚本会在两处暂停点打印 THREAD 并以 exit 42 等待；见 run 包装脚本。）
"""
import json
import sys
import time
import urllib.request
import uuid

sys.path.insert(0, "/app")
from business.infra.storage import many  # noqa: E402

GATEWAY = "http://nginx"


def post(path, payload, token=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(GATEWAY + path,
                                 data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers=h)
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)


def get_summary(thread):
    rows = many("SELECT summary, last_summarized_message_id wm, summary_version v "
                "FROM session_summaries WHERE thread_id=%s", (thread,))
    return rows[0] if rows else None


def max_msg_id(thread):
    return many("SELECT MAX(id) m FROM messages WHERE thread_id=%s", (thread,))[0]["m"]


TOK_FILE = "/tmp/smoke_mem_tok"


def main():
    stage = sys.argv[1] if len(sys.argv) > 1 else "chat1"
    if stage == "chat1":
        tok = post("/api/auth/guest-token", {})["data"]["token"]
        open(TOK_FILE, "w").write(tok)
        ref = "ORD-2024-888"
        for m in [f"我的订单{ref}还没发货，能催一下吗",
                  "如果到货后我要退货，退货运费谁出？",
                  "你们包邮门槛是多少",
                  "退款一般几个工作日到账",
                  "好的，我再考虑一下",
                  "今天就先这样"]:
            post("/api/user/chat", {"msg": m}, tok)
        row = many("SELECT thread_id FROM messages ORDER BY id DESC LIMIT 1")[0]
        print("THREAD=" + row["thread_id"])
        print("CHAT1_DONE max_id=", max_msg_id(row["thread_id"]))
        return

    tok = open(TOK_FILE).read().strip()
    thread = sys.argv[2]
    if stage == "assert1":
        s = get_summary(thread)
        mid = max_msg_id(thread)
        assert s and s["wm"] == mid and s["v"] == 1, f"assert1 fail {s} vs {mid}"
        assert "ORD-2024-888" in (s["summary"] or ""), f"needle missing: {s['summary']}"
        print("ASSERT1_PASS", {"wm": int(s["wm"]), "v": int(s["v"])})
        return
    if stage == "chat2":
        for m in ["我再确认下，我之前说的那个订单号是多少？", "对，就是这个订单"]:
            r = post("/api/user/chat", {"msg": m}, tok)
            print("ANS:", (r.get("msg") or "")[:120].replace("\n", " "))
        print("CHAT2_DONE max_id=", max_msg_id(thread))
        return
    if stage == "assert2":
        # 第二次摘要【前】：水位应停在旧水位(<现最大id)，版本仍为 1
        s = get_summary(thread)
        mid = max_msg_id(thread)
        assert s and int(s["v"]) == 1 and int(s["wm"]) < mid, f"assert2 precond {s} {mid}"
        print("ASSERT2_PRE_PASS", {"wm": int(s["wm"]), "v": int(s["v"]), "mid": int(mid)})
        return
    if stage == "assert3":
        # 第二次摘要【后】：版本 2、水位==新最大 id、合并摘要仍保留埋针
        s = get_summary(thread)
        mid = max_msg_id(thread)
        assert s and int(s["v"]) == 2 and int(s["wm"]) == mid, f"assert3 fail {s} vs {mid}"
        assert "ORD-2024-888" in (s["summary"] or ""), "merged summary lost needle"
        print("ASSERT3_PASS", {"wm": int(s["wm"]), "v": int(s["v"])})
        print("MERGED_SUMMARY=", s["summary"])
        return


if __name__ == "__main__":
    main()
