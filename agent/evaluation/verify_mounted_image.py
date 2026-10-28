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
answering is not "does the container boot" (the workers and DB would be involved) but "does
importing the mounted entry point work, and is the rollout switch off by default, and does an
unfinished HITL approval still go to the old graph". Those three are pure import-time /
function-level facts, and the env vars above are never used to open a connection here.
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

    # 2) the mounted graph must be importable from inside the image (it lives in the app package,
    #    so unlike agent/ it survives the ./python_agent build context).
    from app.multi_agent.graph import GraphDependencies, build_graph
    check("import app.multi_agent.graph", bool(build_graph and GraphDependencies), True)

    # 3) rollout switch semantics, straight from four_agent.enabled().
    from app.services import four_agent

    os.environ.pop("FOUR_AGENT_GRAPH", None)
    check("FOUR_AGENT_GRAPH unset -> off", four_agent.enabled(), False)
    check("unset + pending ticket -> off", four_agent.enabled(pending_ticket=True), False)
    os.environ["FOUR_AGENT_GRAPH"] = "on"
    check("FOUR_AGENT_GRAPH=on -> on", four_agent.enabled(), True)
    # The boundary that must survive the switch: an unfinished approval stays on the old graph.
    check("on + pending ticket -> still old graph", four_agent.enabled(pending_ticket=True), False)
    os.environ["FOUR_AGENT_GRAPH"] = "true"
    check("FOUR_AGENT_GRAPH=true accepted", four_agent.enabled(), True)
    os.environ["FOUR_AGENT_GRAPH"] = "0"
    check("FOUR_AGENT_GRAPH=0 -> off", four_agent.enabled(), False)

    # 4) the agent/ deliverable is deliberately NOT part of this build context; recording that
    #    keeps the "swap the import to agent/cutover.py" advice honest about what the image has.
    #    Probe the IMAGE's own sys.path only. This script runs off a read-only bind mount, so a
    #    repo-root entry on the path would make `agent` importable and turn the check into a lie
    #    (that is exactly what happened on the first run: PYTHONPATH carried /work).
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
    check("agent/ absent from production image", outcome, "absent")
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
