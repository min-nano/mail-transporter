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


def test_sync_rejects_get(monkeypatch):
    monkeypatch.setattr(server, "get_forwarder", lambda: StubForwarder(SyncResult()))
    assert server.app.test_client().get("/sync").status_code == 405


def test_sync_requires_matching_invoker(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "get_forwarder", lambda: StubForwarder(SyncResult(forwarded=1)))
    monkeypatch.setattr(server, "ALLOWED_INVOKER_SA", "watcher@p.iam.gserviceaccount.com")
    monkeypatch.setattr(server, "EXPECTED_AUDIENCE", "https://svc.run.app")

    import google.oauth2.id_token

    def fake_verify(token, request, audience=None):
        assert audience == "https://svc.run.app"  # aud is enforced
        calls.append(token)
        if token == "good":
            return {"email": "watcher@p.iam.gserviceaccount.com", "email_verified": True}
        if token == "other":
            return {"email": "someone@p.iam.gserviceaccount.com", "email_verified": True}
        raise ValueError("bad token")

    monkeypatch.setattr(google.oauth2.id_token, "verify_oauth2_token", fake_verify)
    client = server.app.test_client()
    assert client.post("/sync").status_code == 403
    assert client.post("/sync", headers={"Authorization": "Bearer bad"}).status_code == 403
    assert client.post("/sync", headers={"Authorization": "Bearer other"}).status_code == 403
    assert client.post("/sync", headers={"Authorization": "Bearer good"}).status_code == 200
    assert calls == ["bad", "other", "good"]


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


def test_sync_refused_when_audience_is_missing(monkeypatch):
    """Invoker configured but no audience: fail closed, never fall back to an unchecked aud."""
    monkeypatch.setattr(server, "get_forwarder", lambda: StubForwarder(SyncResult(forwarded=1)))
    monkeypatch.setattr(server, "ALLOWED_INVOKER_SA", "watcher@p.iam.gserviceaccount.com")
    monkeypatch.setattr(server, "EXPECTED_AUDIENCE", "")

    import google.oauth2.id_token

    def fake_verify(token, request, audience=None):
        raise AssertionError("token must not be verified without an audience")

    monkeypatch.setattr(google.oauth2.id_token, "verify_oauth2_token", fake_verify)
    assert server.app.test_client().post("/sync", headers={"Authorization": "Bearer good"}).status_code == 403
