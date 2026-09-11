from __future__ import annotations

import urllib.error
import urllib.request

from mailtransporter.health import Heartbeat, serve

from conftest import FakeClock


def test_heartbeat_expires_after_ttl():
    clock = FakeClock()
    hb = Heartbeat(startup_grace=100, clock=clock)
    assert hb.ok()
    clock.advance(101)
    assert not hb.ok()
    hb.touch(50, "idle")
    assert hb.ok() and hb.status()["note"] == "idle"
    clock.advance(50)
    assert hb.ok()
    clock.advance(1)
    assert not hb.ok()


def test_health_endpoint_reports_status():
    clock = FakeClock()
    hb = Heartbeat(startup_grace=10, clock=clock)
    server = serve(hb, 0, host="127.0.0.1")
    try:
        url = f"http://127.0.0.1:{server.server_port}/healthz"
        with urllib.request.urlopen(url) as resp:
            assert resp.status == 200
            assert b"'healthy': True" in resp.read()
        clock.advance(11)
        try:
            urllib.request.urlopen(url)
            assert False, "expected 503"
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/other")
            assert False, "expected 404"
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.shutdown()
        server.server_close()
