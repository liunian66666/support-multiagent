"""Offline startup self-check for a BUILT agent image -- no service is started, nothing is written.

Run against a candidate image built from the working tree, never against the live service:

    docker build -t cs-agent-candidate python_agent
    docker run --rm --entrypoint python -w /app \
      -v "$PWD:/work:ro" -e PYTHONPATH=/app:/work \
      -e POSTGRES_DSN='postgresql://nobody:none@127.0.0.1:1/nonexistent' \
      -e QDRANT_URL=http://127.0.0.1:1 -e BUSINESS_API=http://127.0.0.1:1 \
      -e ORDER_BUSINESS_API=http://127.0.0.1:1 -e INTERNAL_API_TOKEN=probe \
      -e REDIS_URL=redis://127.0.0.1:1/0 -e MODEL_KEY_ENCRYPT_KEY=0 $(...) \
      cs-agent-candidate /work/python_agent/evaluation/four_agent/verify_mounted_image.py

Or in one command: ``bash python_agent/evaluation/four_agent/run_all.sh image``.

Why this exists: phase 6 asks to verify Docker after mounting the new graph. The question worth
answering is not "does the container boot" (the workers and DB would be involved) but "does the
mounted entry point import, does the image carry the graph package, and does unfinished approval
compatibility still work". Those are pure import-time / function-level facts, and
the env vars above are never used to open a connection here.

注意（收敛后）：主图已并入 ``app/agent/support/``，镜像构建上下文只有 ``python_agent/``，
不存在第二个 ``agent`` 包。所以第 4 项探的是 ``app.agent.support.graph`` 是否**从镜像自身
路径**可导入 —— 挂载路径（``/work``）已在扫描时排除，否则这条会变成假话。
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from types import SimpleNamespace

CHECKS: list[tuple[str, object, object]] = []


def _under(path: str, root: str) -> bool:
    """True when `path` is `root` or sits inside it (bind-mount detection)."""
    path, root = os.path.abspath(path), os.path.abspath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def check(name: str, got, expected) -> None:
    CHECKS.append((name, got, expected))
    print(f"[{'PASS' if got == expected else 'FAIL'}] {name}: got={got!r} expected={expected!r}")


async def _main() -> int:
    # 1) the chat entry module, now carrying the four_agent branch, must import at all:
    #    chat.py imports at module top level, so a broken import here is exactly the
    #    "service dies on startup" failure mode that the deploy notes warn about.
    try:
        import app.api.chat as chat
    except Exception as exc:  # noqa: BLE001 - the failure text is the finding
        print(f"[FAIL] import app.api.chat: {type(exc).__name__}: {exc}")
        return 1
    check("import app.api.chat", "ok", "ok")
    check("chat routes registered", len(chat.router.routes) >= 4, True)

    # 2) 默认入口必须真的指向 app/agent/support/ 那张图。两件事分开验，缺一条都不算通过：
    #    ① 镜像里有 app/agent/support 包且可导入（Dockerfile: COPY --from=builder /build /app）；
    #    ② app 侧的 run_turn 是**委派**到 agent.cutover，而不是回到 app/multi_agent（旧接法）。
    #    只 import 不改行为；② 用源码判定，因为"能 import 到"不等于"入口在用"。
    import inspect

    from app.agent.support.cutover import run_turn as graph_run_turn
    from app.agent.support.rollout import graph_enabled
    from app.services import four_agent

    check("import app.agent.support.rollout + agent.cutover", bool(graph_enabled and graph_run_turn), True)
    delegated = inspect.getsource(four_agent.run_turn)
    check("four_agent.run_turn delegates to agent.cutover",
          "graph_run_turn" in delegated and
          four_agent.run_turn.__globals__.get("graph_run_turn") is graph_run_turn, True)

    # 3) 收敛后的语义：**没有**"走哪张图"的开关，唯一判定是服务端审批三态
    #    （pending → 409 / unknown → 503 / clear → 主图）。旧开关置任何值都不该长出开关。
    check("no graph switch left", hasattr(four_agent, "enabled"), False)
    check("approval probe is tri-state",
          sorted(member.value for member in four_agent.ApprovalState),
          ["clear", "pending", "unknown"])
    os.environ["FOUR_AGENT_GRAPH"] = "off"
    os.environ["SUPPORT_AGENT_V2"] = "off"
    os.environ["X-Agent-Graph"] = "v1"
    check("retired flags ignored", hasattr(four_agent, "enabled"), False)
    # 没有 checkpointer 的镜像里探测必须是 clear（不是 unknown，也不是崩）
    check("no checkpointer -> clear",
          (await four_agent.approval_state(SimpleNamespace(state=SimpleNamespace()),
                                           "t_demo:probe")).value, "clear")
    for name in ("FOUR_AGENT_GRAPH", "SUPPORT_AGENT_V2", "X-Agent-Graph"):
        os.environ.pop(name, None)

    # 4) 默认入口就是 app/agent/support/ 那张图，所以镜像里**必须**带着它
    #    （`python_agent/Dockerfile`: COPY --from=builder /build /app，构建上下文即 python_agent/）。
    #    这一项在"默认 off + 图在第二个 agent 包"时代断言的是 `agent` 可导入；收敛后包的落点是
    #    `app.agent.support.graph`，缺它服务启动就失败，必须显式暴露。
    #    只探镜像自己的 sys.path：本脚本从只读挂载跑，路径上带 /work 会让 `app` 从挂载目录
    #    导入成功，从而把这条检查变成假话（第一次跑就踩过：PYTHONPATH 带了 /work）。
    excluded = {os.path.abspath(os.path.dirname(__file__))}
    image_path = []
    for entry in sys.path:
        resolved = os.path.abspath(entry) if entry else os.getcwd()
        if resolved in excluded or _under(resolved, "/work"):
            continue
        if os.path.isdir(resolved):
            image_path.append(resolved)
    saved, sys.path = sys.path, image_path
    try:
        # 已从挂载路径导入过的模块会留在 sys.modules 里，先摘掉再探，否则量到的还是挂载
        stashed = {k: v for k, v in list(sys.modules.items())
                   if k == "app" or k.startswith("app.agent.support")}
        for k in stashed:
            del sys.modules[k]
        try:
            mod = importlib.import_module("app.agent.support.graph")
            outcome = f"present via {getattr(mod, '__file__', '?')}"
        except ModuleNotFoundError:
            outcome = "absent"
        finally:
            sys.modules.update(stashed)
    finally:
        sys.path = saved
    check("app/agent/support in production image", outcome.startswith("present"), True)
    check("graph package resolved outside the mount", "/work" in outcome, False)
    print(f"       graph package resolved at: {outcome}")
    check("probed image path (must not contain the mount)",
          any("/work" in p for p in image_path), False)

    failed = [name for name, got, expected in CHECKS if got != expected]
    print(json_summary(len(CHECKS), len(failed)))
    return 1 if failed else 0


def json_summary(total: int, failed: int) -> str:
    import json
    return json.dumps({"checks": total, "passed": total - failed, "failed": failed},
                      ensure_ascii=False)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
