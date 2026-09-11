"""Core sync loop: INBOX(iCloud) -> Gmail insert -> iCloud Trash, idempotently."""

from __future__ import annotations

import email.parser
import email.policy
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable

from . import store as st
from .gmail_client import (
    GmailAuthError,
    GmailClient,
    GmailError,
    GmailPermanentError,
    GmailRetryableError,
)
from .imap_client import ICloudMailbox, MailboxError, MessageGone

log = logging.getLogger(__name__)


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
    max_attempts: int = 50
    lease_seconds: int = 600
    time_budget_seconds: float = 480.0


def message_key(uidvalidity: int, uid: int) -> str:
    return f"{uidvalidity}-{uid}"


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


def gmail_labels_for(message, extra_label_ids: list[str]) -> list[str]:
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
        store: st.MessageStore,
        options: ForwarderOptions,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = st.utcnow,
    ) -> None:
        self._mailbox_factory = mailbox_factory
        self._gmail_factory = gmail_factory
        self._store = store
        self._options = options
        self._clock = clock
        self._now = now

    # ------------------------------------------------------------------
    def run(self) -> SyncResult:
        started = self._clock()
        result = SyncResult()
        try:
            with self._mailbox_factory() as mailbox:
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
    def _process(self, uid: int, mailbox: ICloudMailbox, gmail: GmailClient, extra_labels: list[str], result: SyncResult) -> None:
        key = message_key(mailbox.uidvalidity, uid)
        previous = self._store.claim(
            key,
            uid=uid,
            uidvalidity=mailbox.uidvalidity,
            now=self._now(),
            lease_seconds=self._options.lease_seconds,
        )
        if previous is None:
            log.info("uid=%s is being processed elsewhere; skipping", uid)
            result.skipped += 1
            return

        if previous.status in (st.REJECTED, st.QUARANTINED):
            self._quarantine(key, uid, mailbox, result)
            return

        gmail_id = previous.gmail_id
        if previous.status == st.DONE:
            log.warning("uid=%s already marked done but still in INBOX; trashing again", uid)
        elif gmail_id:
            log.info("uid=%s already inserted as gmail_id=%s; skipping insert", uid, gmail_id)
        else:
            try:
                gmail_id = self._insert(key, uid, mailbox, gmail, extra_labels)
            except MessageGone:
                log.info("uid=%s vanished from INBOX before fetch; skipping", uid)
                self._store.mark_done(key)
                result.skipped += 1
                return
            except MailboxError as exc:
                self._store.mark_pending(key, error=str(exc))
                result.retry_later += 1
                raise AbortRun(f"IMAP failure while fetching uid={uid}: {exc}") from exc
            except GmailAuthError as exc:
                self._store.mark_pending(key, error=str(exc))
                result.retry_later += 1
                raise AbortRun(str(exc)) from exc
            except GmailRetryableError as exc:
                attempts = self._store.mark_pending(key, error=str(exc))
                if self._options.max_attempts and attempts >= self._options.max_attempts:
                    log.error("uid=%s exhausted %d attempts: %s", uid, attempts, exc)
                    self._store.mark_rejected(key, error=f"attempts exhausted: {exc}")
                    self._quarantine(key, uid, mailbox, result)
                    return
                log.warning("uid=%s transient Gmail failure (attempt %d): %s", uid, attempts, exc)
                result.retry_later += 1
                return
            except GmailPermanentError as exc:
                log.error("uid=%s permanently rejected by Gmail: %s", uid, exc)
                self._store.mark_rejected(key, error=str(exc))
                self._quarantine(key, uid, mailbox, result)
                return
            if previous.status != st.DONE:
                result.forwarded += 1

        try:
            mailbox.move_to_trash(uid)
        except MailboxError as exc:
            self._store.mark_pending(key, error=str(exc))
            result.retry_later += 1
            raise AbortRun(f"IMAP failure while trashing uid={uid}: {exc}") from exc
        self._store.mark_done(key)
        result.trashed += 1
        log.info("uid=%s forwarded as gmail_id=%s and moved to Trash", uid, gmail_id)

    def _insert(self, key: str, uid: int, mailbox: ICloudMailbox, gmail: GmailClient, extra_labels: list[str]) -> str:
        message = mailbox.fetch_message(uid)
        message_id = extract_message_id(message.raw)
        gmail_id = None
        if message_id:
            gmail_id = gmail.find_by_message_id(message_id)
            if gmail_id:
                log.info("uid=%s already exists in Gmail (%s) by Message-ID; not inserting", uid, gmail_id)
        if not gmail_id:
            gmail_id = gmail.insert_raw(message.raw, gmail_labels_for(message, extra_labels))
        self._store.mark_inserted(key, gmail_id=gmail_id, message_id=message_id)
        return gmail_id

    def _quarantine(self, key: str, uid: int, mailbox: ICloudMailbox, result: SyncResult) -> None:
        try:
            mailbox.move_to_folder(uid, self._options.failed_folder)
        except MailboxError as exc:
            self._store.mark_pending(key, error=str(exc))
            result.retry_later += 1
            raise AbortRun(f"IMAP failure while quarantining uid={uid}: {exc}") from exc
        self._store.mark_quarantined(key)
        result.quarantined += 1
        log.warning("uid=%s moved to %r", uid, self._options.failed_folder)

