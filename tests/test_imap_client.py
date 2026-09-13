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


class SearchClient(StubClient):
    """A server with a small INBOX and per-message flags."""

    def __init__(self, flags: dict[int, tuple[str, ...]], *, keyword_search: bool = True):
        super().__init__({"MOVE"})
        self.message_flags = flags
        self.keyword_search = keyword_search
        self.searches: list[list] = []

    def search(self, criteria):
        self.searches.append(list(criteria))
        if "KEYWORD" in criteria and not self.keyword_search:
            raise ValueError("BAD Unsupported search criterion")
        uids = sorted(self.message_flags)
        while "KEYWORD" in criteria:
            index = criteria.index("KEYWORD")
            keyword = criteria[index + 1].lower()
            negated = index >= 1 and criteria[index - 1] == "NOT"
            uids = [
                uid for uid in uids
                if (keyword in {f.lower() for f in self.message_flags[uid]}) is not negated
            ]
            criteria = criteria[index + 2:]
        return uids

    def fetch(self, uids, parts):
        self.calls.append(("fetch", list(uids), list(parts)))
        return {
            uid: {b"FLAGS": tuple(f.encode() for f in self.message_flags.get(uid, ()))}
            for uid in uids
        }

    def remove_flags(self, uids, flags):
        for uid in uids:
            dropped = {f.decode().lower() for f in flags}
            self.message_flags[uid] = tuple(
                f for f in self.message_flags.get(uid, ()) if f.lower() not in dropped
            )
        return {uid: tuple(f.encode() for f in self.message_flags[uid]) for uid in uids}


def mailbox_with_messages(flags, *, keyword_search=True, skip_keywords=()):
    mailbox = ICloudMailbox("imap.example", 993, "user", "pw", skip_keywords=skip_keywords)
    mailbox._client = SearchClient(flags, keyword_search=keyword_search)
    return mailbox


def test_listing_excludes_quarantined_mail_in_the_search():
    mailbox = mailbox_with_messages(
        {1: (), 2: ("$GmailFailed",), 3: ("\\Seen",), 4: ("$GmailUnverified",)},
        skip_keywords=("$GmailFailed", "$GmailUnverified"),
    )
    assert mailbox.list_inbox_uids() == [1, 3]
    assert mailbox._client.searches[0] == [
        "NOT", "DELETED", "NOT", "KEYWORD", "$GmailFailed", "NOT", "KEYWORD", "$GmailUnverified",
    ]


def test_listing_falls_back_to_client_side_filtering():
    """Some servers store keywords but refuse to SEARCH on them."""
    mailbox = mailbox_with_messages(
        {1: (), 2: ("$gmailfailed",)},
        keyword_search=False,
        skip_keywords=("$GmailFailed",),
    )
    assert mailbox.list_inbox_uids() == [1]


def test_listing_without_skip_keywords_propagates_search_errors():
    mailbox = mailbox_with_messages({1: ()}, keyword_search=False)
    mailbox._client.search = lambda criteria: (_ for _ in ()).throw(ValueError("boom"))
    with pytest.raises(MailboxError, match="IMAP search failed"):
        mailbox.list_inbox_uids()


def test_search_keyword_rechecks_the_flags():
    """A server that ignores the KEYWORD criterion must not report the whole INBOX."""
    mailbox = mailbox_with_messages({1: (), 2: ("$GmailFailed",)})
    mailbox._client.search = lambda criteria: [1, 2]
    assert mailbox.search_keyword("$GmailFailed") == [2]


def test_remove_keyword_confirms_and_fails_closed():
    mailbox = mailbox_with_messages({1: ("$GmailFailed", "\\Seen")})
    mailbox.remove_keyword(1, "$GmailFailed")
    assert mailbox._client.message_flags[1] == ("\\Seen",)

    mailbox = mailbox_with_messages({1: ("$GmailFailed",)})
    mailbox._client.remove_flags = lambda uids, flags: {}
    with pytest.raises(MailboxError, match="did not clear keyword"):
        mailbox.remove_keyword(1, "$GmailFailed")


def test_fetch_headers_decodes_mime_words():
    mailbox = mailbox_with_messages({1: ()})
    raw = (
        b"Date: Mon, 1 Jan 2024 00:00:00 +0000\r\n"
        b"From: a@example.com\r\n"
        b"Subject: =?utf-8?B?44GT44KT44Gr44Gh44Gv?=\r\n"
    )
    mailbox._client.fetch = lambda uids, parts: {1: {b"BODY[HEADER.FIELDS (DATE FROM SUBJECT)]": raw}}
    assert mailbox.fetch_headers([1])[1] == {
        "date": "Mon, 1 Jan 2024 00:00:00 +0000",
        "from": "a@example.com",
        "subject": "こんにちは",
    }
