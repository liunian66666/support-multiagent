"""Compare Go and FastAPI internal contracts without printing keys or chat text.

Run inside the business container during the parallel migration window.
"""

import json
import os
import sys
import urllib.request


def fetch(base, path):
    request = urllib.request.Request(
        base + path,
        headers={"X-Internal-Token": os.environ["INTERNAL_API_TOKEN"]},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def main():
    checks = (
        ("tenant-quota", "/api/agent/tenant-quota?tenant_id=t_demo"),
        ("model-config", "/api/agent/model-config?tenant_id=t_demo"),
    )
    failed = False
    for name, path in checks:
        go = fetch("http://go:8080", path)
        python = fetch(os.getenv("PYTHON_BUSINESS_API", "http://business:8001"), path)
        left, right = go.get("data") or {}, python.get("data") or {}
        differences = sorted(key for key in left.keys() | right.keys()
                             if left.get(key) != right.get(key))
        # Never print values: model-config includes plaintext model API keys.
        ok = go.get("code") == python.get("code") and not differences
        print(f"{name}: {'match' if ok else 'mismatch'}; fields={differences}")
        failed |= not ok
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
