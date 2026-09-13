"""Gmail API wrapper: label management, duplicate lookup and raw insertion."""

from __future__ import annotations

import io
import json
import logging
import socket
import ssl

import google_auth_httplib2
import httplib2
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

log = logging.getLogger(__name__)

# Least privilege: insert messages and manage labels. Neither scope allows
# reading existing mail, so a leaked refresh token cannot exfiltrate the mailbox.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.insert",
    "https://www.googleapis.com/auth/gmail.labels",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"
RESUMABLE_THRESHOLD = 5 * 1024 * 1024  # bytes
# Socket timeout for every Gmail API round trip. Without it httplib2 blocks
# forever on a stalled connection, and with a single gunicorn worker and
# max-instances=1 that one hang would wedge the forwarder until a redeploy.
# It bounds each socket operation, not the whole request, so large uploads
# still complete as long as bytes keep flowing.
HTTP_TIMEOUT_SECONDS = 60.0


class GmailError(Exception):
    """Base class for Gmail failures."""


class GmailRetryableError(GmailError):
    """Transient failure (5xx, 429, quota, network). Leave the mail in INBOX and retry later."""


class GmailPermanentError(GmailError):
    """Gmail rejected this specific message. Retrying will not help."""


class GmailAuthError(GmailError):
    """Credentials are invalid or revoked. The whole run should stop."""


def build_credentials(client_id: str, client_secret: str, refresh_token: str) -> Credentials:
    return Credentials(
        None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )


def classify_http_error(exc: HttpError) -> type[GmailError]:
    status = int(getattr(exc.resp, "status", 0) or 0)
    if status == 401:
        return GmailAuthError
    if status == 429 or status >= 500:
        return GmailRetryableError
    if status == 403:
        # 403s describe the account or project (quota, missing scope, API
        # disabled, org policy), not this particular message. Never treat them
        # as a permanent rejection of the mail: leave it in INBOX and retry.
        return GmailRetryableError
    if status in (400, 404, 409, 410, 412, 413, 422):
        return GmailPermanentError
    return GmailRetryableError




class GmailClient:
    def __init__(
        self,
        credentials: Credentials,
        *,
        num_retries: int = 3,
        http_timeout: float = HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._credentials = credentials
        self._num_retries = num_retries
        http = google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http(timeout=http_timeout))
        self._service = build("gmail", "v1", http=http, cache_discovery=False)
        self._label_cache: dict[str, str] = {}

    # -- helpers -------------------------------------------------------
    def _execute(self, request):
        try:
            return request.execute(num_retries=self._num_retries)
        except HttpError as exc:
            error_cls = classify_http_error(exc)
            raise error_cls(f"Gmail API error {exc.resp.status}: {_message(exc)}") from exc
        except RefreshError as exc:
            raise GmailAuthError(f"Gmail OAuth refresh failed: {exc}") from exc
        except (TransportError, socket.error, ssl.SSLError, TimeoutError, ConnectionError) as exc:
            raise GmailRetryableError(f"Gmail transport error: {exc}") from exc

    # -- labels --------------------------------------------------------
    def ensure_label(self, name: str) -> str:
        """Return the id of label ``name``, creating it when necessary."""
        if name in self._label_cache:
            return self._label_cache[name]
        listing = self._execute(self._service.users().labels().list(userId="me"))
        for label in listing.get("labels", []):
            if label.get("name", "").lower() == name.lower():
                self._label_cache[name] = label["id"]
                return label["id"]
        created = self._execute(
            self._service.users().labels().create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
        )
        log.info("Created Gmail label %r (%s)", name, created["id"])
        self._label_cache[name] = created["id"]
        return created["id"]

    # -- messages ------------------------------------------------------
    def insert_raw(self, raw: bytes, label_ids: list[str]) -> str:
        """Insert an RFC 822 message as-is (no SMTP, no spam filtering) and return its id."""
        media = MediaIoBaseUpload(
            io.BytesIO(raw),
            mimetype="message/rfc822",
            resumable=len(raw) > RESUMABLE_THRESHOLD,
        )
        response = self._execute(
            self._service.users().messages().insert(
                userId="me",
                internalDateSource="dateHeader",
                body={"labelIds": label_ids},
                media_body=media,
            )
        )
        return response["id"]


def _message(exc: HttpError) -> str:
    try:
        payload = json.loads(exc.content.decode("utf-8"))
        return str(payload.get("error", {}).get("message") or exc)
    except Exception:  # noqa: BLE001
        return str(exc)
