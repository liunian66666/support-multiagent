"""Request parsing compatible with the former Go JSON binder."""

import json

from fastapi import Request


async def body_json(request: Request):
    try:
        raw = await request.body()
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
