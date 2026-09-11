from __future__ import annotations

from mailtransporter import store as st
from mailtransporter.forwarder import extract_message_id, message_key
from mailtransporter.gmail_client import GmailAuthError, GmailPermanentError, GmailRetryableError

from conftest import FakeClock, make_forwarder, make_raw


def test_new_message_is_inserted_then_trashed(mailbox, gmail, store):
    mailbox.inbox[1] = make_raw()
    result = make_forwarder(mailbox, gmail, store).run()

    assert result.ok
    assert (result.listed, result.forwarded, result.trashed) == (1, 1, 1)
    assert gmail.inserted[0][1] == ["INBOX", "UNREAD", "Label_1"]
    assert mailbox.inbox == {}
    assert mailbox.folders["Deleted Messages"] == [1]
    record = store.records[message_key(7, 1)]
    assert record.status == st.DONE
    assert record.gmail_id == "gmail-1"
    assert record.message_id == "<abc@example.com>"


def test_flags_map_to_gmail_labels(mailbox, gmail, store):
    mailbox.inbox[1] = make_raw()
    mailbox.flags[1] = frozenset({"\\Seen", "\\Flagged"})
    make_forwarder(mailbox, gmail, store).run()
    assert gmail.inserted[0][1] == ["INBOX", "STARRED", "Label_1"]


def test_no_custom_label(mailbox, gmail, store):
    mailbox.inbox[1] = make_raw()
    make_forwarder(mailbox, gmail, store, label=None).run()
    assert gmail.inserted[0][1] == ["INBOX", "UNREAD"]
    assert gmail.labels == {}


def test_inserted_but_not_trashed_is_not_inserted_twice(mailbox, gmail, store):
    """Crash between Gmail insert and IMAP move: the second run must only trash."""
    mailbox.inbox[1] = make_raw()
    mailbox.fail_move.add(1)
    first = make_forwarder(mailbox, gmail, store).run()
    assert not first.ok
    assert first.forwarded == 1 and first.trashed == 0
    assert store.records[message_key(7, 1)].status == st.INSERTED
    assert mailbox.inbox  # still there

    mailbox.fail_move.clear()
    second = make_forwarder(mailbox, gmail, store).run()
    assert second.ok
    assert second.forwarded == 0 and second.trashed == 1
    assert len(gmail.inserted) == 1
    assert store.records[message_key(7, 1)].status == st.DONE


def test_existing_message_id_in_gmail_skips_insert(mailbox, gmail, store):
    mailbox.inbox[1] = make_raw("<dup@example.com>")
    gmail.existing_message_ids["dup@example.com"] = "gmail-existing"
    result = make_forwarder(mailbox, gmail, store).run()
    assert result.ok and result.trashed == 1
    assert gmail.inserted == []
    assert store.records[message_key(7, 1)].gmail_id == "gmail-existing"


def test_retryable_gmail_error_leaves_message_in_inbox(mailbox, gmail, store):
    mailbox.inbox[1] = make_raw()
    gmail.errors.append(GmailRetryableError("503"))
    result = make_forwarder(mailbox, gmail, store).run()
    assert result.ok  # the run itself completed
    assert result.retry_later == 1 and result.forwarded == 0
    assert 1 in mailbox.inbox
    record = store.records[message_key(7, 1)]
    assert record.status == st.PENDING and record.attempts == 1

    # next run succeeds
    result = make_forwarder(mailbox, gmail, store).run()
    assert result.forwarded == 1 and result.trashed == 1
    assert store.records[message_key(7, 1)].status == st.DONE


def test_retryable_errors_exhaust_attempts_then_quarantine(mailbox, gmail, store):
    mailbox.inbox[1] = make_raw()
    gmail.errors.extend([GmailRetryableError("503")] * 3)
    fwd = make_forwarder(mailbox, gmail, store, max_attempts=3)
    fwd.run()
    fwd.run()
    assert 1 in mailbox.inbox
    result = fwd.run()
    assert result.quarantined == 1
    assert mailbox.folders["Forward-Failed"] == [1]
    assert store.records[message_key(7, 1)].status == st.QUARANTINED


