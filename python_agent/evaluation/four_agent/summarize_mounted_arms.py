"""逐题差异表：把两份挂载路径探针（control / patched）按 case 对齐。

存在的理由：`probe_mounted_graph.py` 与 `probe_mounted_guards.py` 各自打印自己的 PASS/FINDING，
但"补了 §5e-M2 之后 M1–M11 有没有被搞坏"只能靠两份原始数据对齐着看。这里只做一件事：
按 `case` 名字配对，给出 fix（FINDING→PASS）、REG（PASS→FINDING）、both、gap（只在一边出现）、
race（M10/M11 这种每边跑 5 次的竞态项：翻脸不算回退，只把两边的丢失次数并排印出来）。

REG 和 gap 都会让脚本返回非 0——和 `heldout_compare.py` 一样的口径：
**少了一道检查也算回退**，因为那通常意味着补丁让探针半路崩了，而不是"检查通过了"。

用法：
    python python_agent/evaluation/four_agent/summarize_mounted_arms.py <control.jsonl> <patched.jsonl> [--strict-gaps]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load(path: Path) -> dict[str, dict]:
    """jsonl 的第一行是 summary（含整份 rows），后面每行一条 case；都吃掉，按 case 去重。"""
    rows: dict[str, dict] = {}
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        for row in ([obj] if "case" in obj else obj.get("rows", [])):
            if isinstance(row, dict) and "case" in row:
                rows[row["case"]] = row
    return rows


def verdict(row: dict | None) -> str:
    if row is None:
        return "—"
    return "PASS" if row.get("ok") else "FIND"


# 天生带竞态的 case：M10/M11 每边跑 5 次"两个实例共写同一线程"，丢几次是随机的。
# 把它们当确定性判定来算回退会造出**假回退**——2026-10-28 那一轮就是这样：
# 对照组 5 次恰好都没丢（PASS），补丁组丢了 2 次（FIND），而补丁根本没碰这条路径。
# 所以翻脸不计回退，但两边的 `lost_turn_trials` 必须并排印出来：竞态的用"次数"比，不用"旗标"比
# （陷阱 #6 的同一件事，这次出现在差异闸门里）。
RACE_CASES = {"M10_cross_instance_write_mounted", "M11_cross_instance_write_agent_v2"}


def note_of(row: dict | None) -> str:
    if row is None:
        return ""
    note = str(row.get("note", ""))
    return note if len(note) <= 150 else note[:147] + "..."


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("control")
    ap.add_argument("patched")
    ap.add_argument("--show", default="",
                    help="逗号分隔的观测列名（如 attempts,lost_turn_trials）：判定相同不代表成本相同，"
                         "PASS→PASS 的行会把'多跑一遍模型'吞掉，这里把它显式列出来")
    ap.add_argument("--race", default="",
                    help="追加'天生竞态'的 case 名（逗号分隔）：这类翻脸不计回退，只并排印次数")
    ap.add_argument("--strict-gaps", action="store_true",
                    help="把'只在一边出现的 case'也计为失败（默认就计，留着这个开关是为了语义显式）")
    ap.add_argument("--out", default="", help="可选：把同一份表写成 json，报告里直接引用")
    args = ap.parse_args()

    ctrl, patch = load(Path(args.control)), load(Path(args.patched))
    cases = sorted(set(ctrl) | set(patch))
    if not cases:
        print("两份探针输出都读不到 case——先确认文件路径和探针是否真跑完了", file=sys.stderr)
        return 2

    show = [k.strip() for k in args.show.split(",") if k.strip()]
    race = RACE_CASES | {c.strip() for c in args.race.split(",") if c.strip()}
    counts = {"fix": 0, "REG": 0, "pass": 0, "fail": 0, "gap": 0, "race": 0, "cost_delta": 0}
    table = []
    for case in cases:
        c, p = ctrl.get(case), patch.get(case)
        if c is None or p is None:
            cls = "gap"
        elif case in race:
            # 竞态只报数不回退：两边的丢失次数并排印出来（--show 里带上 lost_turn_trials）
            cls = "race"
        elif not c.get("ok") and p.get("ok"):
            cls = "fix"
        elif c.get("ok") and not p.get("ok"):
            cls = "REG"
        elif p.get("ok"):
            cls = "pass"
        else:
            cls = "fail"
        counts[cls] += 1
        deltas = {k: (c.get(k), p.get(k)) for k in show if k in c or k in p}
        if any(a != b for a, b in deltas.values()):
            counts["cost_delta"] += 1
        table.append({"case": case, "control": verdict(c), "patched": verdict(p), "class": cls,
                      "control_note": note_of(c), "patched_note": note_of(p),
                      "deltas": {k: {"control": a, "patched": b} for k, (a, b) in deltas.items()}})

    width = max(len(t["case"]) for t in table) + 2
    header = f"{'case'.ljust(width)} {'对照':<6} {'补丁':<6} 判定   " + \
             ("观测列差异  " if show else "") + "补丁组仍失败的原因"
    print(header)
    for t in table:
        mark = {"fix": "修好", "REG": "回退!", "pass": "都通过", "fail": "都失败",
                "gap": "缺题!", "race": "竞态"}[t["class"]]
        reason = t["patched_note"] if t["class"] in ("fail", "REG", "gap", "race") else ""
        cols = "".join(f"{k}={'—' if v['control'] is None else v['control']}→"
                       f"{'—' if v['patched'] is None else v['patched']} "
                       for k, v in t["deltas"].items())
        print(f"{t['case'].ljust(width)} {t['control']:<6} {t['patched']:<6} {mark:<6} {cols}{reason}")
    print(f"小结：修好 {counts['fix']} / 回退 {counts['REG']} / 都通过 {counts['pass']} / "
          f"都失败 {counts['fail']} / 缺题 {counts['gap']}"
          + (f" / 竞态 {counts['race']}（按次数比，不计回退）" if counts["race"] else "")
          + (f" / 观测列变化 {counts['cost_delta']} 项" if show else ""))

    if args.out:
        Path(args.out).write_text(json.dumps(
            {"control": str(args.control), "patched": str(args.patched),
             "counts": counts, "rows": table}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote → {args.out}")

    bad = counts["REG"] + counts["gap"]
    if bad:
        print(f"闸门：{'回退' if counts['REG'] else ''}"
              f"{' 缺题' if counts['gap'] else ''} ——补丁不能算安全落地", file=sys.stderr)
        return 1
    print("闸门：零回退、零缺题（补丁只改了它声称要改的那些 case）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
