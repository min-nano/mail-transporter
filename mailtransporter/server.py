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
# The service's own URL: ID tokens minted for any other audience are refused.
EXPECTED_AUDIENCE = os.environ.get("EXPECTED_AUDIENCE", "").strip()
_transport = None
_transport_lock = threading.Lock()
if ALLOWED_INVOKER_SA and not EXPECTED_AUDIENCE:
    log.warning(
        "ALLOWED_INVOKER_SA is set but EXPECTED_AUDIENCE is empty: every /sync call "
        "is refused until the audience is configured"
    )


def _google_transport():
    """One shared transport so Google's signing certificates are cached across calls."""
    global _transport
    with _transport_lock:
        if _transport is None:
            import google.auth.transport.requests

            _transport = google.auth.transport.requests.Request()
        return _transport


def verify_invoker(authorization_header: str | None, allowed_sa: str, audience: str | None = None) -> bool:
    """Return True when the bearer token is a Google-signed ID token for ``allowed_sa``.

    ``audience`` (the Cloud Run service URL) must be given as well: a token
    issued for another service could otherwise be replayed here. With an
    invoker configured but no audience, every call is refused (fail closed),
    which is the state of the very first Cloud Run revision until the deploy
    script fills the URL in.
    """
    if not allowed_sa:
        return True
    if not audience:
        log.warning("Rejected /sync call: EXPECTED_AUDIENCE is not configured")
        return False
    if not authorization_header or not authorization_header.startswith("Bearer "):
        return False
    import google.oauth2.id_token

    try:
        claims = google.oauth2.id_token.verify_oauth2_token(
            authorization_header[len("Bearer "):], _google_transport(), audience=audience
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
    if not verify_invoker(request.headers.get("Authorization"), ALLOWED_INVOKER_SA, EXPECTED_AUDIENCE):
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
