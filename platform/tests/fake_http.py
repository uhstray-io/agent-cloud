"""The one request handler base every fake HTTP server in the test suites derives from.

A handler that replies without reading the request body leaves unread bytes in the
socket's receive buffer; closing such a socket makes the kernel send a TCP RST instead
of a FIN, and the client sees "[Errno 54] Connection reset by peer" -- under a no_log
task, an unexplained authentication failure. It only surfaces under load (pytest-xdist),
so it cannot be left to each fake remembering. This base reads the whole declared body
before the do_* method runs, on every path, and serves it back from memory: handlers
keep calling self.rfile.read(...) exactly as before.

It also holds `refused_port()`, the one port a test aims at when it needs "nothing listens here",
and `serve()`, the one way a test starts a fixture server.
"""

import errno
import io
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


# serve_forever's select timeout: shutdown() waits out one poll, so the stdlib's 0.5 s would be paid on
# every server exit. Servers a test builds itself pass it too.
SHUTDOWN_POLL_SECONDS = 0.01

# How long a TLS client gets to finish its handshake before its connection is dropped.
TLS_HANDSHAKE_TIMEOUT = 10

# A wildcard bind is reached on loopback, so the URL names loopback.
_LOOPBACK_FOR = {"0.0.0.0": "127.0.0.1", "::": "::1"}


@contextmanager
def serve(handler, *, host="127.0.0.1", ssl_context=None):
    """A fixture server for `handler` on an ephemeral port of `host`, while the with-block runs; yields
    its URL ("http://127.0.0.1:<port>", or "https://..." when `ssl_context` is given). `host` may be a
    wildcard ("0.0.0.0", for a container that reaches the host) or IPv6 ("::1"). Threaded, with daemon
    request threads, and stopped and closed on exit. Each TLS connection does its handshake on its own
    request thread, so a client that connects and stalls holds up only itself."""
    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        # Default listen backlog is 5; a playbook's request bursts on a loaded machine got
        # "Connection reset by peer" (see test_scoped_publication.py).
        request_queue_size = 128
        daemon_threads = True

        def get_request(self):
            sock, address = self.socket.accept()
            if ssl_context is None:
                return sock, address
            # Wrapping the listening socket would run every handshake inside accept(), on the one
            # serve_forever thread: a silent client would block all requests and shutdown().
            sock.settimeout(TLS_HANDSHAKE_TIMEOUT)
            return ssl_context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), address

        def finish_request(self, request, client_address):
            if ssl_context is not None:
                try:
                    request.do_handshake()
                except OSError:  # ssl.SSLError and timeouts are OSError: a failed client, not a fixture error
                    return
                request.settimeout(None)  # the timeout bounds the handshake only
            super().finish_request(request, client_address)

    server = Server((host, 0), handler)
    url_host = _LOOPBACK_FOR.get(host, host)
    if ":" in url_host:
        url_host = f"[{url_host}]"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": SHUTDOWN_POLL_SECONDS}, daemon=True)
    thread.start()
    try:
        yield f"{'https' if ssl_context is not None else 'http'}://{url_host}:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
