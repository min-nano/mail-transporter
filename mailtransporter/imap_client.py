"""Thin wrapper around imapclient for the iCloud INBOX."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.parser import BytesHeaderParser

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

    def has_keyword(self, keyword: str) -> bool:
        """IMAP flags are case-insensitive atoms (RFC 3501)."""
        return keyword.lower() in {f.lower() for f in self.flags}


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
        skip_keywords: Sequence[str] = (),
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self._password = password
        self.timeout = timeout
        self.readonly = readonly
        # Quarantined mail keeps sitting in the INBOX with one of these
        # keywords; it must never be listed as work again.
        self.skip_keywords = tuple(skip_keywords)
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
    def list_inbox_uids(self, *, skip_keywords: Sequence[str] | None = None) -> list[int]:
        """Return UIDs of INBOX messages that are neither \\Deleted nor quarantined.

        Quarantined mail stays in the INBOX carrying a keyword instead of being
        moved out of the way, so it has to be filtered out here — for the
        forwarder (which would re-send it to Gmail on every run) and for the
        watcher (which would never see an empty INBOX again) alike.
        """
        keywords = list(self.skip_keywords if skip_keywords is None else skip_keywords)
        criteria: list[str] = ["NOT", "DELETED"]
        for keyword in keywords:
            criteria += ["NOT", "KEYWORD", keyword]
        try:
            return sorted(int(u) for u in self.client.search(criteria))
        except Exception as exc:  # noqa: BLE001
            if not keywords:
                raise MailboxError(f"IMAP search failed: {exc}") from exc
            # A server may store keywords happily and still refuse to search on
            # them. Falling back to a plain listing plus a client-side filter
            # keeps quarantined mail out of the queue either way.
            log.warning("IMAP SEARCH with keyword exclusion failed (%s); filtering client-side", exc)
            return [uid for uid, flags in self.inbox_flags().items() if not _matches(flags, keywords)]

    def inbox_flags(self) -> dict[int, frozenset[str]]:
        """FLAGS of every non-\\Deleted INBOX message, keyed by UID (sorted)."""
        try:
            uids = sorted(int(u) for u in self.client.search(["NOT", "DELETED"]))
            fetched = self.client.fetch(uids, [b"FLAGS"]) if uids else {}
        except Exception as exc:  # noqa: BLE001
            raise MailboxError(f"IMAP search failed: {exc}") from exc
        return {
            uid: frozenset(_decode(f) for f in (fetched.get(uid) or {}).get(b"FLAGS", ()))
            for uid in uids
        }

    def search_keyword(self, keyword: str) -> list[int]:
        """UIDs of INBOX messages carrying ``keyword``.

        The SEARCH result is re-checked against the fetched FLAGS: a server
        that ignores an unknown KEYWORD criterion would otherwise report the
        whole INBOX as quarantined.
        """
        return [uid for uid, flags in self.inbox_flags().items() if _matches(flags, [keyword])]

    def fetch_headers(self, uids: Sequence[int]) -> dict[int, dict[str, str]]:
        """Return the Date / From / Subject of each UID, MIME-decoded."""
        if not uids:
            return {}
        try:
            fetched = self.client.fetch(list(uids), [b"BODY.PEEK[HEADER.FIELDS (DATE FROM SUBJECT)]"])
        except Exception as exc:  # noqa: BLE001
            raise MailboxError(f"IMAP header fetch failed: {exc}") from exc
        headers: dict[int, dict[str, str]] = {}
        for uid in uids:
            item = fetched.get(uid) or {}
            # Servers echo the section back with their own spelling, so match on
            # the prefix rather than on an exact key.
            raw = next(
                (v for k, v in item.items() if isinstance(k, bytes) and k.startswith(b"BODY[HEADER")),
                None,
            )
            parsed = BytesHeaderParser().parsebytes(bytes(raw)) if raw else {}
            headers[uid] = {
                name.lower(): _decode_header(parsed.get(name)) for name in ("Date", "From", "Subject")
            }
        return headers

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

        Fails closed: the keyword must be confirmed by the server, either in
        the STORE reply or by re-fetching the flags, so a silently ignored
        keyword can never be mistaken for success. Comparison is
        case-insensitive because IMAP flags are atoms.
        """
        try:
            response = self.client.add_flags([uid], [keyword.encode("ascii")])
            flags = {_decode(f).lower() for f in (response.get(uid) or ())}
            if keyword.lower() not in flags:
                fetched = self.client.fetch([uid], [b"FLAGS"])
                flags = {_decode(f).lower() for f in (fetched.get(uid) or {}).get(b"FLAGS", ())}
        except Exception as exc:  # noqa: BLE001
            raise MailboxError(f"IMAP STORE +FLAGS {keyword} failed for uid={uid}: {exc}") from exc
        if keyword.lower() not in flags:
            raise MailboxError(f"IMAP server did not confirm keyword {keyword} on uid={uid}")

    def remove_keyword(self, uid: int, keyword: str) -> None:
        """Clear ``keyword`` from ``uid``, confirming the server applied it.

        Used to put quarantined mail back into the queue; fails closed so a
        message is never reported as re-queued while the keyword still hides
        it from :meth:`list_inbox_uids`.
        """
        try:
            response = self.client.remove_flags([uid], [keyword.encode("ascii")])
            flags = {_decode(f).lower() for f in (response.get(uid) or ())} if uid in response else None
            if flags is None or keyword.lower() in flags:
                fetched = self.client.fetch([uid], [b"FLAGS"])
                flags = {_decode(f).lower() for f in (fetched.get(uid) or {}).get(b"FLAGS", ())}
        except Exception as exc:  # noqa: BLE001
            raise MailboxError(f"IMAP STORE -FLAGS {keyword} failed for uid={uid}: {exc}") from exc
        if keyword.lower() in flags:
            raise MailboxError(f"IMAP server did not clear keyword {keyword} on uid={uid}")

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
            elif client.has_capability("UIDPLUS"):
                # RFC 4315 UID EXPUNGE removes only this UID. A plain EXPUNGE
                # would also purge every other \Deleted message in INBOX.
                client.copy([uid], folder)
                client.add_flags([uid], [imapclient.DELETED])
                client.uid_expunge([uid])
            else:
                raise MailboxError(
                    "IMAP server supports neither MOVE nor UIDPLUS; refusing to move "
                    f"uid={uid} because the fallback could expunge unrelated mail"
                )
        except MailboxError:
            raise
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


def _matches(flags: frozenset[str], keywords: Sequence[str]) -> bool:
    """True when ``flags`` carries any of ``keywords`` (IMAP atoms, case-insensitive)."""
    lowered = {f.lower() for f in flags}
    return any(keyword.lower() in lowered for keyword in keywords)


def _decode_header(value: str | None) -> str:
    """Render a possibly MIME-encoded header as plain text (never raises)."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 - a malformed header must not break a listing
        return value
