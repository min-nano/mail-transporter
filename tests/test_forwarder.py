from __future__ import annotations

from mailtransporter.forwarder import DEFAULT_INSERTED_KEYWORD as KW
from mailtransporter.gmail_client import GmailAuthError, GmailPermanentError, GmailRetryableError

from conftest import FakeClock, make_forwarder, make_raw


def test_new_message_is_inserted_flagged_then_trashed(mailbox, gmail):
    mailbox.inbox[1] = make_raw()
    result = make_forwarder(mailbox, gmail).run()

    assert result.ok
    assert (result.listed, result.forwarded, result.trashed) == (1, 1, 1)
    assert gmail.inserted[0][1] == ["INBOX", "UNREAD", "Label_1"]
    assert mailbox.inbox == {}
    assert mailbox.folders["Deleted Messages"] == [1]
    assert KW in mailbox.flags[1]


def test_flags_map_to_gmail_labels(mailbox, gmail):
    mailbox.inbox[1] = make_raw()
    mailbox.flags[1] = frozenset({"\\Seen", "\\Flagged"})
    make_forwarder(mailbox, gmail).run()
    assert gmail.inserted[0][1] == ["INBOX", "STARRED", "Label_1"]


def test_no_custom_label(mailbox, gmail):
    mailbox.inbox[1] = make_raw()
    make_forwarder(mailbox, gmail, label=None).run()
    assert gmail.inserted[0][1] == ["INBOX", "UNREAD"]
    assert gmail.labels == {}


def test_keyword_present_skips_insert(mailbox, gmail):
    """Crash between keyword and trash: the next run must only trash."""
    mailbox.inbox[1] = make_raw()
    mailbox.flags[1] = frozenset({KW})
    result = make_forwarder(mailbox, gmail).run()
    assert result.ok
    assert result.forwarded == 0 and result.trashed == 1
    assert gmail.inserted == []
    assert mailbox.folders["Deleted Messages"] == [1]


def test_trash_failure_after_insert_is_not_inserted_twice(mailbox, gmail):
    mailbox.inbox[1] = make_raw()
    mailbox.fail_move.add(1)
    first = make_forwarder(mailbox, gmail).run()
    assert not first.ok
    assert first.forwarded == 1 and first.trashed == 0
    assert KW in mailbox.flags[1]
    assert 1 in mailbox.inbox

    mailbox.fail_move.clear()
    second = make_forwarder(mailbox, gmail).run()
    assert second.ok
    assert second.forwarded == 0 and second.trashed == 1
    assert len(gmail.inserted) == 1


def test_keyword_failure_after_insert_aborts_and_next_run_duplicates_rather_than_loses(mailbox, gmail):
    mailbox.inbox[1] = make_raw("<x@example.com>")
    mailbox.fail_keyword.add(1)
    first = make_forwarder(mailbox, gmail).run()
    assert not first.ok and "flagging" in first.error
    assert len(gmail.inserted) == 1
    assert 1 in mailbox.inbox  # never trashed without the keyword

    mailbox.fail_keyword.clear()
    second = make_forwarder(mailbox, gmail).run()
    assert second.ok and second.trashed == 1
    assert len(gmail.inserted) == 2  # a duplicate in Gmail, not a lost mail


def test_keyword_match_is_case_insensitive(mailbox, gmail):
    mailbox.inbox[1] = make_raw()
    mailbox.flags[1] = frozenset({KW.lower()})
    result = make_forwarder(mailbox, gmail).run()
    assert result.trashed == 1 and gmail.inserted == []


def test_retryable_gmail_error_leaves_message_in_inbox(mailbox, gmail):
    mailbox.inbox[1] = make_raw()
    gmail.errors.append(GmailRetryableError("503"))
    result = make_forwarder(mailbox, gmail).run()
    assert result.ok  # the run itself completed
    assert result.retry_later == 1 and result.forwarded == 0
    assert 1 in mailbox.inbox
    assert KW not in mailbox.flags.get(1, ())

    result = make_forwarder(mailbox, gmail).run()
    assert result.forwarded == 1 and result.trashed == 1


def test_permanent_error_moves_to_failed_folder(mailbox, gmail):
    mailbox.inbox[1] = make_raw()
    mailbox.inbox[2] = make_raw("<ok@example.com>")
    gmail.errors.append(GmailPermanentError("400 invalid"))
    result = make_forwarder(mailbox, gmail).run()
    assert result.ok
    assert result.quarantined == 1 and result.forwarded == 1
    assert mailbox.folders["Forward-Failed"] == [1]
    assert mailbox.folders["Deleted Messages"] == [2]


