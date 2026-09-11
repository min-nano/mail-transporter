"""Per-message state tracking used for idempotent forwarding.

The iCloud INBOX itself is the work queue (a message stays there until it has
been inserted into Gmail *and* moved to the Trash).  The store only has to
remember what has already happened to each message so that a crash or API
outage in the middle of processing never produces duplicates in Gmail.

State machine (``status`` field)::

    pending ──claim──> processing ──insert ok──> inserted ──trash ok──> done
                          │                          │
                          │ retryable error          │ trash error -> stays "inserted"
                          v                          │   (re-claimed; insert skipped)
                       pending (attempts+1)          │
                          │ permanent error / attempts exhausted
                          v
                       rejected ──move to failed folder──> quarantined
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

log = logging.getLogger(__name__)

PENDING = "pending"
PROCESSING = "processing"
INSERTED = "inserted"
DONE = "done"
REJECTED = "rejected"
QUARANTINED = "quarantined"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class MessageRecord:
    key: str
    status: str = PENDING
    attempts: int = 0
    gmail_id: str | None = None
    message_id: str | None = None
    last_error: str | None = None
    lease_until: datetime | None = None
    uid: int | None = None
    uidvalidity: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    expire_at: datetime | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "attempts": self.attempts,
            "gmail_id": self.gmail_id,
            "message_id": self.message_id,
            "last_error": self.last_error,
            "lease_until": self.lease_until,
            "uid": self.uid,
            "uidvalidity": self.uidvalidity,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expire_at": self.expire_at,
        }

    @classmethod
    def from_dict(cls, key: str, data: dict) -> "MessageRecord":
        known = {f for f in cls.__dataclass_fields__ if f not in ("key", "extra")}
        return cls(
            key=key,
            **{k: v for k, v in data.items() if k in known},
            extra={k: v for k, v in data.items() if k not in known},
        )


class MessageStore(Protocol):
    """What the forwarder needs from a state backend."""

    def claim(self, key: str, *, uid: int, uidvalidity: int, now: datetime, lease_seconds: int) -> MessageRecord | None:
        """Atomically take the processing lease for ``key``.

        Returns the record *as it was before the claim* (so the caller can see
        the previous status / gmail_id), or ``None`` when another worker holds
        a live lease.
        """

    def mark_inserted(self, key: str, *, gmail_id: str, message_id: str | None) -> None: ...

    def mark_done(self, key: str) -> None: ...

    def mark_pending(self, key: str, *, error: str) -> int:
        """Record a retryable failure. Returns the new attempt count."""

    def mark_rejected(self, key: str, *, error: str) -> None: ...

    def mark_quarantined(self, key: str) -> None: ...


# ----------------------------------------------------------------------
# In-memory backend (tests, local dry runs)
# ----------------------------------------------------------------------
class InMemoryMessageStore:
    def __init__(self, *, retention_days: int = 30) -> None:
        self.records: dict[str, MessageRecord] = {}
        self.retention_days = retention_days

    def claim(self, key, *, uid, uidvalidity, now, lease_seconds):
        current = self.records.get(key)
        previous = MessageRecord(**{**current.__dict__}) if current else None
        if current and current.status == PROCESSING and current.lease_until and current.lease_until > now:
            return None
        if current is None:
            current = MessageRecord(key=key, uid=uid, uidvalidity=uidvalidity, created_at=now)
            previous = MessageRecord(key=key, status=PENDING)
            self.records[key] = current
        current.status = PROCESSING
        current.lease_until = now + timedelta(seconds=lease_seconds)
        current.updated_at = now
        current.expire_at = now + timedelta(days=self.retention_days)
        return previous

    def _update(self, key: str, **changes) -> MessageRecord:
        record = self.records[key]
        for name, value in changes.items():
            setattr(record, name, value)
        record.updated_at = utcnow()
        return record

    def mark_inserted(self, key, *, gmail_id, message_id):
        self._update(key, status=INSERTED, gmail_id=gmail_id, message_id=message_id, lease_until=None)

    def mark_done(self, key):
        self._update(key, status=DONE, lease_until=None, last_error=None)

    def mark_pending(self, key, *, error):
        record = self.records[key]
        status = INSERTED if record.gmail_id else PENDING
        self._update(key, status=status, attempts=record.attempts + 1, last_error=error, lease_until=None)
        return record.attempts

    def mark_rejected(self, key, *, error):
        record = self.records[key]
        self._update(key, status=REJECTED, attempts=record.attempts + 1, last_error=error, lease_until=None)

    def mark_quarantined(self, key):
        self._update(key, status=QUARANTINED, lease_until=None)


# ----------------------------------------------------------------------
# Firestore backend (production)
# ----------------------------------------------------------------------
class FirestoreMessageStore:
    """State backend on Firestore (Native mode).

    Sized for the always-free quota: one message costs roughly three writes
    (claim, inserted, done) and one read.
    """

    def __init__(self, client, collection: str, *, retention_days: int = 30) -> None:
        from google.cloud import firestore  # noqa: F401 - ensures dependency is present

        self._client = client
        self._collection = client.collection(collection)
        self.retention_days = retention_days

    @classmethod
    def from_settings(cls, collection: str, retention_days: int) -> "FirestoreMessageStore":
        from google.cloud import firestore

        return cls(firestore.Client(), collection, retention_days=retention_days)

    def claim(self, key, *, uid, uidvalidity, now, lease_seconds):
        from google.cloud import firestore

        ref = self._collection.document(key)
        expire_at = now + timedelta(days=self.retention_days)

        @firestore.transactional
        def _claim(transaction):
            snapshot = ref.get(transaction=transaction)
            data = snapshot.to_dict() if snapshot.exists else None
            if data is None:
                previous = MessageRecord(key=key, status=PENDING)
                transaction.set(
                    ref,
                    {
                        "status": PROCESSING,
                        "attempts": 0,
                        "gmail_id": None,
                        "message_id": None,
                        "last_error": None,
                        "uid": uid,
                        "uidvalidity": uidvalidity,
                        "lease_until": now + timedelta(seconds=lease_seconds),
                        "created_at": now,
                        "updated_at": now,
                        "expire_at": expire_at,
                    },
                )
                return previous
            previous = MessageRecord.from_dict(key, data)
            lease_until = data.get("lease_until")
            if data.get("status") == PROCESSING and lease_until and lease_until > now:
                return None
            transaction.update(
                ref,
                {
                    "status": PROCESSING,
                    "lease_until": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                    "expire_at": expire_at,
                },
            )
            return previous

        return _claim(self._client.transaction())

    def _update(self, key: str, **changes) -> None:
        changes["updated_at"] = utcnow()
        self._collection.document(key).update(changes)

    def mark_inserted(self, key, *, gmail_id, message_id):
        self._update(key, status=INSERTED, gmail_id=gmail_id, message_id=message_id, lease_until=None)

    def mark_done(self, key):
        self._update(key, status=DONE, lease_until=None, last_error=None)

    def mark_pending(self, key, *, error):
        from google.cloud import firestore

        ref = self._collection.document(key)
        snapshot = ref.get()
        data = snapshot.to_dict() or {}
        status = INSERTED if data.get("gmail_id") else PENDING
        ref.update(
            {
                "status": status,
                "attempts": firestore.Increment(1),
                "last_error": error[:2000],
                "lease_until": None,
                "updated_at": utcnow(),
            }
        )
        return int(data.get("attempts", 0)) + 1

    def mark_rejected(self, key, *, error):
        from google.cloud import firestore

        self._collection.document(key).update(
            {
                "status": REJECTED,
                "attempts": firestore.Increment(1),
                "last_error": error[:2000],
                "lease_until": None,
                "updated_at": utcnow(),
            }
        )

    def mark_quarantined(self, key):
        self._update(key, status=QUARANTINED, lease_until=None)


def build_store(backend: str, *, collection: str, retention_days: int) -> MessageStore:
    if backend == "memory":
        log.warning("Using in-memory message store: duplicates are possible across restarts")
        return InMemoryMessageStore(retention_days=retention_days)
    if backend == "firestore":
        return FirestoreMessageStore.from_settings(collection, retention_days)
    raise ValueError(f"Unknown STORE_BACKEND: {backend!r}")
