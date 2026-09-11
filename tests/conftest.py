from __future__ import annotations

import pytest

from mailtransporter.forwarder import Forwarder, ForwarderOptions
from mailtransporter.gmail_client import GmailError
from mailtransporter.imap_client import FetchedMessage, MailboxError, MessageGone


class FakeMailbox:
    """In-memory stand-in for ICloudMailbox."""

    def __init__(self, messages: dict[int, bytes] | None = None, *, uidvalidity: int = 7, flags=None):
        self.inbox: dict[int, bytes] = dict(messages or {})
        self.flags: dict[int, frozenset[str]] = dict(flags or {})
        self.uidvalidity = uidvalidity
        self.folders: dict[str, list[int]] = {"Deleted Messages": []}
        self.supports_idle = True
        self.supports_keywords = True
        self.fail_fetch: set[int] = set()
        self.fail_move: set[int] = set()
        self.fail_keyword: set[int] = set()
        self.connected = False
        self.idle_events: list[bool] = []

    def require_keywords(self):
        if not self.supports_keywords:
            raise MailboxError("no keyword support")

    def add_keyword(self, uid, keyword):
        if uid in self.fail_keyword:
            raise MailboxError("boom keyword")
        if uid not in self.inbox:
            raise MailboxError(f"uid={uid} not in INBOX")
        self.flags[uid] = self.flags.get(uid, frozenset()) | {keyword}

    def __enter__(self):
        self.connected = True
        return self

    def __exit__(self, *exc):
        self.connected = False

    def list_inbox_uids(self):
        return sorted(self.inbox)

    def fetch_message(self, uid):
        if uid in self.fail_fetch:
            raise MailboxError("boom fetch")
        if uid not in self.inbox:
            raise MessageGone(f"uid={uid} gone")
        return FetchedMessage(uid=uid, raw=self.inbox[uid], flags=self.flags.get(uid, frozenset()))

    def move_to_trash(self, uid):
        self.move_to_folder(uid, "Deleted Messages")

    def move_to_folder(self, uid, folder):
        if uid in self.fail_move:
            raise MailboxError("boom move")
        if uid not in self.inbox:
            raise MailboxError(f"uid={uid} not in INBOX")
        self.folders.setdefault(folder, []).append(uid)
        del self.inbox[uid]

    def idle_wait(self, timeout):
        return self.idle_events.pop(0) if self.idle_events else False


class FakeGmail:
    def __init__(self):
        self.inserted: list[tuple[bytes, list[str]]] = []
        self.existing_message_ids: dict[str, str] = {}
        self.errors: list[GmailError] = []  # raised (in order) by insert_raw
        self.labels: dict[str, str] = {}

    def ensure_label(self, name):
        return self.labels.setdefault(name, f"Label_{len(self.labels) + 1}")

    def find_by_message_id(self, message_id):
        return self.existing_message_ids.get(message_id.strip("<>"))

    def insert_raw(self, raw, label_ids):
        if self.errors:
            raise self.errors.pop(0)
        self.inserted.append((raw, list(label_ids)))
        return f"gmail-{len(self.inserted)}"


def make_raw(message_id: str | None = "<abc@example.com>", subject: str = "hello") -> bytes:
    lines = [f"Subject: {subject}", "From: a@example.com", "To: b@example.com", "Date: Mon, 1 Jan 2024 00:00:00 +0000"]
    if message_id:
        lines.append(f"Message-ID: {message_id}")
    return ("\r\n".join(lines) + "\r\n\r\nbody\r\n").encode()


@pytest.fixture
def mailbox():
    return FakeMailbox()


@pytest.fixture
def gmail():
    return FakeGmail()


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock():
    return FakeClock()


def make_forwarder(mailbox, gmail, clock=None, **opts):
    options = ForwarderOptions(**{"label": "iCloud", "time_budget_seconds": 100, **opts})
    return Forwarder(lambda: mailbox, lambda: gmail, options, **({"clock": clock} if clock else {}))
