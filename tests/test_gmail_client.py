from __future__ import annotations

import json

from googleapiclient.errors import HttpError

from mailtransporter.gmail_client import (
    SCOPES,
    GmailAuthError,
    GmailPermanentError,
    GmailRetryableError,
    classify_http_error,
)


def test_scopes_cannot_read_mail():
    assert all(s.endswith(("gmail.insert", "gmail.labels")) for s in SCOPES)


class Resp(dict):
    def __init__(self, status):
        super().__init__(status=status)
        self.status = status
        self.reason = "x"


def http_error(status, reason=None):
    body = {"error": {"message": "m", "errors": [{"reason": reason}] if reason else []}}
    return HttpError(Resp(status), json.dumps(body).encode())


def test_classification():
    assert classify_http_error(http_error(401)) is GmailAuthError
    assert classify_http_error(http_error(429)) is GmailRetryableError
    assert classify_http_error(http_error(500)) is GmailRetryableError
    assert classify_http_error(http_error(503, "backendError")) is GmailRetryableError
    # 403 describes the account/project, never a single message: always retry.
    assert classify_http_error(http_error(403, "rateLimitExceeded")) is GmailRetryableError
    assert classify_http_error(http_error(403, "insufficientPermissions")) is GmailRetryableError
    assert classify_http_error(http_error(403, "accessNotConfigured")) is GmailRetryableError
    assert classify_http_error(http_error(400, "invalidArgument")) is GmailPermanentError
    assert classify_http_error(http_error(413)) is GmailPermanentError


def test_client_http_has_socket_timeout():
    from mailtransporter.gmail_client import HTTP_TIMEOUT_SECONDS, GmailClient, build_credentials

    creds = build_credentials("id", "secret", "refresh")
    assert GmailClient(creds)._service._http.http.timeout == HTTP_TIMEOUT_SECONDS
    assert GmailClient(creds, http_timeout=5)._service._http.http.timeout == 5
