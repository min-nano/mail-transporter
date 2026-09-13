"""The maintenance commands are the only way to see and undo a keyword quarantine.

Apple Mail and icloud.com do not display custom IMAP keywords, so a bug here
would leave quarantined mail invisible *and* unrecoverable.
"""

from __future__ import annotations

import json

import pytest

from mailtransporter import cli
from mailtransporter.imap_client import MailboxError

FAILED = "$GmailFailed"
UNVERIFIED = "$GmailUnverified"


class FakeMailbox:
    def __init__(self, flags: dict[int, set[str]]):
        self.flags = flags
        self.headers = {
            uid: {"date": "Mon, 1 Jan 2024 00:00:00 +0000", "from": "a@example.com", "subject": f"mail {uid}"}
            for uid in flags
        }
        self.keyword_support = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def require_keywords(self):
        if not self.keyword_support:
            raise MailboxError("no keyword support")

    def search_keyword(self, keyword):
        return sorted(uid for uid, flags in self.flags.items() if keyword in flags)

    def fetch_headers(self, uids):
        return {uid: self.headers[uid] for uid in uids}

    def add_keyword(self, uid, keyword):
        self.flags.setdefault(uid, set()).add(keyword)

    def remove_keyword(self, uid, keyword):
        self.flags.get(uid, set()).discard(keyword)


@pytest.fixture
def mailbox(monkeypatch):
    box = FakeMailbox({1: {FAILED}, 2: set(), 3: {UNVERIFIED}})
    monkeypatch.setattr(cli, "_mailbox", lambda *, readonly: (box, FAILED, UNVERIFIED))
    monkeypatch.setattr(cli, "configure_logging", lambda: None)
    return box


def test_list_reports_both_kinds_with_their_gmail_state(mailbox, capsys):
    assert cli.main(["list-quarantined"]) == 0
    out = capsys.readouterr().out
    assert f"uid=1 {FAILED} [not in Gmail]" in out
    assert f"uid=3 {UNVERIFIED} [in Gmail" in out
    assert "uid=2" not in out


def test_list_json_is_machine_readable(mailbox, capsys):
    assert cli.main(["list-failed", "--json"]) == 0
    entries = json.loads(capsys.readouterr().out)
    assert [(e["uid"], e["keyword"], e["in_gmail"]) for e in entries] == [
        (1, FAILED, False),
        (3, UNVERIFIED, True),
    ]
    assert entries[0]["subject"] == "mail 1"


def test_retry_clears_the_failed_keyword(mailbox, capsys):
    assert cli.main(["retry", "--uid", "1"]) == 0
    assert mailbox.flags[1] == set()
    assert "cleared" in capsys.readouterr().out


def test_retry_all_only_touches_failed_mail(mailbox):
    assert cli.main(["retry", "--all"]) == 0
    assert mailbox.flags[1] == set()
    assert mailbox.flags[3] == {UNVERIFIED}  # still in Gmail: not re-queued by accident


def test_retry_unverified_needs_explicit_confirmation(mailbox, capsys):
    assert cli.main(["retry", "--all", "--unverified"]) == 2
    assert mailbox.flags[3] == {UNVERIFIED}
    assert "duplicate" in capsys.readouterr().err

    assert cli.main(["retry", "--all", "--unverified", "--yes"]) == 0
    assert mailbox.flags[3] == set()


def test_mark_failed_takes_a_message_out_of_the_queue(mailbox):
    assert cli.main(["mark-failed", "--uid", "2"]) == 0
    assert mailbox.flags[2] == {FAILED}


def test_mark_failed_refuses_when_keywords_are_unsupported(mailbox, capsys):
    mailbox.keyword_support = False
    assert cli.main(["mark-failed", "--uid", "2"]) == 1
    assert mailbox.flags[2] == set()
    assert "error:" in capsys.readouterr().err


def test_retry_requires_a_target(mailbox):
    with pytest.raises(SystemExit):
        cli.main(["retry"])


def test_retry_reports_a_uid_that_is_not_quarantined(mailbox, capsys):
    assert cli.main(["retry", "--uid", "3"]) == 0  # uid 3 carries the unverified keyword
    assert mailbox.flags[3] == {UNVERIFIED}
    assert "does not carry" in capsys.readouterr().err
