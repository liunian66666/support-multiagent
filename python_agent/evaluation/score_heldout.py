# -*- coding: utf-8 -*-
"""留出集逐题预检：字面分仅作线索，输出人工判定清单（--veto 一票否决项）。

用法：python score_heldout.py gold.jsonl answers.jsonl [--veto ho-c10,ho-c11]
退出码：0=无回退线索 1=存在字面失败 2=一票否决项命中 forbidden/空答
"""
import argparse
import json
import re
import sys


def norm(s: str) -> str:
    return re.sub(r"[\s*_`>]", "", str(s))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gold", help="gold jsonl (含 required/forbidden/category)")
    ap.add_argument("answers", help="采集答案 jsonl (含 id/answer)")
    ap.add_argument("--veto", default="", help="逗号分隔：一票否决题id")
    ap.add_argument("--output", default="", help="可选：清单写出路径(jsonl)")
    args = ap.parse_args()

    gold = [json.loads(l) for l in open(args.gold, encoding="utf-8") if l.strip()]
    ans = {json.loads(l)["id"]: json.loads(l) for l in
           open(args.answers, encoding="utf-8") if l.strip()}
    veto_ids = {v.strip() for v in args.veto.split(",") if v.strip()}

    rows, literal_fail, veto_hit = [], 0, []
    for g in gold:
        a = ans.get(g["id"], {})
        text = norm(a.get("answer", ""))
        missing = [k for k in g.get("required", []) if norm(k) not in text]
        forb = [f for f in g.get("forbidden", []) if norm(f) in text]
        ok = bool(a.get("answer")) and not missing and not forb
        if not ok:
            literal_fail += 1
        if g["id"] in veto_ids and (forb or not a.get("answer")):
            veto_hit.append(g["id"])
        rows.append({"id": g["id"], "category": g.get("category", ""),
                     "literal_pass": ok, "missing": missing, "forbidden_hit": forb,
                     "answer_head": (a.get("answer") or "")[:120],
                     "trace_id": a.get("trace_id", "")})
    for r in rows:
        flag = "OK " if r["literal_pass"] else "CHK"
        extra = (f" miss={','.join(r['missing'])}" if r["missing"] else "") + \
                (f" FORBID={','.join(r['forbidden_hit'])}" if r["forbidden_hit"] else "")
        print(f"{flag} {r['id']:12} {r['category']:12}{extra} | {r['answer_head'][:60]}")
    print(f"—— 字面通过 {len(rows)-literal_fail}/{len(rows)}；"
          f"待人工判定 {literal_fail}；一票否决命中 {len(veto_hit)} {veto_hit and veto_hit or ''}")
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    if veto_hit:
        sys.exit(2)
    sys.exit(1 if literal_fail else 0)


if __name__ == "__main__":
    main()
