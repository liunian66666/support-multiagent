# -*- coding: utf-8 -*-
"""S6 回归对照：有记忆 checklist vs 冻结 off 基线（ho_off_r4），逐题零回退判定。

用法：python ho_regression_diff.py <baseline_checklist> <new_checklist_r1> [new_checklist_r2]
输出：字面 pass 数、翻正/回退逐题清单。回退=基线过而新挂（字面线索，人工终审）。
"""
import json
import sys


def load(p):
    return {r["id"]: r for r in (json.loads(l) for l in open(p, encoding="utf-8"))}


base = load(sys.argv[1])
news = [load(p) for p in sys.argv[2:]]
bp = sum(1 for r in base.values() if r["literal_pass"])
print(f"baseline: {bp}/{len(base)}")
for i, nf in enumerate(news, 1):
    npass = sum(1 for r in nf.values() if r["literal_pass"])
    regress = [k for k in base if base[k]["literal_pass"] and not nf[k]["literal_pass"]]
    improve = [k for k in base if not base[k]["literal_pass"] and nf[k]["literal_pass"]]
    print(f"new_r{i}: {npass}/{len(nf)}  improve={improve} regress={regress}")
    for k in regress:
        print(f"  REGRESS {k} miss={nf[k]['missing']} forbid={nf[k]['forbidden_hit']}")
        print(f"    ans: {nf[k]['answer_head'][:80]}")