def test_systemic_rejections_do_not_quarantine(mailbox, gmail):
    """Every message rejected and nothing accepted: the account is broken, not the mail."""
    for uid in range(1, 5):
        mailbox.inbox[uid] = make_raw(f"<{uid}@example.com>")
    gmail.errors.extend([GmailPermanentError("400 bad label")] * 4)
    result = make_forwarder(mailbox, gmail, rejection_threshold=3).run()
    assert not result.ok and "rejected" in result.error
    assert result.quarantined == 0
    assert set(mailbox.inbox) == {1, 2, 3, 4}
    assert "Forward-Failed" not in mailbox.folders


def test_rejections_below_threshold_are_quarantined_even_without_successes(mailbox, gmail):
    mailbox.inbox[1] = make_raw()
    mailbox.inbox[2] = make_raw("<two@example.com>")
    gmail.errors.extend([GmailPermanentError("413 too large")] * 2)
    result = make_forwarder(mailbox, gmail, rejection_threshold=3).run()
    assert result.ok and result.quarantined == 2
    assert mailbox.folders["Forward-Failed"] == [1, 2]


def test_rejections_with_a_success_are_quarantined(mailbox, gmail):
    for uid in range(1, 5):
        mailbox.inbox[uid] = make_raw(f"<{uid}@example.com>")
    gmail.errors.extend([GmailPermanentError("413")] * 3)  # uids 1-3 rejected, 4 succeeds
    result = make_forwarder(mailbox, gmail, rejection_threshold=3).run()
    assert result.ok and result.forwarded == 1 and result.quarantined == 3
    assert mailbox.folders["Forward-Failed"] == [1, 2, 3]


def test_auth_error_aborts_run(mailbox, gmail):
    mailbox.inbox[1] = make_raw()
    mailbox.inbox[2] = make_raw("<two@example.com>")
    gmail.errors.append(GmailAuthError("401"))
    result = make_forwarder(mailbox, gmail).run()
    assert not result.ok
    assert result.remaining == 2
    assert gmail.inserted == []
    assert set(mailbox.inbox) == {1, 2}


def test_imap_fetch_error_aborts_run(mailbox, gmail):
    mailbox.inbox[1] = make_raw()
    mailbox.inbox[2] = make_raw("<two@example.com>")
    mailbox.fail_fetch.add(1)
    result = make_forwarder(mailbox, gmail).run()
    assert not result.ok and result.remaining == 2
    assert gmail.inserted == []


def test_server_without_keyword_support_refuses_to_forward(mailbox, gmail):
    mailbox.inbox[1] = make_raw()
    mailbox.supports_keywords = False
    result = make_forwarder(mailbox, gmail).run()
    assert not result.ok and "keyword" in result.error
    assert gmail.inserted == [] and 1 in mailbox.inbox


def test_message_gone_before_fetch(mailbox, gmail):
    mailbox.inbox[1] = make_raw()
    original = mailbox.fetch_message

    def vanish(uid):
        del mailbox.inbox[uid]
        return original(uid)

    mailbox.fetch_message = vanish
    result = make_forwarder(mailbox, gmail).run()
    assert result.skipped == 1 and gmail.inserted == []


def test_time_budget_leaves_remaining(mailbox, gmail):
    clock = FakeClock()
    for uid in range(1, 5):
        mailbox.inbox[uid] = make_raw(f"<{uid}@example.com>")
    original_insert = gmail.insert_raw

    def slow_insert(raw, labels):
        clock.advance(60)
        return original_insert(raw, labels)

    gmail.insert_raw = slow_insert
    result = make_forwarder(mailbox, gmail, clock, time_budget_seconds=100).run()
    assert result.ok
    assert result.forwarded == 2 and result.remaining == 2
    assert set(mailbox.inbox) == {3, 4}


def test_custom_keyword_name(mailbox, gmail):
    mailbox.inbox[1] = make_raw()
    make_forwarder(mailbox, gmail, inserted_keyword="$Moved").run()
    assert "$Moved" in mailbox.flags[1]


def test_message_without_message_id_is_forwarded(mailbox, gmail):
    mailbox.inbox[1] = make_raw(message_id=None)
    assert make_forwarder(mailbox, gmail).run().forwarded == 1
