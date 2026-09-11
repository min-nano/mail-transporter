"""Thin wrapper around imapclient for the iCloud INBOX."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import imapclient
from imapclient import IMAPClient

log = logging.getLogger(__name__)

TRASH_FOLDER_CANDIDATES = ("Deleted Messages", "Trash")


class MailboxError(Exception):
    """Any IMAP-level failure. The caller should assume the connection is unusable."""


class MessageGone(MailboxError):
    """The requested UID no longer exists in the INBOX."""


@dataclass(frozen=True)
class FetchedMessage:
    uid: int
    raw: bytes
    flags: frozenset[str]

    @property
    def seen(self) -> bool:
        return "\\Seen" in self.flags

    @property
    def flagged(self) -> bool:
        return "\\Flagged" in self.flags


class ICloudMailbox:
    """A single IMAP session with INBOX selected.

    Use as a context manager: the connection is opened on enter and logged
    out on exit.  All methods raise :class:`MailboxError` on failure.
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        *,
        timeout: int = 60,
        readonly: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self._password = password
        self.timeout = timeout
        self.readonly = readonly
        self._client: IMAPClient | None = None
        self._trash_folder: str | None = None
        self.uidvalidity: int = 0
        self.supports_idle = False
        self.supports_keywords = False

    # -- lifecycle -----------------------------------------------------
    def __enter__(self) -> "ICloudMailbox":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        try:
            client = IMAPClient(self.host, port=self.port, ssl=True, timeout=self.timeout)
            client.login(self.user, self._password)
            info = client.select_folder("INBOX", readonly=self.readonly)
        except Exception as exc:  # noqa: BLE001 - normalise every failure
            raise MailboxError(f"IMAP connect failed: {exc}") from exc
        self._client = client
        self.uidvalidity = int(info.get(b"UIDVALIDITY", 0))
        self.supports_idle = client.has_capability("IDLE")
        # RFC 3501: "\*" in PERMANENTFLAGS means the server accepts new keywords.
        permanent = info.get(b"PERMANENTFLAGS", ())
        self.supports_keywords = any(_decode(f) == "\\*" for f in permanent)
        log.info(
            "IMAP connected host=%s user=%s uidvalidity=%s idle=%s keywords=%s",
            self.host, self.user, self.uidvalidity, self.supports_idle, self.supports_keywords,
        )

    def require_keywords(self) -> None:
        """Fail closed when the server cannot persist custom keywords.

        The forwarder records "already inserted into Gmail" as an IMAP keyword;
        without it a crash between insert and trash could duplicate mail.
        """
        if not self.supports_keywords:
            raise MailboxError(
                "IMAP server does not advertise support for custom keywords "
                "(PERMANENTFLAGS lacks \\*); refusing to forward"
            )

    def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.logout()
        except Exception:  # noqa: BLE001 - best effort
            try:
                client.shutdown()
            except Exception:  # noqa: BLE001
                pass

    @property
    def client(self) -> IMAPClient:
        if self._client is None:
            raise MailboxError("IMAP session is not connected")
        return self._client

    # -- queries -------------------------------------------------------
    def list_inbox_uids(self) -> list[int]:
        """Return UIDs of every message in INBOX that is not flagged \\Deleted."""
        try:
            return sorted(int(u) for u in self.client.search(["NOT", "DELETED"]))
        except Exception as exc:  # noqa: BLE001
            raise MailboxError(f"IMAP search failed: {exc}") from exc

    def fetch_message(self, uid: int) -> FetchedMessage:
        """Fetch the raw RFC 822 bytes and flags for ``uid`` without marking it read."""
        try:
            data = self.client.fetch([uid], [b"BODY.PEEK[]", b"FLAGS"])
        except Exception as exc:  # noqa: BLE001
            raise MailboxError(f"IMAP fetch failed for uid={uid}: {exc}") from exc
        item = data.get(uid)
        if not item or b"BODY[]" not in item:
            raise MessageGone(f"uid={uid} is no longer in INBOX")
        flags = frozenset(_decode(f) for f in item.get(b"FLAGS", ()))
        return FetchedMessage(uid=uid, raw=bytes(item[b"BODY[]"]), flags=flags)

    # -- mutations -----------------------------------------------------
    def add_keyword(self, uid: int, keyword: str) -> None:
        """Set a custom keyword (e.g. ``$GmailInserted``) on ``uid``.

        The server's reply is checked so that a silently ignored keyword is
        treated as a failure rather than a success.
        """
        try:
            response = self.client.add_flags([uid], [keyword.encode("ascii")])
        except Exception as exc:  # noqa: BLE001
            raise MailboxError(f"IMAP STORE +FLAGS {keyword} failed for uid={uid}: {exc}") from exc
        flags = {_decode(f) for f in (response.get(uid) or ())}
        if uid in response and keyword not in flags:
            raise MailboxError(f"IMAP server did not persist keyword {keyword} on uid={uid}")

    def trash_folder(self) -> str:
        if self._trash_folder:
            return self._trash_folder
        try:
            name = self.client.find_special_folder(imapclient.TRASH)
            if not name:
                for candidate in TRASH_FOLDER_CANDIDATES:
                    if self.client.folder_exists(candidate):
                        name = candidate
                        break
        except Exception as exc:  # noqa: BLE001
            raise MailboxError(f"IMAP folder lookup failed: {exc}") from exc
        if not name:
            raise MailboxError("Could not locate the Trash folder on the IMAP server")
        self._trash_folder = _decode(name)
        return self._trash_folder

    def move_to_trash(self, uid: int) -> None:
        self.move_to_folder(uid, self.trash_folder())

    def move_to_folder(self, uid: int, folder: str) -> None:
        """Move ``uid`` from INBOX into ``folder`` (created on demand)."""
        client = self.client
        try:
            if not client.folder_exists(folder):
                log.info("Creating IMAP folder %r", folder)
                client.create_folder(folder)
            if client.has_capability("MOVE"):
                client.move([uid], folder)
            else:
                client.copy([uid], folder)
                client.add_flags([uid], [imapclient.DELETED])
                client.uid_expunge([uid])
        except Exception as exc:  # noqa: BLE001
            raise MailboxError(f"IMAP move uid={uid} -> {folder!r} failed: {exc}") from exc

    # -- waiting -------------------------------------------------------
    def idle_wait(self, timeout: float) -> bool:
        """Block in IMAP IDLE for up to ``timeout`` seconds.

        Returns True when the server pushed any untagged response (typically
        EXISTS for new mail), False on timeout.
        """
        client = self.client
        try:
            client.idle()
            try:
                responses = client.idle_check(timeout=timeout)
            finally:
                client.idle_done()
        except Exception as exc:  # noqa: BLE001
            raise MailboxError(f"IMAP IDLE failed: {exc}") from exc
        return bool(responses)

    def noop(self) -> None:
        try:
            self.client.noop()
        except Exception as exc:  # noqa: BLE001
            raise MailboxError(f"IMAP NOOP failed: {exc}") from exc


def _decode(value: bytes | str) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
