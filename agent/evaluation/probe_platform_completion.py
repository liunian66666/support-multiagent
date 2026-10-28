# ruff: noqa: RUF001
"""Can a chat completion actually be produced right now? One call per source.

``probe_model_gateway.py`` only opens a TCP socket, which is not enough: a reachable
endpoint can still refuse every request with a quota or key error, and an unreachable
one tells you nothing about the other tenant. Phase 6 needs to know which case it is
before spending 36 + 10 live runs, so this script makes exactly one minimal completion
per requested model source and reports the outcome verbatim.

Read-only by construction: model config comes from the same authenticated
``GET /api/agent/model-config`` the service already makes; no thread is created, no
message is persisted, no ``kb_hits`` row is written, no collection is touched, and the
PG pool and Qdrant client are never built. ``get_tenant_model_config`` only needs
``app.state.http_client``, so the app stand-in is one ``SimpleNamespace``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

# Same tenant as run_ab_live.py: the held-out and long-chat arms are keyed to t_demo's
# knowledge base and orders, so the probe must ask about the model that arm would use.
TENANT = "t_demo"


async def try_one(label: str, model: object) -> dict:
    from langchain_core.messages import HumanMessage

    started = time.perf_counter()
    try:
        reply = await model.ainvoke([HumanMessage(content="只回复两个字：可用")])  # type: ignore[attr-defined]
        elapsed = round((time.perf_counter() - started) * 1000)
        usage = getattr(reply, "usage_metadata", None) or {}
        return {"source": label, "ok": True, **_ts(), "latency_ms": elapsed,
                "text": str(reply.content)[:80],
                "tokens": {key: usage.get(key) for key in
                           ("input_tokens", "output_tokens", "total_tokens")}}
    except Exception as exc:  # noqa: BLE001 - reporting the real failure is the point
        return {"source": label, "ok": False, **_ts(),
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "error": f"{type(exc).__name__}: {str(exc)[:220]}"}


def _ts() -> dict:
    """Wall clock on every row: gateway availability is a claim about *when*, and an
    appended JSONL without timestamps can only be dated by file mtime."""
    return {"ts": datetime.now().astimezone().isoformat(timespec="seconds")}


async def list_models(http, label: str, base: str, key: str) -> dict:
    """Ask the gateway which models this key can even see — GET /models, zero inference.

    Worth a separate call because the alt-name probes above guessed. A 404 "Model not
    exist" says the name was never valid for this key; the model list says which names
    *are*, so a following completion probe is targeted instead of another coin flip.
    Kept in its own artifact so it can't inflate the completion ledger's row count.
    """
    url = base.rstrip("/") + "/models"
    started = time.perf_counter()
    try:
        resp = await http.get(url, headers={"Authorization": f"Bearer {key}"})
        ids: list[str] = []
        try:
            payload = resp.json()
            items = payload.get("data", payload) if isinstance(payload, dict) else payload
            ids = sorted({str(i.get("id")) for i in items if isinstance(i, dict) and i.get("id")})
        except Exception:  # noqa: BLE001 - a non-JSON error page is still a verdict
            ids = []
        return {"source": label, "ok": resp.status_code < 400, "url": url, **_ts(),
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "status": resp.status_code, "count": len(ids), "models": ids,
                "body": "" if resp.status_code < 400 else resp.text[:200]}
    except Exception as exc:  # noqa: BLE001
        return {"source": label, "ok": False, "url": url, **_ts(),
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


async def main(args: argparse.Namespace) -> None:
    import httpx

    from app.agent.llm_config import get_platform_model, get_tenant_model

    http = httpx.AsyncClient(timeout=30)
    app = SimpleNamespace(state=SimpleNamespace(http_client=http))
    rows = []
    listed: list[dict] = []
    try:
        if args.list_models:
            # Both endpoints, so "the key sees nothing" and "the tenant host is down"
            # stay two different answers instead of one merged failure.
            cfg = (await _platform_config(app)).get("model_config", {})
            if cfg.get("api_key") and cfg.get("api_base"):
                listed.append(await list_models(http, "list:platform", cfg["api_base"], cfg["api_key"]))
                if args.auto_probe_listed:
                    names = listed[0].get("models", [])[: args.auto_probe_listed]
                    args.try_models = ",".join(names)
                    print(f"--auto-probe-listed → 用列到的前 {len(names)} 个名字补全：{names}", flush=True)
            tcfg = (await _tenant_config(app)).get("model_config", {})
            if tcfg.get("api_base"):
                listed.append(await list_models(http, f"list:tenant:{TENANT}",
                                                tcfg["api_base"], tcfg.get("api_key") or ""))
            for row in listed:
                print(f"{row['source']:<20} ok={row['ok']} status={row.get('status', '')} "
                      f"count={row.get('count', 0)} {row.get('error') or row.get('models')}", flush=True)
        sources = []
        if args.source in {"tenant", "both"}:
            sources.append((f"tenant:{TENANT}", lambda: get_tenant_model(app, TENANT)))
        if args.source in {"platform", "both"}:
            sources.append(("platform:t_admin", lambda: get_platform_model(app)))
        if args.try_models:
            # DashScope grants the free tier per model, so one model's 403 does not
            # necessarily bar the others under the same key. Reusing t_admin's key and
            # base_url with a different model name is the cheapest way to find out
            # whether phase 6 is blocked account-wide or just on the configured model.
            # Any arm that works this way is model-incomparable with the frozen
            # baseline (qwen3.8-flash-next / source=custom) and must be labelled so.
            cfg = (await _platform_config(app)).get("model_config", {})
            key, base = cfg.get("api_key") or "", cfg.get("api_base") or ""
            if not key or not base:
                print("t_admin 缺 api_key/api_base，跳过 --try-model")
            else:
                from langchain.chat_models import init_chat_model

                for name in [item.strip() for item in args.try_models.split(",") if item.strip()]:
                    model = init_chat_model(model=name, model_provider="openai", temperature=0,
                                            max_tokens=64, timeout=30, api_key=key, base_url=base)
                    sources.append((f"alt:{name}", lambda m=model: m))
        for label, build in sources:
            try:
                model = await _resolve(build)
            except Exception as exc:  # noqa: BLE001
                rows.append({"source": label, "ok": False, **_ts(), "latency_ms": 0,
                             "error": f"config resolution failed: {type(exc).__name__}: {str(exc)[:180]}"})
            else:
                rows.append(await try_one(label, model))
            latest = rows[-1]
            print(f"{latest['source']:<24} ok={latest['ok']} {latest['latency_ms']}ms "
                  f"{latest.get('text') or latest.get('error')}", flush=True)
    finally:
        await http.aclose()

    if args.list_out and listed:
        out = Path(args.list_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as handle:
            for row in listed:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"appended {len(listed)} model-list row(s) → {out}")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"appended {len(rows)} row(s) → {out}")


async def _resolve(build):
    """Accept either an awaitable factory (tenant/platform) or a ready model object."""
    resolved = build()
    if asyncio.iscoroutine(resolved):
        return await resolved
    return resolved


async def _platform_config(app) -> dict:
    from app.agent.llm_config import get_platform_model_config

    return await get_platform_model_config(app)


async def _tenant_config(app) -> dict:
    from app.agent.llm_config import get_tenant_model_config

    return await get_tenant_model_config(app, TENANT)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=["tenant", "platform", "both", "none"], default="both",
                        help="'none' when only --try-model probes are wanted")
    parser.add_argument("--try-model", dest="try_models", default="",
                        help="comma separated model names to probe with t_admin's key/base "
                             "(a hit is model-incomparable with the frozen baseline)")
    parser.add_argument("--list-models", dest="list_models", action="store_true",
                        help="GET /models on the platform and tenant gateways first (read-only, "
                             "zero inference); stops the alt-name guessing loop")
    parser.add_argument("--auto-probe-listed", dest="auto_probe_listed", type=int, default=0,
                        help="after --list-models, send one minimal completion to the first N "
                             "listed ids instead of hand-picked names")
    parser.add_argument("--list-out", dest="list_out", default="",
                        help="append the model-list rows here (kept away from --out on purpose: "
                             "that ledger counts completions, and its row count is a claim)")
    parser.add_argument("--out", default="", help="append one JSONL row per probe")
    asyncio.run(main(parser.parse_args()))
