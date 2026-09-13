from __future__ import annotations

import pytest

from mailtransporter.imap_client import ICloudMailbox, MailboxError


class StubClient:
    def __init__(self, capabilities):
        self.capabilities = set(capabilities)
        self.calls: list[tuple] = []

    def has_capability(self, name):
        return name in self.capabilities

    def folder_exists(self, folder):
        return True

    def move(self, uids, folder):
        self.calls.append(("move", uids, folder))

    def copy(self, uids, folder):
        self.calls.append(("copy", uids, folder))

    def add_flags(self, uids, flags):
        self.calls.append(("add_flags", uids))

    def uid_expunge(self, uids):
        self.calls.append(("uid_expunge", uids))


def mailbox_with(capabilities):
    mailbox = ICloudMailbox("imap.example", 993, "user", "pw")
    mailbox._client = StubClient(capabilities)
    return mailbox


def test_move_uses_move_when_available():
    mailbox = mailbox_with({"MOVE", "UIDPLUS"})
    mailbox.move_to_folder(7, "Archive")
    assert mailbox._client.calls == [("move", [7], "Archive")]


def test_move_falls_back_to_copy_and_uid_expunge_with_uidplus():
    mailbox = mailbox_with({"UIDPLUS"})
    mailbox.move_to_folder(7, "Archive")
    assert [c[0] for c in mailbox._client.calls] == ["copy", "add_flags", "uid_expunge"]
    assert mailbox._client.calls[-1] == ("uid_expunge", [7])


def test_move_refuses_without_move_or_uidplus():
    """A plain EXPUNGE would purge every \\Deleted message in INBOX, so fail closed."""
    mailbox = mailbox_with(set())
    with pytest.raises(MailboxError, match="neither MOVE nor UIDPLUS"):
        mailbox.move_to_folder(7, "Archive")
    assert mailbox._client.calls == []
