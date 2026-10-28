# 评测文案与判分注释含中文
"""Phase 6 collection: drive the new four-agent graph over the frozen题集.

    docker run --rm --env-file ./.env --network cs-customer-service_cs-network \\
      -v "$ROOT:/work:ro" -v "$ROOT/agent/evaluation/runs:/out" \\
      -e PYTHONPATH=/work:/work/python_agent -e PYTHONPYCACHEPREFIX=/tmp/pyc \\
      -e QDRANT_URL=http://qdrant:6333 -e BUSINESS_API=http://business:8001 \\
      -e ORDER_BUSINESS_API=http://business:8001 -w /work/python_agent cs-test:agent \\
      sh -c 'export POSTGRES_DSN="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}"; \\
             python /work/agent/evaluation/run_ab_live.py heldout --out /out/heldout_v2_r1.jsonl'

Two arms:

``heldout``  the frozen 36-case set (16 target + 20 control), one fresh thread per case,
             so nothing about ordering can flatter the result.
``mem``      the 10 long conversations from ``evaluation/memory_ab_live.cases`` — same
             needles, same fillers, same single-shot probe — so the numbers can be read
             next to the existing off/on memory arms.

Scoring is deliberately the project's own literal rule (required phrases present after
stripping markdown emphasis; forbidden phrase ⇒ blocked; a numeric claim accompanied by
a refusal ⇒ flagged for manual review, never counted as correct). Cases the frozen set
marks ``manual`` are reported as 需人工复核, not as passes. This tool produces raw
evidence; it does not declare answer quality.

Side effects, stated plainly: with ``--parity`` the FAQ delegate is the production
agent, so ``search_kb`` records ``kb_hits`` rows for these synthetic threads and fills
an in-process 5-minute cache inside this throwaway container (it cannot touch the live
service's cache). Without ``--parity`` the read-only shadow FAQ is used instead and the
prompt differs from production — comparable on retrieval, not on wording.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from agent.legacy_bindings import deps_for_app, session_context
from agent.service import SupportService

TENANT = "t_demo"
GOLD = Path("/work/python_agent/evaluation/gold_heldout_v1.jsonl")


def _load_mock():
    """The mock endpoint lives next to this script; load it by path (no package needed)."""
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("mock_openai_server",
                                                 here / "mock_openai_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_corpus():
    """逐轮脚本指纹/转写行 helper，同样按路径加载（容器里 --out 走 /work 只读挂载，包名不可靠）。"""
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("mem_corpus", here / "mem_corpus.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

# [ours] identical to python_agent/evaluation/score_heldout.norm — the ruler the frozen
# off-baseline (ho_off_r4_checklist.jsonl) was scored with. Stripping emphasis alone is
# not enough: "1 个工作日" vs required "1个工作日" flips exactly one held-out case, so a
# collector that disagrees with the scorer would invent a 1-case regression out of
# whitespace. Keep the two in lockstep or the A/B diff is meaningless.
def norm(value: str) -> str:
    return re.sub(r"[\s*_`>]", "", str(value))


def load_heldout(limit: int, only: str = "") -> list[dict]:
    cases = [json.loads(line) for line in GOLD.read_text(encoding="utf-8").splitlines() if line.strip()]
    if only:
        # re-measuring a handful of flipped cases shouldn't require paying for all 36 again
        wanted = [item.strip() for item in only.split(",") if item.strip()]
        picked = [case for case in cases if case["id"] in wanted]
        missing = sorted(set(wanted) - {case["id"] for case in picked})
        if missing:
            raise SystemExit(f"--only 里有不存在的题号：{missing}")
        return picked
    return cases[:limit]


def literal_score(answer: str, case: dict) -> dict[str, Any]:
    text = norm(answer or "")
    required = [term for term in case.get("required", [])]
    forbidden = [term for term in case.get("forbidden", [])]
    missing = [term for term in required if norm(term) not in text]
    tripped = [term for term in forbidden if norm(term) in text]
    refusal = bool(re.search(r"无法(确认|核实|查询)|以店铺公示为准|建议.{0,4}人工", text))
    numeric_claim = bool(re.search(r"\d+(?:\.\d+)?\s*(?:元|天|小时|%|个工作日)", text))
    return {
        "required_missing": missing,
        "forbidden_hit": tripped,
        "literal_pass": not missing and not tripped,
        "numeric_with_refusal": numeric_claim and refusal,
        "needs_manual_review": bool(case.get("manual")) and (not missing or tripped or refusal),
    }


async def scaffold() -> Any:
    """A minimal app stand-in: the same clients the real lifespan builds."""
    import httpx
    from psycopg_pool import AsyncConnectionPool
    from qdrant_client import QdrantClient

    from app.agent import tools as business_tools
    from app.agent.retrieval import ensure_tenant_vector, get_tenant_retriever
    from app.infra.config import POSTGRES_DSN, QDRANT_URL
    from app.infra.context import pending_ticket_var, tenant_var, thread_var
    from app.infra.db.tenant import collection_name

    logger = logging.getLogger("eval")
    client = QdrantClient(url=QDRANT_URL)
    if not client.collection_exists(collection_name(TENANT)):
        raise SystemExit(f"collection {collection_name(TENANT)} missing; refusing to create it")
    pool = AsyncConnectionPool(POSTGRES_DSN, min_size=1, max_size=4, open=False,
                               kwargs={"autocommit": True})
    await pool.open()
    http = httpx.AsyncClient(timeout=30)
    app = SimpleNamespace(state=SimpleNamespace(
        http_client=http, qdrant_client=client, db_pool=pool, vec_dim=0,
        tenant_stores={}, tenant_retrievers={}, tenant_agents={}, faq_agents={},
        intake_agents={}, checkpointer=None))
    business_tools.set_app(app, logger, tenant_var, pending_ticket_var, thread_var)
    await ensure_tenant_vector(app, TENANT)
    if get_tenant_retriever(app, TENANT) is None:
        await app.aclose() if hasattr(app, "aclose") else None
        raise SystemExit("tenant retriever unavailable")
    return app, http, pool


def _stream(path: str) -> tuple[list, Any]:
    """逐条落盘的采集器写入器。

    长对话臂一跑就是半小时（10 条 × 12 轮真模型），最后一行统计崩掉或进程被 kill，
    都不该把整批已采到的数据丢掉——旧路径臂就是这么丢过一次（见 run_legacy_heldout.py
    里同一决定的注释）。先截断，之后每例 append 一行。
    """
    rows: list = []
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("", encoding="utf-8")

    def persist(record: dict) -> None:
        rows.append(record)
        with out.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    return rows, persist


async def run_heldout(app: Any, args: argparse.Namespace) -> list[dict]:
    cases = load_heldout(args.limit, args.only)
    rows, persist = _stream(args.out)
    for case in cases:
        thread = f"{TENANT}:v2-{case['id']}-{uuid.uuid4().hex[:6]}"

        async def deps_for_tenant(target: str, _app=app) -> Any:
            return await deps_for_app(_app, target, reuse_faq_agent=args.parity,
                                      model_source=args.model, model_name=args.model_name)

        service = SupportService(deps_for_tenant, checkpointer=InMemorySaver())
        await session_context(TENANT, thread)()
        start = time.perf_counter()
        record = {"id": case["id"], "category": case.get("category"), "query": case["query"],
                  "mode": "parity" if args.parity else "shadow", "model": args.model,
                  "model_name": args.model_name or "",
                  "comparable": args.model == "tenant" and not args.model_name, "thread": thread}
        try:
            turn = await service.run_turn(TENANT, thread, case["query"], turn_id=f"{case['id']}-1")
            record.update(answer=turn.answer, outcome=turn.outcome, roles=turn.roles,
                          evidence_ids=turn.evidence_ids, attempts=turn.attempts,
                          graph_error=turn.error,
                          usage=turn.usage, latency_ms=round((time.perf_counter() - start) * 1000, 1),
                          **literal_score(turn.answer, case))
        except Exception as exc:
            record.update(error=f"{type(exc).__name__}: {str(exc)[:200]}", literal_pass=False,
                          latency_ms=round((time.perf_counter() - start) * 1000, 1))
        persist(record)
        print(f"{case['id']:<10} pass={record.get('literal_pass')} outcome={record.get('outcome')} "
              f"roles={record.get('roles')} forbidden={record.get('forbidden_hit')} "
              f"missing={record.get('required_missing')} {record['latency_ms']}ms", flush=True)
    return rows


async def run_mem(app: Any, args: argparse.Namespace) -> list[dict]:
    from evaluation.memory_ab_live import cases as long_cases

    corpus = _load_corpus()
    rows, persist = _stream(args.out)
    for case in (long_cases()[: args.limit]):
        thread = f"{TENANT}:v2mem-{case['id']}-{uuid.uuid4().hex[:6]}"

        async def deps_for_tenant(target: str, _app=app) -> Any:
            return await deps_for_app(_app, target, reuse_faq_agent=args.parity,
                                      model_source=args.model, model_name=args.model_name)

        service = SupportService(deps_for_tenant, checkpointer=InMemorySaver(),
                                 graph_kwargs={"compact_trigger": args.trigger,
                                               "compact_keep": args.keep})
        await session_context(TENANT, thread)()
        started = time.perf_counter()
        per_turn, tokens = [], {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        for index, message in enumerate(case["turns"]):
            turn_started = time.perf_counter()
            turn = await service.run_turn(TENANT, thread, message, turn_id=f"{case['id']}-t{index}")
            # 逐轮转写：旧臂只有 {turn, error, latency_ms} 时，"这一例到底说过什么、
            # 模型当时答了什么"在原始数据里是空白，探针失手就无从归因（阶段4 自证性缺口）。
            per_turn.append(corpus.turn_row(
                index, message, turn.answer,
                latency_ms=(time.perf_counter() - turn_started) * 1000,
                extra={"outcome": turn.outcome, "roles": turn.roles, "error": turn.error}))
            for key in tokens:
                tokens[key] += (turn.usage or {}).get(key, 0)
        probe_started = time.perf_counter()
        probe = await service.run_turn(TENANT, thread, case["probe"], turn_id=f"{case['id']}-probe")
        probe_latency = round((time.perf_counter() - probe_started) * 1000, 1)
        state = await service.memory_state(TENANT, thread)
        text = norm(probe.answer or "")
        persist({"id": case["id"], "needle_ref": case["needles"]["ref"],
                     "needle_company": case["needles"]["company"], "probe": case["probe"],
                     "answer": probe.answer, "expect": case["expect"],
                     "thread": thread, "script_sha": corpus.fingerprint(case),
                     "probe_latency_ms": probe_latency, "outcome": probe.outcome,
                     "roles": probe.roles, "probe_error": probe.error,
                     "containment": [norm(term) in text for term in case["expect"]],
                     "all_needles_recalled": all(norm(term) in text for term in case["expect"]),
                     "turns": len(case["turns"]) + 1, "memory_state": state,
                     "tokens_total": tokens, "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                     "per_turn": per_turn, "mode": "parity" if args.parity else "shadow",
                     "model": args.model, "model_name": args.model_name or "",
                     "comparable": args.model == "tenant" and not args.model_name})
        print(f"{case['id']:<8} recall={rows[-1]['containment']} watermark={state['watermark']} "
              f"tokens={tokens['total_tokens']} {rows[-1]['latency_ms']}ms", flush=True)
    return rows


async def main(args: argparse.Namespace) -> None:
    if args.model == "platform" and args.parity:
        # The production FAQ agent builds its own model from the tenant config, which
        # is exactly the endpoint being overridden here. Silently mixing the two would
        # produce an arm whose supervisor and delegate run on different backends.
        raise SystemExit("--model platform 必须配合 shadow FAQ（去掉 --parity）")
    if args.model_name and args.model != "platform":
        raise SystemExit("--model-name 只在 --model platform 下有效：换 key 的方式只有两种，"
                         "custom 走环境变量，tenant 不换 key 就不许换模型名")
    if args.model_name and args.parity:
        raise SystemExit("--model-name 必须配合 shadow FAQ：生产 FAQ 用租户自己配置的模型，"
                         "一轮里会同时打到两个后端")
    if args.model == "custom" and args.parity:
        raise SystemExit("--model custom 必须配合 shadow FAQ：生产 FAQ 会用租户自己配置的模型，"
                         "两条后端混在一轮里就没法解释结果")
    stats_url = ""
    if args.model == "custom":
        if args.mock:
            server = _load_mock().serve_in_background(args.mock_port)
            os.environ["AGENT_CHAT_BASE_URL"] = f"http://127.0.0.1:{args.mock_port}/v1"
            stats_url = f"http://127.0.0.1:{args.mock_port}/mock/stats"
        if not os.getenv("AGENT_CHAT_BASE_URL"):
            raise SystemExit("--model custom 需要 AGENT_CHAT_BASE_URL（或用 --mock 现起一个）")
        os.environ.setdefault("AGENT_CHAT_MODEL", "mock-support-model")
        print(f"[custom] 端点 {os.getenv('AGENT_CHAT_BASE_URL')} · 模型 "
              f"{os.getenv('AGENT_CHAT_MODEL')} · 这一臂 comparable=False", flush=True)
    app, http, pool = await scaffold()
    try:
        rows = await (run_heldout(app, args) if args.arm == "heldout" else run_mem(app, args))
        if stats_url:  # how many real HTTP round trips the arm actually made
            try:
                response = await http.get(stats_url, timeout=5)
                print("[mock/stats]", response.text, flush=True)
            except Exception as exc:  # noqa: BLE001 - diagnostics must never fail the run
                print("[mock/stats] unavailable:", exc)
    finally:
        await pool.close()
        await http.aclose()
        if args.model == "custom" and args.mock:
            server.should_exit = True
    # 不在这里重写整批：两臂都是**逐条落盘**（_stream / persist），进程被 kill 或最后一行
    # 统计抛异常时，已经采到的例子仍在文件里——半小时的模型调用不能因为收尾而蒸发。
    out = Path(args.out)
    passed = sum(1 for row in rows if row.get("literal_pass") or row.get("all_needles_recalled"))
    print(f"\n{args.arm}: {len(rows)} 例，字面判定通过 {passed}，原始数据 → {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("arm", choices=["heldout", "mem"])
    parser.add_argument("--limit", type=int, default=36)
    parser.add_argument("--only", default="",
                        help="逗号分隔的题号：只重测这几种（改动后复测翻掉的题不必再付 36 题的钱）")
    parser.add_argument("--out", default="/tmp/ab.jsonl")
    parser.add_argument("--parity", action="store_true", help="use the production FAQ agent (writes kb_hits)")
    parser.add_argument("--model", choices=["tenant", "platform", "custom"], default="tenant",
                        help="chat model source; 'platform' keeps the tenant KB but changes the model, "
                             "so results are NOT comparable with a baseline collected on the tenant "
                             "model. 'custom' points at AGENT_CHAT_BASE_URL (an OpenAI-compatible "
                             "endpoint, e.g. the bundled --mock server) to test the transport; those "
                             "rows carry comparable=False and measure nothing about wording.")
    parser.add_argument("--model-name", dest="model_name", default="",
                        help="with --model platform: keep the platform key/base_url but swap only the "
                             "model id. GET /models showed the key is alive and sees 182 models while "
                             "the configured one is 403 Free quota exhausted, so a per-model free tier "
                             "is the difference between 'no live arm' and 'a live arm'. Rows are "
                             "stamped comparable=False: same model on both arms is comparable with "
                             "itself, not with the frozen qwen3.8-flash-next baseline.")
    parser.add_argument("--mock", action="store_true",
                        help="with --model custom: start agent/evaluation/mock_openai_server.py "
                             "in-process and point the arm at it (real HTTP, deterministic replies)")
    parser.add_argument("--mock-port", type=int, default=8123)
    parser.add_argument("--trigger", type=int, default=12, help="mem arm: compaction trigger")
    parser.add_argument("--keep", type=int, default=6, help="mem arm: verbatim tail kept")
    asyncio.run(main(parser.parse_args()))
