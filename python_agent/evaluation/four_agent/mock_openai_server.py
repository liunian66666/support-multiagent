# 中文文案含全角标点
"""A deterministic OpenAI-compatible chat endpoint, so the live arms can be wired end-to-end.

Why this exists: every model-free check in this project drives the graph with **in-process**
fakes (`agent/fakes.py`). That proves the graph logic, but it has never proved the transport:
that `run_ab_live.py` can actually complete an HTTP round trip against a real
OpenAI-compatible server, that tool calls survive JSON serialisation, that `usage` lands in
the billing channel, and that the collected file is readable by the diff gate. The real
gateway is blocked (report §8: tenant endpoint refuses, platform key 403 — 15 completion
probes), so the only way to de-risk that step today is to serve a model stand-in over HTTP.

What this is **not**: it is not a language model. The answer text is copied out of the tool
payload, so nothing here measures wording quality, and no number from a `custom` run may be
compared with the frozen baseline. The collector stamps every row `comparable=False` for
exactly that reason.

The server is deliberately stateless and schema-driven: it reads the `tools` array of the
incoming request, decides which tool the "model" would call, and builds the arguments from
that tool's own JSON schema — so it keeps working if a parameter is renamed.

    python python_agent/evaluation/four_agent/mock_openai_server.py --port 8123     # standalone
    # …or let the collector start it in-process: run_ab_live.py heldout --model custom --mock
"""

# NOTE: deliberately no `from __future__ import annotations` here. With PEP 563 the FastAPI
# route's `Request` parameter becomes the *string* "Request", which FastAPI cannot resolve to
# the class when it is imported inside the function body — so it silently reinterprets it as a
# required **query** parameter and answers every chat completion with 422
# ({"loc":["query","raw"]}). Measured, not guessed: the first mock run failed the whole arm
# with `UnprocessableEntityError: Error code: 422` while the graph itself stayed healthy.
import argparse
import json
import os
import re
import threading
import time
from typing import Any

# What an *order* question looks like: an order id, or the customer pointing at their own
# order. "双十一发货时间会变吗" is a policy question even though it says 发货 — a real model
# tells those apart, and this stand-in has to be explicit about the rule instead of keyword-guessing.
FAQTION_ORDER = re.compile(r"(ORD-[A-Za-z0-9-]+|TB\d+|JD\d+|PDD\d+|我的订单|我这一单|这单|订单号|我的快递)", re.I)
POLICY_MARK = re.compile(r"(退货|退款|换货|运费|邮费|付邮|包邮|发票|保修|售后|政策|发货|顺延|节假日"
                         r"|几天|多久|多少|几块|什么时候|期限|门槛)")

STATS: dict[str, Any] = {"requests": 0, "tool_calls": 0, "answers": 0, "errors": 0,
                         "last_error": "", "tools_seen": {}, "max_prompt_chars": 0,
                         # (role, has_tool_calls) of the tail of the last request: the only way to
                         # see whether the caller echoes assistant tool calls back, which is what
                         # lets the mock stop delegating.
                         "last_roles": [], "last_answer_tools": [],
                         # the last 24 decisions, so a delegation loop can be read off the ledger
                         # instead of guessed: (how many messages, which tools were offered, what
                         # the stand-in chose)
                         "history": []}
_LOCK = threading.Lock()


def _note_tool(name: str) -> None:
    with _LOCK:
        STATS["tools_seen"][name] = STATS["tools_seen"].get(name, 0) + 1


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # multipart content blocks
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content or "")


def _last_user(messages: list[dict]) -> str:
    return next((_text_of(m.get("content")) for m in reversed(messages)
                 if m.get("role") == "user"), "")


def _called_names(messages: list[dict]) -> set[str]:
    """Which tools this conversation already used — read back from the echoed assistant turns."""
    out: set[str] = set()
    for msg in messages:
        for call in (msg.get("tool_calls") or []):
            name = ((call.get("function") or {}).get("name")) or call.get("name")
            if name:
                out.add(str(name))
    return out


def _tool_payload(messages: list[dict]) -> str:
    """Everything the tools returned so far, unwrapped from the JSON envelope when there is one.

    All of them, not just the last: a compound question delegates twice, and an answer that
    quotes only the final payload would look like it dropped the first half.
    """
    found: list[str] = []
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        raw = _text_of(msg.get("content")).strip()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict):
            for key in ("answer", "text", "finding", "result"):
                if data.get(key):
                    raw = str(data[key])
                    break
        if raw and raw not in found:
            found.append(raw)
    return "；".join(found)


def _arguments_for(spec: dict, question: str) -> str:
    """Fill the tool's own required string parameter with the question — never guess names."""
    params = spec.get("function", {}).get("parameters") or {}
    props = params.get("properties") or {}
    required = params.get("required") or list(props)
    args: dict[str, Any] = {}
    for key in required:
        schema = props.get(key) or {}
        kind = schema.get("type", "string")
        if kind == "string":
            args[key] = question[:300] or "请查询"
        elif kind == "integer":
            args[key] = 3
        elif kind == "boolean":
            args[key] = False
        elif kind in {"array", "object"}:
            args[key] = [] if kind == "array" else {}
    return json.dumps(args, ensure_ascii=False)