def test_permanent_error_moves_to_failed_folder(mailbox, gmail, store):
    mailbox.inbox[1] = make_raw()
    mailbox.inbox[2] = make_raw("<ok@example.com>")
    gmail.errors.append(GmailPermanentError("400 invalid"))
    result = make_forwarder(mailbox, gmail, store).run()
    assert result.ok
    assert result.quarantined == 1 and result.forwarded == 1
    assert mailbox.folders["Forward-Failed"] == [1]
    assert mailbox.folders["Deleted Messages"] == [2]
    assert store.records[message_key(7, 1)].status == st.QUARANTINED
    assert "400 invalid" in store.records[message_key(7, 1)].last_error


def test_auth_error_aborts_run(mailbox, gmail, store):
    mailbox.inbox[1] = make_raw()
    mailbox.inbox[2] = make_raw("<two@example.com>")
    gmail.errors.append(GmailAuthError("401"))
    result = make_forwarder(mailbox, gmail, store).run()
    assert not result.ok
    assert result.remaining == 2
    assert gmail.inserted == []
    assert set(mailbox.inbox) == {1, 2}


def test_imap_fetch_error_aborts_run(mailbox, gmail, store):
    mailbox.inbox[1] = make_raw()
    mailbox.inbox[2] = make_raw("<two@example.com>")
    mailbox.fail_fetch.add(1)
    result = make_forwarder(mailbox, gmail, store).run()
    assert not result.ok and result.remaining == 2
    assert store.records[message_key(7, 1)].attempts == 1


def test_lease_held_elsewhere_is_skipped(mailbox, gmail, store):
    mailbox.inbox[1] = make_raw()
    store.claim(message_key(7, 1), uid=1, uidvalidity=7, now=st.utcnow(), lease_seconds=600)
    result = make_forwarder(mailbox, gmail, store).run()
    assert result.skipped == 1 and gmail.inserted == []
    assert 1 in mailbox.inbox


def test_expired_lease_is_reclaimed(mailbox, gmail, store):
    mailbox.inbox[1] = make_raw()
    store.claim(message_key(7, 1), uid=1, uidvalidity=7, now=st.utcnow(), lease_seconds=-1)
    result = make_forwarder(mailbox, gmail, store).run()
    assert result.forwarded == 1


def test_message_gone_before_fetch(mailbox, gmail, store):
    mailbox.inbox[1] = make_raw()
    original = mailbox.fetch_message

    def vanish(uid):
        del mailbox.inbox[uid]
        return original(uid)

    mailbox.fetch_message = vanish
    result = make_forwarder(mailbox, gmail, store).run()
    assert result.skipped == 1 and gmail.inserted == []


def test_time_budget_leaves_remaining(mailbox, gmail, store):
    clock = FakeClock()
    for uid in range(1, 5):
        mailbox.inbox[uid] = make_raw(f"<{uid}@example.com>")
    original_insert = gmail.insert_raw

    def slow_insert(raw, labels):
        clock.advance(60)
        return original_insert(raw, labels)

    gmail.insert_raw = slow_insert
    result = make_forwarder(mailbox, gmail, store, clock, time_budget_seconds=100).run()
    assert result.ok
    assert result.forwarded == 2 and result.remaining == 2
    assert set(mailbox.inbox) == {3, 4}


def test_quarantined_record_still_in_inbox_is_moved_again(mailbox, gmail, store):
    mailbox.inbox[1] = make_raw()
    key = message_key(7, 1)
    store.claim(key, uid=1, uidvalidity=7, now=st.utcnow(), lease_seconds=1)
    store.mark_rejected(key, error="bad")
    result = make_forwarder(mailbox, gmail, store).run()
    assert result.quarantined == 1 and gmail.inserted == []
    assert mailbox.folders["Forward-Failed"] == [1]


def test_missing_message_id_is_inserted_without_lookup(mailbox, gmail, store):
    mailbox.inbox[1] = make_raw(message_id=None)
    result = make_forwarder(mailbox, gmail, store).run()
    assert result.forwarded == 1
    assert store.records[message_key(7, 1)].message_id is None


def test_extract_message_id():
    assert extract_message_id(make_raw("<x@y>")) == "<x@y>"
    assert extract_message_id(make_raw(None)) is None
    assert extract_message_id(b"Message-ID:\r\n <folded@\r\n example.com>\r\n\r\n") == "<folded@example.com>"
    assert extract_message_id(b"\xff\xfe not mail") is None
