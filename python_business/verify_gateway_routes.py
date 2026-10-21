"""Read-only public route smoke test after switching the Nginx upstream."""

import os
import sys
import urllib.error
import urllib.request


BASE = os.getenv("GATEWAY_URL", "http://nginx")
EXPECT = {
    "/chat/pc.html": 200,
    "/admin/": 200,
    "/health": 200,
    "/metrics": 200,
    "/api/auth/tenants": 200,
    "/api/user/messages": 401,
    "/api/admin/menus/tree": 401,
    "/api/agent/order": 404,
    "/api/business/order": 410,
    "/py-api/health": 200,
}


def main():
    failed = False
    for path, expected in EXPECT.items():
        try:
            with urllib.request.urlopen(BASE + path, timeout=10) as response:
                status = response.status
                body = response.read(2048)
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read(2048)
        ok = status == expected
        if path == "/metrics":
            ok &= b"cs_http_requests_total" in body
        print(f"{path}: {status} {'ok' if ok else 'FAIL'}")
        failed |= not ok
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
