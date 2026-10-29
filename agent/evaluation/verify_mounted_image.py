"""Offline startup self-check for a BUILT agent image -- no service is started, nothing is written.

Run against a candidate image built from the working tree, never against the live service:

    docker build -t cs-agent-candidate python_agent
    docker run --rm --entrypoint python -w /app \
      -v "$PWD:/work:ro" -e PYTHONPATH=/app:/work \
      -e POSTGRES_DSN='postgresql://nobody:none@127.0.0.1:1/nonexistent' \
      -e QDRANT_URL=http://127.0.0.1:1 -e BUSINESS_API=http://127.0.0.1:1 \
      -e ORDER_BUSINESS_API=http://127.0.0.1:1 -e INTERNAL_API_TOKEN=probe \
      -e REDIS_URL=redis://127.0.0.1:1/0 -e MODEL_KEY_ENCRYPT_KEY=0 $(...) \
      cs-agent-candidate /work/agent/evaluation/verify_mounted_image.py

Or in one command: ``bash agent/evaluation/run_all.sh image``.

Why this exists: phase 6 asks to verify Docker after mounting the new graph. The question worth
answering is not "does the container boot" (the workers and DB would be involved) but "does the mounted entry point import, does the image carry the agent package, and does unfinished approval compatibility still work". Those are pure import-time / function-level facts, and
the env vars above are never used to open a connection here.
"""

from __future__ import annotations

import importlib
import os
import sys

CHECKS: list[tuple[str, object, object]] = []


def _under(path: str, root: str) -> bool:
    """True when `path` is `root` or sits inside it (bind-mount detection)."""
    path, root = os.path.abspath(path), os.path.abspath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def check(name: str, got, expected) -> None:
    CHECKS.append((name, got, expected))
    print(f"[{'PASS' if got == expected else 'FAIL'}] {name}: got={got!r} expected={expected!r}")


def main() -> int:
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

    # 2) 默认入口必须真的指向 agent/ 那张图。两件事分开验，缺一条都不算通过：
    #    ① 镜像里有 agent/ 包且可导入（Dockerfile: COPY --from=agent_src . /app/agent）；
    #    ② app 侧的 run_turn 是**委派**到 agent.cutover，而不是回到 app/multi_agent（旧接法）。
    #    只 import 不改行为；② 用源码判定，因为"能 import 到"不等于"入口在用"。
    import inspect

    from app.services import four_agent
    from agent.cutover import run_turn as graph_run_turn
    from agent.rollout import graph_enabled

    check("import agent.rollout + agent.cutover", bool(graph_enabled and graph_run_turn), True)
    delegated = inspect.getsource(four_agent.run_turn)
    check("four_agent.run_turn delegates to agent.cutover",
          "graph_run_turn" in delegated and
          four_agent.run_turn.__globals__.get("graph_run_turn") is graph_run_turn, True)

    # 3) Ordinary turns always use the new graph; only pending approvals retain
    #    their original checkpoint. Obsolete rollout flags cannot change that.
    check("ordinary chat -> four-agent graph", four_agent.enabled(), True)
    check("pending approval -> historical graph", four_agent.enabled(pending_ticket=True), False)
    os.environ["FOUR_AGENT_GRAPH"] = "off"
    check("obsolete off flag ignored", four_agent.enabled(), True)
    os.environ.pop("FOUR_AGENT_GRAPH", None)

    # 4) 默认入口就是 agent/ 那张图，所以镜像里**必须**有 agent/ 包
    #    （`python_agent/Dockerfile`: COPY --from=agent_src . /app/agent）。
    #    这一项在"默认 off"时代断言的是 absent；现在反过来 —— 包不在镜像里，开关默认打开就会
    #    让服务启动失败；缺包必须显式暴露。
    #    只探镜像自己的 sys.path：本脚本从只读挂载跑，路径上带 /work 会让 `agent` 可导入，
    #    从而把这条检查变成假话（第一次跑就踩过：PYTHONPATH 带了 /work）。
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
        try:
            mod = importlib.import_module("agent")
            outcome = f"present via {getattr(mod, '__file__', '?')}"
        except ModuleNotFoundError:
            outcome = "absent"
    finally:
        sys.path = saved
    check("agent/ present in production image", outcome.startswith("present"), True)
    print(f"       agent package resolved at: {outcome}")
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
    sys.exit(main())
