# ruff: noqa: RUF001  # 中文断言消息
"""检查"检查挂载入口的那两支探针"自己是否还成立。

2026-10-28 挂载入口换了实现（`app/services/four_agent.py` 变成薄适配器，
`run_turn` 里 `from app.agent.support.cutover import run_turn`），于是旧探针 `probe_mounted_graph.py`
打的桩（全在 `app.multi_agent.graph.*`）不再覆盖生产路径，新探针 `probe_mounted_bridge.py`
接管了 `run_all.sh mounted`。这类"被测对象换了、量具没换"的失败不会让任何功能测试报红，
只会让一张账本悄悄变成假绿（陷阱 #5 / #14 的又一形状）。所以这里钉三件小事：

1. 接法判定是按**源码**做的，不是按我的记忆——`VIA_BRIDGE` 必须与 `four_agent.run_turn` 的实际 import 一致；
2. 新探针的闸门必须**覆盖它自己印出来的每一项**（除了明确写进注释的那两项缺隙描述），
   漏一项目光就等于那一项永不回归；
3. 旧探针在换接法之后必须**自述并退出**，不能留一段 traceback 让人误读成被测链路坏了。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[2]
AGENT_DIR = PKG / "app" / "agent" / "support"
EVAL_DIR = PKG / "evaluation" / "four_agent"


def _emitted_cases(source: str) -> list[str]:
    """探针里 emit(\"<case>\", ...) 印出的项名，按出现顺序去重。"""
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "emit" and node.args
                and isinstance(node.args[0], ast.Constant)):
            name = str(node.args[0].value)
            if name not in names:
                names.append(name)
    return names


def test_the_bridge_detection_agrees_with_the_entry_it_reads() -> None:
    """`VIA_BRIDGE` 判定与 `four_agent.run_turn` 的真实 import 必须一致。

    这里**不导入 app 包**（宿主 venv 里 app 侧依赖不全，探针本来就跑在容器内），
    而是把两边源码里同一件事对起来：入口 import 了 `agent.cutover` ⇒ 判定必须是 True。
    """
    entry_src = (PKG / "app" / "services" / "four_agent.py").read_text(
        encoding="utf-8")
    guards_src = (EVAL_DIR / "probe_mounted_guards.py").read_text(encoding="utf-8")
    reaches_cutover = "app.agent.support.cutover" in entry_src
    detects = "VIA_BRIDGE = \"app.agent.support.cutover\" in inspect.getsource(_pmg.four_agent.run_turn)" in guards_src
    assert detects, "挂载侧对抗语料探针必须按源码判定接法，而不是写死一个实现名"
    if reaches_cutover:
        assert "python_agent/evaluation/four_agent/probe_mounted_bridge.py" in (
            EVAL_DIR / "run_all.sh").read_text(encoding="utf-8"), (
            "入口已经是 support 图了，`run_all.sh mounted` 必须指到新探针，否则账本量的是下线那份实现")


def test_the_bridge_gate_covers_every_case_the_probe_prints() -> None:
    """闸门集合必须等于"探针印出的项"减去**写在注释里的**两项缺隙描述。"""
    src = (EVAL_DIR / "probe_mounted_bridge.py").read_text(encoding="utf-8")
    printed = set(_emitted_cases(src))
    gate = set(re.findall(r'"(B[0-9A-Za-z_]+)"', src.split("invariant = {")[1].split("}")[0]))
    nongated = {"B2_replay_without_turn_id", "B11_two_instances_same_thread_loses"}
    assert printed, "探针没印任何 emit(...) 项：说明它已经不能跑了"
    assert gate | nongated == printed, (
        f"闸门漏掉或未覆盖：{sorted((gate | nongated) ^ printed)}——"
        "新增一项却不进闸门，等于这一项永远不会拦住回归")
    assert printed == {"B0_topology", "B1_compound_entry", "B2_replay_without_turn_id",
                       "B2b_replay_with_turn_id", "B3_legacy_history_visible", "B4_bounded_retry",
                       "B5_failure_turn_persisted_and_explained", "B6_watermark_advances_without_deleting",
                       "B7_unverified_order_claim_blocked", "B9_same_process_concurrency",
                       "B10_entry_lock_granularity", "B11_two_instances_same_thread_loses"}, printed


def test_the_old_probe_refuses_to_run_as_if_it_were_production() -> None:
    """旧接法探针必须自述并退出，而不是抛一段看起来像被测链路坏掉的 traceback。"""
    src = (EVAL_DIR / "probe_mounted_graph.py").read_text(encoding="utf-8")
    assert "app.agent.support.cutover" in src and "probe_mounted_bridge.py" in src, (
        "旧探针没有对换接法作出自述：它会在 run_turn 处抛 RuntimeError，"
        "读输出的人会把「实现换了」误读成「链路坏了」")
    guard = src.split("async def main():")[1]
    assert "raise SystemExit" in guard.split("rows = []")[0], "自述必须是退出，不是打印一句就继续跑"
