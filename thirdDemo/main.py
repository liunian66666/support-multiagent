"""Local third-party SDK demo. Only demo guest sessions are issued here."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen


CS_BASE_URL = os.getenv("CS_BASE_URL", "http://localhost:8080").rstrip("/")
PORT = int(os.getenv("DEMO_PORT", "8090"))
PAGE = Path(__file__).with_name("index.html")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            content = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif self.path == "/get-token":
            try:
                request = Request(
                    CS_BASE_URL + "/api/auth/guest-token", data=b"{}",
                    headers={"Content-Type": "application/json"},
                )
                with urlopen(request, timeout=10) as response:
                    result = json.load(response)
                if result.get("code") != 0:
                    raise ValueError("guest login unavailable")
                content = json.dumps({
                    "token": result["data"]["token"],
                    "tenant_id": result["data"].get("tenant_id", "t_demo"),
                }).encode()
                self.send_response(200)
            except Exception:
                content = b'{"error":"guest login unavailable"}'
                self.send_response(503)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_error(404)
            return
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


if __name__ == "__main__":
    print(f"SDK demo: http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
