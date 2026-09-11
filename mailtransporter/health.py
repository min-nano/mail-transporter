"""Liveness signal for the watcher, consumed by the MIG health check.

The watcher is a single long-running loop that blocks in IMAP IDLE, sleeps,
or waits for the forwarder to answer.  Before each blocking operation it
calls :meth:`Heartbeat.touch` with the longest time that operation may
legitimately take; ``/healthz`` answers 200 only while that budget has not
run out.  A stuck process therefore turns unhealthy shortly after the
budget expires, while a stopped VM simply refuses the connection.
"""

from __future__ import annotations

import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

log = logging.getLogger(__name__)


class Heartbeat:
    def __init__(self, *, startup_grace: float = 300.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._deadline = clock() + startup_grace
        self._note = "starting"

    def touch(self, ttl: float, note: str = "") -> None:
        """Declare the loop alive for another ``ttl`` seconds."""
        with self._lock:
            self._deadline = self._clock() + max(ttl, 0.0)
            self._note = note

    def ok(self) -> bool:
        with self._lock:
            return self._clock() <= self._deadline

    def status(self) -> dict:
        with self._lock:
            return {
                "healthy": self._clock() <= self._deadline,
                "seconds_until_stale": round(self._deadline - self._clock(), 1),
                "note": self._note,
            }


def serve(heartbeat: Heartbeat, port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    """Start the health endpoint on a daemon thread and return the server."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            if self.path.split("?", 1)[0] != "/healthz":
                self.send_error(404)
                return
            status = heartbeat.status()
            body = str(status).encode()
            self.send_response(200 if status["healthy"] else 503)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002 - quiet health probes
            return

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="healthz", daemon=True)
    thread.start()
    log.info("Health endpoint listening on %s:%d/healthz", host, server.server_port)
    return server
