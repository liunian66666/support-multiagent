# ruff: noqa: RUF001  # 报告文案含中文
"""现场演示的第二条路：把"复合咨询 + 长对话续聊"跑在**已挂载的 app 侧图**上，并留下可核对的原始记录。

`bash python_agent/evaluation/four_agent/run_all.sh demo` 演的是 `agent/graph.py`。但真正上台按开关演示时会走
`FOUR_AGENT_GRAPH` -> `app.services.four_agent.run_turn`。**2026-10-28 之后这条链的下一跳换了**：
four_agent 是薄适配器，实际执行的是 `agent.cutover.run_turn` -> `agent/graph.py`（旧接法的
`app/multi_agent/graph.py` 已不在这条路上）。本脚本因此改跑新接法，并且**逐轮去读检查点里的持久化状态**
而不是入口返回的那份摘要字典——新接法返回的字典里没有 messages/水位线，只看它会得出"记忆没推进"的错结论。：8 轮真实入口对话（问候 -> 单订单 -> 复合问题 -> 指代追问 ->
政策数值 -> 若干日常轮把线程推过压缩阈值 -> 换图实例模拟重启 -> 问回最早的锚点），逐轮记录
委派了哪些 Agent、Reviewer 判了什么、检查点里剩多少条、水位线/摘要多长、**这一轮模型实际看到的
上下文里锚点还在不在**。

零模型调用、零真实数据：脚本化委派 + `InMemorySaver`；业务订单适配器与租户模型解析都被替换。
"重启"那一轮的做法是 `agent.cutover.clear_services()` ——同一 checkpointer、重新编译图，
与服务进程重启后第一次请求的形状一致。

    bash python_agent/evaluation/four_agent/run_all.sh demo-mounted
    # 原始记录 → python_agent/evaluation/four_agent/runs/demo_mounted.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_pmg = _load("python_agent/evaluation/four_agent/probe_mounted_graph.py", "_probe_mounted_graph")
_pmb = _load("python_agent/evaluation/four_agent/probe_mounted_bridge.py", "_probe_mounted_bridge")

TENANT = "t_demo"
THREAD = f"{TENANT}:demo-mounted-1"
ANCHOR = "这一单实付 168 元，抬头开「星辰科技」，单号 ORD-2024-003。"
NEEDLES = ["ORD-2024-003", "168元", "星辰科技"]

TURNS = [
    "你好",
    ANCHOR,
    "ORD-2024-003 到哪了？顺便问下怎么退货。",           # 复合：Order + FAQ 同轮
    "那它什么时候能到？",                                 # 指代上一单的时效
    "退货运费谁承担？有没有期限？",
    "了解了，还有别的要注意吗？",
    "好的，那我等发货。",
]
RESTART_ASK = "最早那单的实付金额、抬头和单号分别是什么？"


def text(message) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else str(content)


def squash(s: str) -> str:
    """Whitespace-insensitive matching, same discipline as `score_heldout.norm`.

    Without this, a needle written as `168元` never matches a reply that says `168 元`, and the
    table reads like eleven lost anchors when it is really one test-data typo (that trap is already
    documented for the four-arm probe; the demo script is the same trap one step away).
    """
    return re.sub(r"\s+", "", s or "")


def scripted_delegates(question: str):
    out = []
    if "ORD-" in question or "它" in question or "那单" in question:
        out.append("consult_order")
    if any(k in question for k in ("退货", "运费", "期限")):
        out.append("consult_faq")
    return out or ["consult_general"]


async def main(args: argparse.Namespace) -> int:
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver

    merge_model = _pmb.BridgeModel(
        text="摘要：客户提过一笔订单并咨询过退货，具体单号金额抬头未保留。")
    shared = InMemorySaver()
    app, _ = _pmb.app_with(shared, model=merge_model)
    history = [m for i in range(8) for m in (HumanMessage(content=f"历史提问{i}"),
                                             AIMessage(content=f"历史回答{i}"))]
    supervisor = _pmg.Supervisor(scripted_delegates, "已按核验结果答复。")
    rows = []

    script = [(f"turn-{index + 1}", question) for index, question in enumerate(TURNS)]
    script.append(("after-restart", RESTART_ASK))
    for turn_id, question in script:
        if turn_id == RESTART_ASK:
            # 同一 checkpointer、重新编译的图实例 == 服务重启后的第一次请求
            import app.agent.support.cutover as cutover
            cutover.clear_services()
        graph, state, info = await _pmb.bturn(app, merge_model, supervisor, question,
                                              thread=THREAD, history=history)
        vals = await _pmb.persisted(graph, THREAD)      # 读**持久化状态**，不读入口返回的摘要字典
        msgs = vals.get("messages", [])
        NL = chr(10)
        fed = NL.join(text(m) for m in supervisor.last_prompt)
        channel = NL.join(text(m) for m in msgs)
        rows.append({
            "turn": turn_id, "question": question,
            "agents": [d["specialist"] for d in vals.get("delegations", [])],
            "trace_specialist": info["specialist"], "trace_route": info["route"],
            "review_issues": state.get("review_issues") or [],
            "outcome": state.get("outcome"),
            "answer": (state.get("answer") or "")[:80],
            "checkpoint_messages": len(msgs),
            "summary_watermark": int(vals.get("summary_watermark") or 0),
            "summary_chars": len(vals.get("summary") or ""),
            "fed_chars": len(fed),
            "needles_in_fed_context": {n: (squash(n) in squash(fed)) for n in NEEDLES},
            "needles_in_checkpoint": {n: (squash(n) in squash(channel)) for n in NEEDLES},
        })
        r = rows[-1]
        print(f"{r['turn']:<26} agents={','.join(r['agents']) or '-':<10} "
              f"checkpoint={r['checkpoint_messages']:>3} wm={r['summary_watermark']:>3} "
              f"sum={r['summary_chars']:>3} fed={r['fed_chars']:>5} "
              f"锚点进上下文={sum(r['needles_in_fed_context'].values())}/3 "
              f"锚点可恢复={sum(r['needles_in_checkpoint'].values())}/3")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    composite = next(r for r in rows if r["turn"] == "turn-3")
    final = rows[-1]
    print(f"\n{datetime.now(timezone.utc).isoformat(timespec='seconds')}  "
          f"复合轮同轮委派 = {composite['agents']}（trace specialist={composite['trace_specialist']}）")
    print(f"重启后续聊 = {bool(final['answer'])}；重启前水位线={final['summary_watermark']}、"
          f"检查点剩 {final['checkpoint_messages']} 条")
    print(f"最终锚点可见性 = {sum(final['needles_in_fed_context'].values())}/3，"
          f"最终锚点可恢复 = {sum(final['needles_in_checkpoint'].values())}/3")
    print(f"原始数据 → {out}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="mounted-path live demo record")
    parser.add_argument("--out", default=str(ROOT / "agent" / "evaluation" / "runs" /
                                             "demo_mounted.jsonl"))
    raise SystemExit(asyncio.run(main(parser.parse_args())))
