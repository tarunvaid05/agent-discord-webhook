"""Local HTTP server standing in for a Discord webhook endpoint."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeDiscord:
    def __init__(self, script: list[int] | None = None, retry_after: float = 1.0) -> None:
        self.script = list(script or [])
        self.retry_after = retry_after
        self.requests: list[dict] = []
        self.url = ""
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> FakeDiscord:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                try:
                    fake.requests.append(json.loads(body))
                except ValueError:
                    fake.requests.append({"raw": body.decode(errors="replace")})
                status = fake.script.pop(0) if fake.script else 204
                if status == -1:  # malformed response: client raises http.client.BadStatusLine
                    self.wfile.write(b"garbage\r\n\r\n")
                    self.close_connection = True
                    return
                self.send_response(status)
                if status == 429:
                    payload = json.dumps({"retry_after": fake.retry_after}).encode()
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                else:
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def log_message(self, *args: object) -> None:
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        host, port = self._server.server_address[:2]
        self.url = f"http://{host}:{port}/api/webhooks/123/secret-token"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()
