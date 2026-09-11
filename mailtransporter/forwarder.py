"""Core sync loop: INBOX(iCloud) -> Gmail insert -> iCloud Trash, idempotently.

No external state store is involved.  Progress is recorded on the message
itself with an IMAP keyword:

    INBOX message ──insert into Gmail──► +$GmailInserted ──MOVE──► Trash

* A message carrying the keyword has already been inserted, so a crash
  between insert and move can only ever cause the move to be repeated.
* A crash between insert and setting the keyword is covered by looking the
  message's Message-ID up in Gmail before inserting.
* Messages Gmail rejects permanently are moved to a separate IMAP folder so
  they are never lost and never block the queue.
"""

from __future__ import annotations

import email.parser
import email.policy
import logging
import time
from dataclasses import asdict, dataclass
from typing import Callable

from .gmail_client import (
    GmailAuthError,
    GmailClient,
    GmailError,
    GmailPermanentError,
    GmailRetryableError,
)
from .imap_client import FetchedMessage, ICloudMailbox, MailboxError, MessageGone

log = logging.getLogger(__name__)

DEFAULT_INSERTED_KEYWORD = "$GmailInserted"


class AbortRun(Exception):
    """The current run cannot continue (IMAP session broken, auth failure...)."""


@dataclass
class SyncResult:
    listed: int = 0
    forwarded: int = 0
    trashed: int = 0
    skipped: int = 0
    retry_later: int = 0
    quarantined: int = 0
    remaining: int = 0
    duration_seconds: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = "ok" if self.ok else "error"
        return data


@dataclass(frozen=True)
class ForwarderOptions:
    label: str | None = "iCloud"
    failed_folder: str = "Forward-Failed"
    inserted_keyword: str = DEFAULT_INSERTED_KEYWORD
    time_budget_seconds: float = 480.0


def extract_message_id(raw: bytes) -> str | None:
    try:
        headers = email.parser.BytesHeaderParser(policy=email.policy.compat32).parsebytes(raw)
    except Exception:  # noqa: BLE001 - malformed headers are not fatal
        return None
    value = headers.get("Message-ID")
    if not value:
        return None
    value = "".join(str(value).split())
    return value or None


def gmail_labels_for(message: FetchedMessage, extra_label_ids: list[str]) -> list[str]:
    labels = ["INBOX"]
    if not message.seen:
        labels.append("UNREAD")
    if message.flagged:
        labels.append("STARRED")
    labels.extend(extra_label_ids)
    return labels


class Forwarder:
    def __init__(
        self,
        mailbox_factory: Callable[[], ICloudMailbox],
        gmail_factory: Callable[[], GmailClient],
        options: ForwarderOptions,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mailbox_factory = mailbox_factory
        self._gmail_factory = gmail_factory
        self._options = options
        self._clock = clock

    # ------------------------------------------------------------------
    def run(self) -> SyncResult:
        started = self._clock()
        result = SyncResult()
        try:
            with self._mailbox_factory() as mailbox:
                mailbox.require_keywords()
                gmail = self._gmail_factory()
                extra_labels = [gmail.ensure_label(self._options.label)] if self._options.label else []
                uids = mailbox.list_inbox_uids()
                result.listed = len(uids)
                for index, uid in enumerate(uids):
                    if self._clock() - started > self._options.time_budget_seconds:
                        log.info("Time budget exhausted; %d message(s) left for the next run", len(uids) - index)
                        result.remaining = len(uids) - index
                        break
                    try:
                        self._process(uid, mailbox, gmail, extra_labels, result)
                    except AbortRun as exc:
                        log.error("Aborting run: %s", exc)
                        result.error = str(exc)
                        result.remaining = len(uids) - index
                        break
        except (MailboxError, GmailError) as exc:
            log.error("Run failed before processing could start: %s", exc)
            result.error = str(exc)
        result.duration_seconds = round(self._clock() - started, 3)
        log.info("Sync finished: %s", result.to_dict())
        return result

    # ------------------------------------------------------------------
    def _process(
        self,
        uid: int,
        mailbox: ICloudMailbox,
        gmail: GmailClient,
        extra_labels: list[str],
        result: SyncResult,
    ) -> None:
        keyword = self._options.inserted_keyword
        try:
            message = mailbox.fetch_message(uid)
        except MessageGone:
            log.info("uid=%s vanished from INBOX before fetch; skipping", uid)
            result.skipped += 1
            return
        except MailboxError as exc:
            raise AbortRun(f"IMAP failure while fetching uid={uid}: {exc}") from exc

        if keyword in message.flags:
            log.info("uid=%s already carries %s; skipping insert", uid, keyword)
        else:
            try:
                gmail_id = self._insert(message, gmail, extra_labels)
            except GmailAuthError as exc:
                raise AbortRun(str(exc)) from exc
            except GmailRetryableError as exc:
                log.warning("uid=%s transient Gmail failure; will retry next run: %s", uid, exc)
                result.retry_later += 1
                return
            except GmailPermanentError as exc:
                log.error("uid=%s permanently rejected by Gmail: %s", uid, exc)
                self._quarantine(uid, mailbox, result)
                return
            try:
                mailbox.add_keyword(uid, keyword)
            except MailboxError as exc:
                # Gmail has the message; the Message-ID lookup protects the next run.
                raise AbortRun(f"IMAP failure while flagging uid={uid} (gmail_id={gmail_id}): {exc}") from exc
            result.forwarded += 1
            log.info("uid=%s inserted into Gmail as %s", uid, gmail_id)

        try:
            mailbox.move_to_trash(uid)
        except MailboxError as exc:
            raise AbortRun(f"IMAP failure while trashing uid={uid}: {exc}") from exc
        result.trashed += 1
        log.info("uid=%s moved to Trash", uid)

    def _insert(self, message: FetchedMessage, gmail: GmailClient, extra_labels: list[str]) -> str:
        message_id = extract_message_id(message.raw)
        if message_id:
            existing = gmail.find_by_message_id(message_id)
            if existing:
                log.info("uid=%s already exists in Gmail (%s) by Message-ID; not inserting", message.uid, existing)
                return existing
        return gmail.insert_raw(message.raw, gmail_labels_for(message, extra_labels))

    def _quarantine(self, uid: int, mailbox: ICloudMailbox, result: SyncResult) -> None:
        try:
            mailbox.move_to_folder(uid, self._options.failed_folder)
        except MailboxError as exc:
            raise AbortRun(f"IMAP failure while quarantining uid={uid}: {exc}") from exc
        result.quarantined += 1
        log.warning("uid=%s moved to %r", uid, self._options.failed_folder)