def _specs(request: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for spec in (request.get("tools") or []):
        name = (spec.get("function") or {}).get("name") or spec.get("name")
        if name:
            out[str(name)] = spec
    return out


def decide(request: dict) -> dict:
    """The whole 'model': pick a tool the caller offered, else summarise what tools returned."""
    messages = request.get("messages") or []
    question = _last_user(messages)
    specs = _specs(request)
    for name in specs:
        _note_tool(name)
    used = _called_names(messages)
    pending = [name for name in specs if name not in used]
    with _LOCK:
        STATS["max_prompt_chars"] = max(STATS["max_prompt_chars"],
                                        sum(len(_text_of(m.get("content"))) for m in messages))
        STATS["last_roles"] = [(str(m.get("role")), bool(m.get("tool_calls"))) for m in messages][-8:]
        STATS["last_answer_tools"] = sorted(used)

    def choose(candidates: list[str]) -> str | None:
        """First tool the question actually calls for that has not been used yet.

        Deliberately no "otherwise call some other pending tool" fallback: that fallback made
        the mock delegate forever (faq → order → general → …), and the parent graph hit its
        recursion limit with zero answers. A stand-in model has to terminate, or it cannot be
        used to test a bounded loop.
        """
        for candidate in candidates:
            if candidate in pending:
                return candidate
        return None

    order_names = sorted({*(specs)} & {"lookup_my_order", "list_my_orders", "order_lookup"})
    if "search_kb" in specs:  # the FAQ delegate
        chosen = choose(["search_kb"])
    elif order_names:  # the order delegate
        chosen = choose(["lookup_my_order", "order_lookup"] if FAQTION_ORDER.search(question)
                        else ["list_my_orders", "lookup_my_order", "order_lookup"])
    elif "consult_faq" in specs or "consult_order" in specs:  # the supervisor
        wanted = []
        if POLICY_MARK.search(question):
            wanted.append("consult_faq")
        if FAQTION_ORDER.search(question):
            wanted.append("consult_order")
        # No keyword hit: ask the FAQ agent first anyway — in this corpus an unrecognised
        # customer question is far more likely to be policy than a greeting, and delegating
        # to general would silently skip the retrieval path this arm exists to exercise.
        chosen = choose(wanted) if wanted else choose(["consult_faq", "consult_general"])
    else:
        chosen = None
    with _LOCK:
        STATS["history"].append([len(messages), "+".join(sorted(specs)) or "-", chosen or "answer"])
        del STATS["history"][:-24]

    if chosen:
        call_id = f"call-{int(time.time() * 1000) % 1000000}-{chosen}"
        with _LOCK:
            STATS["tool_calls"] += 1
        message = {"role": "assistant", "content": None,
                   "tool_calls": [{"id": call_id, "type": "function",
                                   "function": {"name": chosen,
                                                "arguments": _arguments_for(specs[chosen], question)}}]}
        return {"message": message, "finish_reason": "tool_calls"}

    payload = _tool_payload(messages)
    body = (f"根据本轮核验结果：{payload[:260]}" if payload else
            "抱歉，本轮没有可核验的信息，无法给出结论。")
    with _LOCK:
        STATS["answers"] += 1
    return {"message": {"role": "assistant", "content": body}, "finish_reason": "stop"}


def completion(request: dict) -> dict:
    latency_ms = float(os.getenv("MOCK_LATENCY_MS", "40"))
    time.sleep(latency_ms / 1000.0)
    decided = decide(request)
    prompt_chars = sum(len(_text_of(m.get("content"))) for m in (request.get("messages") or []))
    completion_chars = len(_text_of(decided["message"].get("content")) or
                           json.dumps(decided["message"].get("tool_calls"), ensure_ascii=False))
    # ~4 chars per token: a fake tokenizer, but a *consistent* one, so token-cost accounting
    # (the thing this arm is measuring) is exercised with realistic magnitudes.
    prompt_tokens = max(1, prompt_chars // 4)
    completion_tokens = max(1, completion_chars // 4)
    return {"id": f"chatcmpl-mock-{int(time.time() * 1000)}", "object": "chat.completion",
            "created": int(time.time()), "model": request.get("model", "mock-support-model"),
            "choices": [{"index": 0, "message": decided["message"],
                         "finish_reason": decided["finish_reason"]}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                      "total_tokens": prompt_tokens + completion_tokens}}


def build_app():
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    app = FastAPI(title="mock-openai")

    @app.get("/v1/models")
    async def models() -> dict:
        return {"object": "list", "data": [{"id": "mock-support-model", "object": "model"}]}

    @app.get("/mock/stats")
    async def stats() -> dict:
        with _LOCK:
            return dict(STATS)

    @app.post("/v1/chat/completions")
    async def completions(raw: Request) -> JSONResponse:
        with _LOCK:
            STATS["requests"] += 1
        try:
            body = await raw.json()
        except Exception as exc:  # noqa: BLE001 - the point is to report, not to crash
            with _LOCK:
                STATS["errors"] += 1
                STATS["last_error"] = f"{type(exc).__name__}: {exc}"[:160]
            return JSONResponse({"error": {"message": "invalid json body"}}, status_code=400)
        return JSONResponse(completion(body))

    return app


def serve_in_background(port: int = 8123) -> Any:
    """Run uvicorn in a daemon thread; return the server so the caller can stop it."""
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(build_app(), host="127.0.0.1", port=port,
                                           log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True, name="mock-openai")
    thread.start()
    deadline = time.time() + 15
    import httpx
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/v1/models", timeout=1).status_code == 200:
                return server
        except Exception:  # noqa: BLE001 - still booting
            time.sleep(0.1)
    raise SystemExit(f"mock server did not come up on port {port}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="deterministic OpenAI-compatible endpoint")
    parser.add_argument("--port", type=int, default=8123)
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(build_app(), host="127.0.0.1", port=args.port, log_level="info")
