from __future__ import annotations

import json

from googleapiclient.errors import HttpError

from mailtransporter.gmail_client import (
    GmailAuthError,
    GmailPermanentError,
    GmailRetryableError,
    classify_http_error,
)


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
    assert classify_http_error(http_error(403, "rateLimitExceeded")) is GmailRetryableError
    assert classify_http_error(http_error(403, "insufficientPermissions")) is GmailPermanentError
    assert classify_http_error(http_error(400, "invalidArgument")) is GmailPermanentError
    assert classify_http_error(http_error(413)) is GmailPermanentError
