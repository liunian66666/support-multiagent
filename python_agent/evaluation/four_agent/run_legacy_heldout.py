"""Old-path ("off") arm of the phase-6 comparison, collected in-process on a live model.

Why this file exists: the frozen off-baseline (`ho_off_r4.jsonl`) was collected through the
gateway, which drives the tenant's own chat endpoint — and that endpoint is down. So until
today there was no way to answer "new graph vs old graph, same question set, same model".
This harness runs the REAL legacy pipeline (`app.api.chat.agent_chat`, four-agent switch left
off) over the same 36 held-out cases, with only one substitution: the chat model client.

What is deliberately real, and what is not:

* Real: routing (`decide_turn`), shortcuts/compound plan, FAQ retrieval against Qdrant,
  `approved_evidence` publication filters, order ownership through the business API,
  `finalize_faq_reply` / `mark_demo_faq_reply` post-processing, the bridge checkpoint,
  `session_meta` / `kb_hits` / `token_usage` / `agent_trace_runs` writes, and the trusted
  identity header check (`X-Internal-Token` + `X-Tenant-Id`, straight from `.env`).
* Not real: the chat model. `--model-name` rebinds `get_tenant_model` at the four legacy
  import sites to the platform key/base_url plus that model id, because DashScope grants
  free quota per model and the configured one is exhausted. Every row therefore carries
  `comparable=false`: this arm is comparable with the new-path arm collected with the SAME
  model name, and with nothing else — never with the qwen3.8-flash-next baseline.
* Left untouched: the tenant's stored model config. We patch process-local attributes only.

Cleanup: every thread id is throwaway (`t_demo:leg-…`). After the run, the bridge
checkpoints, `session_meta`, `kb_hits`, `token_usage` and `agent_trace_runs` rows for those
threads are deleted, and the residue count is printed so a leak is visible rather than silent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

TENANT = "t_demo"
MODEL_PATCH_SITES = ("app.agent.faq_agent", "app.agent.intake_agent",
                     "app.services.compound", "app.agent.builder")

# reuse the ruler and the gold loader from the new-path collector: two arms scored by two
# different rulers would be the exact mistake recorded in 实际效果报告 §2.
try:  # container layout: -w /work/python_agent, PYTHONPATH=/work:/work/python_agent
    from evaluation.four_agent.mem_corpus import fingerprint, turn_row
    from evaluation.four_agent.run_ab_live import GOLD, literal_score, norm  # noqa: F401
except ImportError:  # invoked as a script from python_agent/ with /work on the path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from evaluation.four_agent.mem_corpus import fingerprint, turn_row  # type: ignore
    from evaluation.four_agent.run_ab_live import GOLD, literal_score, norm  # type: ignore


def load_cases(limit: int) -> list[dict]:
    cases = [json.loads(line) for line in GOLD.read_text(encoding="utf-8").splitlines() if line.strip()]
    return cases[:limit]


async def build_model(app: Any, name: str) -> Any:
    """Platform key/base_url + a different model id — the only substitution this arm makes."""
    from langchain.chat_models import init_chat_model

    from app.agent.llm_config import get_platform_model_config

    cfg = (await get_platform_model_config(app)).get("model_config", {})
    key, base = cfg.get("api_key") or "", cfg.get("api_base") or ""
    if not key or not base:
        raise SystemExit("平台 key/base 取不到，旧路径臂无法跑")
    model_options = {"reasoning_effort": "low"} if name == "glm-5.3" else {}
    model = init_chat_model(model=name, model_provider="openai", temperature=0,
                            max_tokens=1024, timeout=90, api_key=key, base_url=base,
                            **model_options)
    if name.startswith("qwen3.8"):
        model = model.bind(extra_body={"enable_thinking": False})
    return model


async def scaffold() -> tuple[Any, Any, Any]:
    """The app.state the lifespan would build, minus the background workers.

    Deliberately NOT entering `app.lifespan`: it starts distill/summary/cleanup loops that
    use the tenant model and would write rows outside the measurement.
    """
    import httpx
    import psycopg
    from psycopg_pool import AsyncConnectionPool
    from qdrant_client import QdrantClient

    from app.agent import tools as business_tools
    from app.agent.faq_bridge import build_faq_bridge
    from app.infra.checkpoint import SafeAsyncPostgresSaver
    from app.infra.config import POSTGRES_DSN, QDRANT_URL
    from app.infra.context import pending_ticket_var, tenant_var, thread_var

    client = QdrantClient(url=QDRANT_URL)
    pool = AsyncConnectionPool(POSTGRES_DSN, min_size=1, max_size=4, open=False,
                               kwargs={"autocommit": True})
    await pool.open()
    ck_conn = await psycopg.AsyncConnection.connect(POSTGRES_DSN, autocommit=True)
    checkpointer = SafeAsyncPostgresSaver(ck_conn, dsn=POSTGRES_DSN)
    await checkpointer.setup()
    http = httpx.AsyncClient(timeout=60)
    app = SimpleNamespace(state=SimpleNamespace(
        http_client=http, qdrant_client=client, db_pool=pool, vec_dim=0,
        tenant_stores={}, tenant_retrievers={}, tenant_agents={}, faq_agents={},
        intake_agents={}, checkpointer=checkpointer, faq_bridge=build_faq_bridge(checkpointer)))
    business_tools.set_app(app, logging.getLogger("legacy-eval"), tenant_var,
                           pending_ticket_var, thread_var)
    return app, http, (pool, ck_conn, checkpointer)


async def cleanup(app: Any, threads: list[str]) -> tuple[dict[str, int], dict[str, dict]]:
    """Read the per-thread accounting back out, then delete only what this run created.

    The route lives in ``agent_trace_runs``, not in the response, and the wrapper resets the
    trace contextvar before we get control back — so the honest way to report "which
    specialist answered this question on the old path" is to read the row the service wrote.
    Token cost comes from ``token_usage`` for the same reason. Residue counts are printed so
    a leak is visible instead of silent.
    """
    out = {"checkpoints": 0, "session_meta": 0, "kb_hits": 0, "token_usage": 0,
           "agent_trace_runs": 0}
    per_thread: dict[str, dict] = {}
    for thread in threads:
        try:
            await app.state.checkpointer.adelete_thread(thread)
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask the result
            print(f"[cleanup] adelete_thread({thread}) failed: {exc}", flush=True)
    async with app.state.db_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT thread_id, route, specialist, status, duration_ms FROM agent_trace_runs "
            "WHERE thread_id = ANY(%s)", (threads,))
        for thread_id, route, specialist, status, duration in await cur.fetchall():
            per_thread.setdefault(thread_id, {"route": route or "", "specialist": specialist or "",
                                              "trace_status": status or "",
                                              "trace_duration_ms": duration, "total_tokens": 0})
        cur = await conn.execute(
            "SELECT thread_id, sum(total_tokens) FROM token_usage WHERE thread_id = ANY(%s) "
            "GROUP BY thread_id", (threads,))
        for thread_id, tokens in await cur.fetchall():
            per_thread.setdefault(thread_id, {"route": "", "specialist": "", "trace_status": "",
                                              "trace_duration_ms": None,
                                              "total_tokens": 0})["total_tokens"] = int(tokens or 0)
        # every one of these tables carries thread_id (verified against the live schema);
        # checkpoint_blobs / checkpoint_writes go with adelete_thread above.
        for table in ("session_meta", "kb_hits", "token_usage", "agent_trace_runs"):
            for thread in threads:
                await conn.execute(f"DELETE FROM {table} WHERE thread_id=%s", (thread,))
        for table in out:
            cur = await conn.execute(f"SELECT count(*) FROM {table} WHERE thread_id = ANY(%s)",
                                     (threads,))
            out[table] = (await cur.fetchone())[0]
    return out, per_thread


async def run(args: argparse.Namespace) -> list[dict]:
    from app.api import chat as chat_api
    from app.schemas import ChatRequest

    # 逐条落盘：这一臂一跑就是十几分钟，最后一行统计崩掉不该把整批数据丢掉
    # （上一次 legacy mem 臂就是这么丢的——per_turn 少了一个 key，全部白跑）。
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("", encoding="utf-8")

    def persist(record: dict) -> None:
        collected.append(record)
        with out.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    app, http, _stack = await scaffold()
    model = await build_model(app, args.model_name)
    for site in MODEL_PATCH_SITES:
        module = __import__(site, fromlist=["get_tenant_model"])
        setattr(module, "get_tenant_model", lambda *_a, **_k: asyncio.sleep(0, result=model))
    print(f"旧路径臂（arm={args.arm}）：模型换成 {args.model_name}（平台 key），"
          f"patch 位点 {MODEL_PATCH_SITES}", flush=True)

    from app.infra.config import INTERNAL_API_TOKEN
    if not INTERNAL_API_TOKEN:
        raise SystemExit(".env 缺 INTERNAL_API_TOKEN——不伪造鉴权，宁可不打这一臂")

    threads, collected = [], []

    async def ask(thread: str, message: str) -> dict:
        started = time.perf_counter()
        request = SimpleNamespace(headers={"X-Internal-Token": INTERNAL_API_TOKEN,
                                           "X-Tenant-Id": TENANT,
                                           "X-Trace-Id": f"legacy-{thread[-12:]}"},
                                  app=app)
        try:
            resp = await chat_api.agent_chat(ChatRequest(thread_id=thread, user_message=message),
                                             request)
            return {"answer": resp.content, "success": resp.success,
                    "is_interrupt": resp.is_interrupt, "error": "",
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
        except Exception as exc:  # noqa: BLE001 - one bad case must not end the arm
            return {"answer": "", "success": False, "is_interrupt": False,
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1)}

    try:
        if args.arm == "heldout":
            for case in load_cases(args.limit):
                thread = f"{TENANT}:leg-{case['id']}-{uuid.uuid4().hex[:6]}"
                threads.append(thread)
                got = await ask(thread, case["query"])
                record = {"id": case["id"], "category": case.get("category"),
                          "query": case["query"], "path": "legacy", "arm": "heldout",
                          "model": "platform-substituted", "model_name": args.model_name,
                          "comparable": False, "thread": thread,
                          **got, **literal_score(got["answer"], case)}
                persist(record)
                print(f"{record['id']:<10} pass={record.get('literal_pass')} "
                      f"forbidden={record.get('forbidden_hit')} "
                      f"missing={record.get('required_missing')} {record['latency_ms']}ms", flush=True)
        else:
            # 长对话臂：与 run_ab_live.py mem 同一批语料、同一个"含针就记 recall"的判据，
            # 区别只在存储——旧路径的记忆是 memory_messages + session_summaries + faq_bridge，
            # 新图的是水位线 + 图内通道。同一模型下比这两套，才是阶段4 想证明的那件事。
            from evaluation.memory_ab_live import cases as long_cases

            for case in long_cases()[: args.limit]:
                thread = f"{TENANT}:legmem-{case['id']}-{uuid.uuid4().hex[:6]}"
                threads.append(thread)
                per_turn = []
                for index, message in enumerate(case["turns"]):
                    got = await ask(thread, message)
                    # 与新臂同一形状的逐轮转写：原话全文 + 答复开头 + 该轮墙钟。
                    # 旧臂以前只留 {turn,error,latency_ms}，探针失手时"这轮到底问过什么"无法复核。
                    per_turn.append(turn_row(index, message, got["answer"],
                                             latency_ms=got["latency_ms"],
                                             extra={"error": got["error"],
                                                    "success": got["success"],
                                                    "is_interrupt": got["is_interrupt"]}))
                probe = await ask(thread, case["probe"])
                text = norm(probe["answer"] or "")
                record = {"id": case["id"], "needle_ref": case["needles"]["ref"],
                          "needle_company": case["needles"]["company"],
                          "probe": case["probe"], "path": "legacy", "arm": "mem",
                          "model": "platform-substituted", "model_name": args.model_name,
                          "comparable": False, "thread": thread,
                          "script_sha": fingerprint(case),
                          "probe_latency_ms": probe["latency_ms"],
                          "answer": probe["answer"], "expect": case["expect"],
                          "containment": [norm(term) in text for term in case["expect"]],
                          "all_needles_recalled": all(norm(term) in text
                                                      for term in case["expect"]),
                          "turns": len(case["turns"]) + 1, "per_turn": per_turn,
                          "latency_ms": round(sum(t.get("latency_ms", 0) for t in per_turn)
                                              + probe["latency_ms"], 1)}
                persist(record)
                print(f"{record['id']:<8} recall={record['containment']} "
                      f"总耗时={record['latency_ms']}ms", flush=True)
        residue, per_thread = await cleanup(app, threads)
        for record in collected:
            # route/specialist/token cost come off the rows the service itself wrote, not
            # off a contextvar the wrapper already reset.
            info = per_thread.get(record["thread"], {})
            record["route"] = info.get("route", "")
            record["specialist"] = info.get("specialist", "")
            record["trace_status"] = info.get("trace_status", "")
            record["usage"] = {"total_tokens": info.get("total_tokens", 0)}
        print("[cleanup] 残留行数:", json.dumps(residue, ensure_ascii=False), flush=True)
        if any(value for value in residue.values()):
            print("[cleanup] 仍有残留——这一臂的账不能算干净", file=__import__("sys").stderr)
    finally:
        pool, ck_conn, _ = _stack
        await ck_conn.close()
        await pool.close()
        await http.aclose()
    return collected


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=["heldout", "mem"], default="heldout",
                        help="heldout=36 题留出集；mem=10 条长对话（同 memory_ab_live 的语料与判据）")
    parser.add_argument("--limit", type=int, default=36)
    parser.add_argument("--model-name", dest="model_name", required=True,
                        help="模型 id（平台 key + base_url 不变）。必带，且行内 comparable=false")
    parser.add_argument("--out", default="/tmp/legacy.jsonl")
    args = parser.parse_args()
    collected = asyncio.run(run(args))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in collected),
                   encoding="utf-8")
    if args.arm == "heldout":
        score = sum(1 for r in collected if r.get("literal_pass"))
        print(f"legacy {args.arm}: {len(collected)} 例，字面判定通过 {score}，原始数据 → {out}")
    else:
        score = sum(1 for r in collected if r.get("all_needles_recalled"))
        print(f"legacy {args.arm}: {len(collected)} 条长对话，针全部命中 {score}，原始数据 → {out}")
