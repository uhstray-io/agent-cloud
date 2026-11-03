"""The one request handler base every fake HTTP server in the test suites derives from.

A handler that replies without reading the request body leaves unread bytes in the
socket's receive buffer; closing such a socket makes the kernel send a TCP RST instead
of a FIN, and the client sees "[Errno 54] Connection reset by peer" -- under a no_log
task, an unexplained authentication failure. It only surfaces under load (pytest-xdist),
so it cannot be left to each fake remembering. This base reads the whole declared body
before the do_* method runs, on every path, and serves it back from memory: handlers
keep calling self.rfile.read(...) exactly as before.

It also holds `refused_port()`, the one port a test aims at when it needs "nothing listens here".
"""

import errno
import io
import socket
from http.server import BaseHTTPRequestHandler

# Below every ephemeral range (Linux 32768-60999, macOS 49152-65535), so a bind to port 0 never
# returns it, and nothing listens on it: a connection is refused at once on both.
REFUSED_PORT = 1


def refused_port():
    """A loopback port that refuses connections and that no parallel test can be handed.

    A port freed by stopping a server or closing a socket is NOT that: a bind to port 0 in another
    xdist worker can be handed it, and the request meant to find nothing reaches that worker's
    server instead. One such request landed on the conformance test's upstream stub and failed
    it (docs/MISTAKES.md 2.33). Holding the port bound without listening does not work either:
    macOS then leaves the connect to time out instead of refusing it.
    """
    # Both loopbacks: a test that names `localhost` may reach either. A machine with no IPv6
    # loopback cannot resolve `localhost` to ::1, so that address is skipped there.
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            probe = socket.socket(family)
        except OSError:
            continue
        with probe:
            probe.settimeout(2)
            try:
                probe.connect((address, REFUSED_PORT))
            except ConnectionRefusedError:
                continue
            except OSError as error:
                if family == socket.AF_INET6 and error.errno in (errno.EADDRNOTAVAIL, errno.ENETUNREACH,
                                                                errno.EAFNOSUPPORT):
                    continue
                raise RuntimeError(f"[{address}]:{REFUSED_PORT} did not refuse a connection: {error}") from error
        raise RuntimeError(f"something listens on [{address}]:{REFUSED_PORT}; refused_port() needs it closed")
    return REFUSED_PORT


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
