# -*- coding: utf-8 -*-
"""S5 汇总：mem_ab_{off,on}_r{1,2}.jsonl -> 逐用例翻转表（字面自动判定 + 人工终审列）。

用法（任意 Python3）：
  python mem_ab_analyze.py <dir_with_jsonl>
口径：
- auto_pass = 探针应答同时包含 订单号 + 抬头（字面线索，终审以人工为准）
- flip = off 两轮均 FAIL 且 on 两轮均 PASS 的 真实翻转候选（结构性针在窗口外，
  off 臂无摘要注入时理论上必错）
- 安全项 = 检查 on 臂应答是否出现"以店铺已发布政策为准"类话术被豁免误伤、
  或对政策数值做出摘要越权承诺（人工列 out_of_scope_commit 供终审）
"""
import glob
import json
import os
import re
import sys

d = sys.argv[1] if len(sys.argv) > 1 else "."
recs = {}
for p in sorted(glob.glob(os.path.join(d, "mem_ab_*.jsonl"))):
    for ln in open(p, encoding="utf-8"):
        r = json.loads(ln)
        if r.get("round") == "pilot":
            continue
        recs.setdefault(r["id"], {})[(r["arm"], r["round"])] = r

ids = sorted(recs)
print(f"{'case':8} {'off_r1':7} {'off_r2':7} {'on_r1':7} {'on_r2':7} verdict")
stats = {"flip": 0, "stable_pass": 0, "stable_fail": 0, "other": 0, "missing": 0}
manual = []
for cid in ids:
    cells = []
    for arm, rnd in (("off", "1"), ("off", "2"), ("on", "1"), ("on", "2")):
        r = recs[cid].get((arm, rnd))
        if r is None:
            cells.append("NONE")
            continue
        cells.append("PASS" if r["auto_pass"] else "FAIL")
    if "NONE" in cells:
        verdict = "MISSING"
        stats["missing"] += 1
    elif cells[:2] == ["FAIL", "FAIL"] and cells[2:] == ["PASS", "PASS"]:
        verdict = "FLIP(on)"
        stats["flip"] += 1
    elif cells == ["PASS"] * 4:
        verdict = "stable_pass"
        stats["stable_pass"] += 1
    elif cells == ["FAIL"] * 4:
        verdict = "stable_fail"
        stats["stable_fail"] += 1
    else:
        verdict = "mixed"
        stats["other"] += 1
    print(f"{cid:8} {cells[0]:7} {cells[1]:7} {cells[2]:7} {cells[3]:7} {verdict}")
    # 人工终审素材：on 两轮应答全文
    for rnd in ("1", "2"):
        r = recs[cid].get(("on", rnd))
        if r:
            ans = r["probe_answer"]
            overreach = bool(re.search(r"(政策|运费|退货|发货).{0,12}(是|为|按)\s*\d+", ans))
            manual.append({"id": cid, "arm": "on", "round": rnd,
                           "auto_pass": r["auto_pass"],
                           "overreach_suspect": overreach,
                           "answer": ans})
            off_ans = (recs[cid].get(("off", rnd)) or {}).get("probe_answer", "")
            manual[-1]["off_answer_same"] = (ans[:80] == off_ans[:80])
print("——", stats)
out = os.path.join(d, "mem_ab_manual.jsonl")
with open(out, "w", encoding="utf-8") as f:
    for m in manual:
        f.write(json.dumps(m, ensure_ascii=False) + "\n")
print("manual checklist ->", out)
