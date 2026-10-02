"""The one request handler base every fake HTTP server in the test suites derives from.

A handler that replies without reading the request body leaves unread bytes in the
socket's receive buffer; closing such a socket makes the kernel send a TCP RST instead
of a FIN, and the client sees "[Errno 54] Connection reset by peer" -- under a no_log
task, an unexplained authentication failure. It only surfaces under load (pytest-xdist),
so it cannot be left to each fake remembering. This base reads the whole declared body
before the do_* method runs, on every path, and serves it back from memory: handlers
keep calling self.rfile.read(...) exactly as before.
"""

import io
from http.server import BaseHTTPRequestHandler


class DrainingHandler(BaseHTTPRequestHandler):
    """Reads the Content-Length body before dispatch, so no reply leaves bytes unread."""

    def parse_request(self):
        ok = super().parse_request()
        if ok:
            length = int(self.headers.get("Content-Length") or 0)
            self._socket_rfile = self.rfile
            self.rfile = io.BytesIO(self.rfile.read(length) if length > 0 else b"")
        return ok

    def handle_one_request(self):
        try:
            super().handle_one_request()
        finally:
            sock = self.__dict__.pop("_socket_rfile", None)
            if sock is not None:
                self.rfile = sock
