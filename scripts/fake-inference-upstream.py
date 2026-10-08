#!/usr/bin/env python3
"""Stand-in for a vLLM OpenAI-compatible server, for exercising the Caddy edge locally.

The local `inference.<zone>` route (caddy_routes entry with `inference_api: true`)
must let only /v1/* (with a Bearer header) and /health reach the upstream, and
answer 404 at the edge for everything else. Proving that needs an upstream that
answers EVERY path — so an edge 404 is distinguishable from an upstream 404 —
and that streams, so `flush_interval -1` can be observed. This is that upstream;
it never checks the token (vLLM does that in prod), it only reports whether one
arrived.

    python3 scripts/fake-inference-upstream.py [port]     # default 8001, binds 0.0.0.0

Caddy in the podman machine reaches it as host.containers.internal:<port>. Stdlib
only. Not a deploy artefact — a local test fixture.
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _seen_auth(self) -> bool:
        return self.headers.get("Authorization", "").startswith("Bearer ")

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/v1/models":
            self._json(
                200,
                {"object": "list", "data": [{"id": "fake-model"}], "upstream_saw_bearer": self._seen_auth()},
            )
            return
        # Every other path answers 200 with a marker: if a client ever sees this
        # body through Caddy, the edge allowlist has a hole.
        self._json(200, {"LEAK": "upstream reached", "path": self.path})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path == "/v1/chat/completions":
            # Three SSE chunks a second apart: through a flushing proxy the client
            # sees them arrive one at a time; through a buffering one, all at once.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for i in range(3):
                self.wfile.write(f"data: {json.dumps({'chunk': i, 't': time.time()})}\n\n".encode())
                self.wfile.flush()
                time.sleep(1)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        self._json(200, {"LEAK": "upstream reached", "path": self.path})

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write(f"upstream {self.address_string()} - {fmt % args}\n")


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"fake inference upstream on 0.0.0.0:{port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
