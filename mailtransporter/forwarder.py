"""Core sync loop: INBOX(iCloud) -> Gmail insert -> iCloud Trash, idempotently.

No external state store is involved.  Progress is recorded on the message
itself with an IMAP keyword:

    INBOX message ──insert into Gmail──► +$GmailInserted ──MOVE──► Trash

* A message carrying the keyword has already been inserted, so a crash
  between insert and move can only ever cause the move to be repeated.
* A crash between insert and setting the keyword (a window of one IMAP
  round trip) is resolved towards a duplicate in Gmail, never towards loss.
  Deduplicating via Gmail search would require a read scope on the mailbox
  and would let a forged Message-ID suppress delivery, so it is not done.
  If the keyword is refused outright, the message is parked in the failed
  folder so the duplicate count stays at one.
* Messages Gmail rejects permanently are moved to a separate IMAP folder so
  they are never lost and never block the queue. Rejections are only acted
  on at the end of a run: if every message was rejected and nothing got
  through, the cause is almost certainly the account or the request shape,
  not the mail, and nothing is moved.
"""

from __future__ import annotations

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
    # Circuit breaker: this many permanent rejections in one run with no
    # successful insert at all is treated as a systemic failure.
    rejection_threshold: int = 3


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
        rejected: list[tuple[int, str]] = []
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
                        self._process(uid, mailbox, gmail, extra_labels, result, rejected)
                    except AbortRun as exc:
                        log.error("Aborting run: %s", exc)
                        result.error = str(exc)
                        result.remaining = len(uids) - index
                        break
                if rejected and result.error is None:
                    self._settle_rejections(rejected, mailbox, result)
        except (MailboxError, GmailError) as exc:
            log.error("Run failed before processing could start: %s", exc)
            result.error = str(exc)
        result.duration_seconds = round(self._clock() - started, 3)
        log.info("Sync finished: %s", result.to_dict())
        return result

    def _settle_rejections(self, rejected: list[tuple[int, str]], mailbox: ICloudMailbox, result: SyncResult) -> None:
        threshold = self._options.rejection_threshold
        if threshold and len(rejected) >= threshold and result.forwarded == 0:
            result.error = (
                f"{len(rejected)} message(s) were rejected by Gmail and none were accepted; "
                f"suspecting an account or configuration problem, nothing was quarantined. "
                f"First error: {rejected[0][1]}"
            )
            log.error(result.error)
            return
        for uid, error in rejected:
            log.error("uid=%s permanently rejected by Gmail: %s", uid, error)
            try:
                self._quarantine(uid, mailbox, result)
            except AbortRun as exc:
                log.error("Aborting run: %s", exc)
                result.error = str(exc)
                return

    # ------------------------------------------------------------------
    def _process(
        self,
        uid: int,
        mailbox: ICloudMailbox,
        gmail: GmailClient,
        extra_labels: list[str],
        result: SyncResult,
        rejected: list[tuple[int, str]],
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

        if message.has_keyword(keyword):
            log.info("uid=%s already carries %s; skipping insert", uid, keyword)
        else:
            try:
                gmail_id = gmail.insert_raw(message.raw, gmail_labels_for(message, extra_labels))
            except GmailAuthError as exc:
                raise AbortRun(str(exc)) from exc
            except GmailRetryableError as exc:
                log.warning("uid=%s transient Gmail failure; will retry next run: %s", uid, exc)
                result.retry_later += 1
                return
            except GmailPermanentError as exc:
                rejected.append((uid, str(exc)))
                return
            result.forwarded += 1
            log.info("uid=%s inserted into Gmail as %s", uid, gmail_id)
            try:
                mailbox.add_keyword(uid, keyword)
            except MailboxError as exc:
                # Gmail already has the message but we cannot record that on
                # the iCloud copy. Leaving it in INBOX would insert a fresh
                # duplicate on every run, so park it in the failed folder
                # instead (never the Trash: the keyword is the only proof).
                log.error("uid=%s inserted as %s but the keyword could not be set: %s", uid, gmail_id, exc)
                self._quarantine(uid, mailbox, result)
                return

        try:
            mailbox.move_to_trash(uid)
        except MailboxError as exc:
            raise AbortRun(f"IMAP failure while trashing uid={uid}: {exc}") from exc
        result.trashed += 1
        log.info("uid=%s moved to Trash", uid)

    def _quarantine(self, uid: int, mailbox: ICloudMailbox, result: SyncResult) -> None:
        try:
            mailbox.move_to_folder(uid, self._options.failed_folder)
        except MailboxError as exc:
            raise AbortRun(f"IMAP failure while quarantining uid={uid}: {exc}") from exc
        result.quarantined += 1
        log.warning("uid=%s moved to %r", uid, self._options.failed_folder)
