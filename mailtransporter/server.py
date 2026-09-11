"""Cloud Run entrypoint: an HTTP endpoint that performs one sync pass."""

from __future__ import annotations

import logging
import os
import threading

from flask import Flask, jsonify, request

from .runtime import build_forwarder, configure_logging

configure_logging()
log = logging.getLogger(__name__)

app = Flask(__name__)
_run_lock = threading.Lock()
_forwarder = None
_forwarder_lock = threading.Lock()

# Defence in depth: Cloud Run's IAM already restricts invokers, but if that
# binding is ever loosened, the app itself still only accepts identity
# tokens issued to the configured service account.
ALLOWED_INVOKER_SA = os.environ.get("ALLOWED_INVOKER_SA", "").strip()


def verify_invoker(authorization_header: str | None, allowed_sa: str) -> bool:
    """Return True when the bearer token is a Google-signed ID token for ``allowed_sa``."""
    if not allowed_sa:
        return True
    if not authorization_header or not authorization_header.startswith("Bearer "):
        return False
    import google.auth.transport.requests
    import google.oauth2.id_token

    try:
        claims = google.oauth2.id_token.verify_oauth2_token(
            authorization_header[len("Bearer "):], google.auth.transport.requests.Request()
        )
    except Exception as exc:  # noqa: BLE001 - any verification failure is a denial
        log.warning("Rejected /sync call: %s", exc)
        return False
    return bool(claims.get("email_verified")) and claims.get("email") == allowed_sa


def get_forwarder():
    global _forwarder
    with _forwarder_lock:
        if _forwarder is None:
            _forwarder = build_forwarder()
        return _forwarder


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.post("/sync")
def sync():
    if not verify_invoker(request.headers.get("Authorization"), ALLOWED_INVOKER_SA):
        return jsonify({"status": "forbidden"}), 403
    # Cloud Run is deployed with concurrency=1 / max-instances=1, but guard
    # anyway: two overlapping passes would fight over the same UIDs.
    if not _run_lock.acquire(blocking=False):
        return jsonify({"status": "busy"}), 409
    try:
        result = get_forwarder().run()
        return jsonify(result.to_dict()), (200 if result.ok else 500)
    except Exception as exc:  # noqa: BLE001 - always answer with JSON
        log.exception("sync failed")
        return jsonify({"status": "error", "error": str(exc)}), 500
    finally:
        _run_lock.release()
