# ruff: noqa: RUF001
"""Read-only diagnosis of the model gateway behind the live A/B arms.

Phase 6 needs a reachable chat model. This script answers, without spending any
inference tokens: which tenants exist, which ``api_base`` each one resolves to
(tenant custom config, else the platform default from ``t_admin``), and whether that
endpoint is reachable *from inside the container network* right now.

It performs no writes: the only calls are the same authenticated
``GET /api/agent/model-config`` the service already makes, plus a TCP connect.
"""

from __future__ import annotations

import asyncio
import json
import socket
from urllib.parse import urlparse

import httpx

TENANTS = ["t_admin", "t_demo", "t_shop", "t_live", "t_test"]


def reachable(url: str, timeout: float = 4.0) -> str:
    parsed = urlparse(url)
    host, port = parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return "no-host"
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return f"tcp-ok {host}:{port}"
    except OSError as exc:
        return f"FAIL {type(exc).__name__}: {str(exc)[:60]}"


async def main() -> None:
    from app.infra.config import BUSINESS_API, INTERNAL_API_TOKEN

    base = BUSINESS_API
    headers = {"X-Internal-Token": INTERNAL_API_TOKEN} if INTERNAL_API_TOKEN else {}
    print(f"BUSINESS_API={base}")
    async with httpx.AsyncClient(timeout=8) as client:
        for tenant in TENANTS:
            try:
                resp = await client.get(f"{base}/api/agent/model-config",
                                        params={"tenant_id": tenant}, headers=headers)
                payload = resp.json()
            except Exception as exc:
                print(f"{tenant:<8} config request failed: {type(exc).__name__} {str(exc)[:80]}")
                continue
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                print(f"{tenant:<8} code={payload.get('code') if isinstance(payload, dict) else '?'} "
                      f"no config (tenant may not exist)")
                continue
            chat_base = data.get("api_base") or ""
            emb_base = data.get("embedding", {}).get("api_base") if isinstance(data.get("embedding"), dict) else ""
            print(f"{tenant:<8} use_custom={data.get('use_custom')} model={data.get('model_name')!r} "
                  f"chat_base={chat_base or '(none)'} → {reachable(chat_base) if chat_base else 'n/a'} | "
                  f"emb_base={emb_base or '(none)'} → {reachable(emb_base) if emb_base else 'n/a'} | "
                  f"key_configured={bool(data.get('api_key'))}")
        # also dump the raw shape once, keys only, so the field names stay honest
        probe = await client.get(f"{base}/api/agent/model-config", params={"tenant_id": "t_demo"},
                                 headers=headers)
        body = probe.json()
        data = body.get("data") if isinstance(body, dict) else None
        print("keys:", sorted(data.keys()) if isinstance(data, dict) else json.dumps(body)[:160])


if __name__ == "__main__":
    asyncio.run(main())
