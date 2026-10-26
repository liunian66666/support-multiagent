# -*- coding: utf-8 -*-
"""S5 长对话记忆 on/off 双臂对照采集（分段式，宿主驱动）。

每段对话：1-3 轮埋 3 针（订单号/金额/抬头）→ 4-11 轮 8 句政策闲聊填充
（把埋针推出 12 条滑动窗口）→（外部触发本会话摘要，两臂都触发）→
等读路径 15s 缓存过期 → 探针一次成型（只发一次，绝不复读埋针）。

臂间唯一差异 = Redis cs:memory:inject（驱动脚本在 chat 前 set 好）。
分段（都在 cs_agent 容器内跑，token 落 /tmp/memab_tok_<case>）：
  chat <case>            埋针+填充 11 轮，打印 SESS=&lt;session_id&gt;
  probe <case> &lt;thread&gt;  等 16s 后发探针，打印 JSON 结果行
离线自校验：--dry
"""
import base64
import json
import os
import sys
import time
import urllib.request

GATEWAY = os.getenv("GATEWAY_URL", "http://nginx")
PACING = float(os.getenv("EVAL_PACING", "1.2"))
CACHE_WAIT = 16

COMPANIES = ["星辰科技", "云帆贸易", "澜山食品", "墨白文化", "启元电子",
             "禾风家居", "川流物流", "柏舟服饰", "朗越传媒", "叙白医药"]
FILLERS = ["你们包邮门槛是多少", "退款一般几个工作日到账",
           "下单后多久能发货", "支持七天无理由退货吗",
           "会员积分怎么获得", "客服工作时间是几点到几点",
           "尺码不合适可以换货吗", "优惠券能和满减一起用吗"]


def cases():
    out = []
    for i in range(10):
        ref = f"ORD-2024-7{i+1:02d}"
        amount = 259 + 37 * i
        out.append({
            "id": f"mem-{i+1:02d}",
            "needles": {"ref": ref, "amount": amount, "company": COMPANIES[i]},
            "turns": [
                f"我的订单{ref}下单好几天了一直没发货，能帮我催一下吗",
                f"这一单我当时实付了{amount}元，到货之后如果我要退货，退货运费是谁出的？",
                f"对了，那笔单子的发票抬头我填的是{COMPANIES[i]}，别给我开错了",
                *FILLERS,
            ],
            "probe": ("考考你，我最开始提到的那笔单子，编号是什么来着？"
                      "发票抬头用的是哪家？"),
            "expect": [ref, COMPANIES[i]],
        })
    return out


def case_by_id(cid):
    for c in cases():
        if c["id"] == cid:
            return c
    raise SystemExit(f"unknown case {cid}")


def post(path, payload, token=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(GATEWAY + path,
                                 data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers=h)
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)


def tok_file(cid):
    return f"/tmp/memab_tok_{cid}"


def stage_chat(cid, arm, rnd):
    case = case_by_id(cid)
    part = post("/api/auth/guest-token", {})["data"]
    tok, thread = part["token"], part["thread_id"]
    open(tok_file(cid), "w").write(tok)
    for msg in case["turns"]:
        post("/api/user/chat", {"msg": msg}, tok)
        time.sleep(PACING)
    print(f"ARM={arm} ROUND={rnd}")
    print("THREAD=" + thread)
    print("CHAT_DONE " + cid)


def stage_probe(cid, thread, arm, rnd, outfile=None):
    case = case_by_id(cid)
    tok = open(tok_file(cid)).read().strip()
    time.sleep(CACHE_WAIT)  # 读路径摘要缓存 TTL 15s
    t0 = time.perf_counter()
    body = post("/api/user/chat", {"msg": case["probe"]}, tok)
    ans = body.get("msg", "")
    rec = {"id": cid, "arm": arm, "round": rnd, "thread_id": thread,
           "needles": case["needles"], "expect": case["expect"],
           "probe_answer": ans,
           "probe_trace": body.get("trace_id", ""),
           "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
           "auto_pass": all(k in ans for k in case["expect"])}
    line = json.dumps(rec, ensure_ascii=False)
    if outfile:
        with open(outfile, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    print("RESULT_JSON=" + line)


def stage_run(cid, arm, rnd, outfile):
    """单用例端到端：chat→触发摘要(校验+重试)→probe（一次 docker exec 内完成）。"""
    import subprocess
    r1 = subprocess.run(["python", "/app/evaluation/memory_ab_live.py", "chat", cid],
                        capture_output=True, text=True)
    thread = ""
    for ln in (r1.stdout or "").splitlines():
        if ln.startswith("THREAD="):
            thread = ln.split("=", 1)[1]
    if not thread:
        print("RUN_FAIL chat", cid, (r1.stderr or "")[-300:], flush=True)
        return
    env = {**os.environ, "PYTHONPATH": "/app",
           "SESSION_SUMMARY_IDLE_MINUTES": "0", "SESSION_SUMMARY_LOCK_TTL": "5"}
    ok_sum = False
    for attempt in range(3):
        rs = subprocess.run(
            ["python", "/app/evaluation/summary_once.py", "--thread", thread],
            capture_output=True, text=True, env=env)
        if "summarized=1/" in (rs.stdout or ""):
            ok_sum = True
            break
        print(f"SUM_RETRY {cid} attempt{attempt} "
              f"out={(rs.stdout or '').strip()[-120:]} "
              f"err={(rs.stderr or '').strip()[-200:]}", flush=True)
        time.sleep(5)
    r2 = subprocess.run(
        ["python", "/app/evaluation/memory_ab_live.py", "probe",
         cid, thread, outfile], capture_output=True, text=True)
    ok = '"auto_pass": true' in (r2.stdout or "")
    print(f"RUN_DONE {cid} thread={thread[:20]}.. summary={ok_sum} pass={ok}",
          flush=True)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--dry"]:
        for c in cases():
            assert len(c["turns"]) == 11 and c["probe"]
            print(c["id"], c["needles"], "probe_once")
        raise SystemExit(0)
    stage = args[0]
    ARM = os.getenv("ARM", "?")
    ROUND = os.getenv("ROUND", "?")
    if stage == "chat":
        stage_chat(args[1], ARM, ROUND)
    elif stage == "probe":
        stage_probe(args[1], args[2], ARM, ROUND,
                    args[3] if len(args) > 3 else None)
    elif stage == "run":
        # run <start> <stop> <outfile>：闭区间用例序号
        for n in range(int(args[1]), int(args[2]) + 1):
            stage_run(f"mem-{n:02d}", ARM, ROUND, args[3])
    else:
        raise SystemExit("usage: chat <case> | probe <case> <thread> [out] "
                         "| run <start> <stop> <out> | --dry")
