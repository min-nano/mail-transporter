from __future__ import annotations

import threading

from mailtransporter import server
from mailtransporter.forwarder import SyncResult


class StubForwarder:
    def __init__(self, result, gate=None):
        self.result = result
        self.gate = gate

    def run(self):
        if self.gate:
            self.gate.wait()
        return self.result


def test_sync_ok(monkeypatch):
    monkeypatch.setattr(server, "get_forwarder", lambda: StubForwarder(SyncResult(listed=1, forwarded=1, trashed=1)))
    client = server.app.test_client()
    resp = client.post("/sync")
    assert resp.status_code == 200
    assert resp.get_json()["forwarded"] == 1


def test_sync_error_status(monkeypatch):
    monkeypatch.setattr(server, "get_forwarder", lambda: StubForwarder(SyncResult(error="imap down")))
    resp = server.app.test_client().post("/sync")
    assert resp.status_code == 500
    assert resp.get_json()["status"] == "error"


def test_concurrent_sync_is_rejected(monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(server, "get_forwarder", lambda: StubForwarder(SyncResult(), gate))
    client = server.app.test_client()
    results = {}

    def first():
        results["first"] = client.post("/sync").status_code

    t = threading.Thread(target=first)
    t.start()
    while not server._run_lock.locked():
        pass
    assert client.post("/sync").status_code == 409
    gate.set()
    t.join()
    assert results["first"] == 200
