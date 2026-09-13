from __future__ import annotations

from mailtransporter.watcher import BUSY_DELAY, SHORT_DELAY, TriggerResult, WatchLoop

from conftest import FakeClock, FakeMailbox


class FakeNotifier:
    def __init__(self):
        self.calls = 0
        self.results: list[TriggerResult] = []

    def trigger(self):
        self.calls += 1
        if self.results:
            return self.results.pop(0)
        return TriggerResult(ok=True)


def test_empty_inbox_does_not_trigger():
    notifier, clock = FakeNotifier(), FakeClock()
    loop = WatchLoop(notifier, clock=clock)
    assert loop.step(FakeMailbox()) is None
    assert notifier.calls == 0


def test_new_uid_triggers_once_until_retrigger_interval():
    notifier, clock = FakeNotifier(), FakeClock()
    loop = WatchLoop(notifier, retrigger_interval=600, clock=clock)
    mailbox = FakeMailbox({1: b"x"})

    assert loop.step(mailbox) is None
    assert notifier.calls == 1
    # same UID still sitting there (e.g. Gmail is down): don't hammer the forwarder
    clock.advance(100)
    assert loop.step(mailbox) is None
    assert notifier.calls == 1
    # ...but do re-trigger after the interval
    clock.advance(600)
    assert loop.step(mailbox) is None
    assert notifier.calls == 2


def test_additional_new_uid_triggers_immediately():
    notifier, clock = FakeNotifier(), FakeClock()
    loop = WatchLoop(notifier, clock=clock)
    mailbox = FakeMailbox({1: b"x"})
    loop.step(mailbox)
    mailbox.inbox[2] = b"y"
    loop.step(mailbox)
    assert notifier.calls == 2


def test_remaining_work_requests_short_delay():
    notifier, clock = FakeNotifier(), FakeClock()
    notifier.results.append(TriggerResult(ok=True, remaining=5))
    loop = WatchLoop(notifier, clock=clock)
    mailbox = FakeMailbox({1: b"x"})
    assert loop.step(mailbox) == SHORT_DELAY
    clock.advance(SHORT_DELAY)
    assert loop.step(mailbox) is None
    assert notifier.calls == 2


def test_busy_forwarder_is_retried_after_short_delay():
    notifier, clock = FakeNotifier(), FakeClock()
    notifier.results.append(TriggerResult(ok=False, busy=True))
    loop = WatchLoop(notifier, clock=clock)
    mailbox = FakeMailbox({1: b"x"})
    assert loop.step(mailbox) == BUSY_DELAY
    assert loop.step(mailbox) == BUSY_DELAY  # not yet due: returns the wait time
    assert notifier.calls == 1
    clock.advance(BUSY_DELAY)
    assert loop.step(mailbox) is None
    assert notifier.calls == 2


def test_failures_back_off_exponentially():
    notifier, clock = FakeNotifier(), FakeClock()
    notifier.results.extend([TriggerResult(ok=False, error="500")] * 3)
    loop = WatchLoop(notifier, clock=clock)
    mailbox = FakeMailbox({1: b"x"})
    d1 = loop.step(mailbox)
    clock.advance(d1)
    d2 = loop.step(mailbox)
    clock.advance(d2)
    d3 = loop.step(mailbox)
    assert (d1, d2, d3) == (30, 60, 120)
    clock.advance(d3)
    assert loop.step(mailbox) is None  # success resets
    assert notifier.calls == 4


def test_forgets_uids_that_left_the_inbox():
    notifier, clock = FakeNotifier(), FakeClock()
    loop = WatchLoop(notifier, clock=clock)
    mailbox = FakeMailbox({1: b"x"})
    loop.step(mailbox)
    del mailbox.inbox[1]
    assert loop.step(mailbox) is None
    mailbox.inbox[1] = b"x"  # same uid cannot normally reappear, but be safe
    loop.step(mailbox)
    assert notifier.calls == 2


def test_run_forever_keeps_heartbeat_alive_and_reconnects():
    from mailtransporter.config import ICloudSettings, WatcherSettings
    from mailtransporter.health import Heartbeat
    from mailtransporter.imap_client import MailboxError

    clock = FakeClock()
    settings = WatcherSettings(
        icloud=ICloudSettings(user="u", password="p", timeout=10),
        forwarder_url="https://x",
        idle_timeout=30,
        request_timeout=100,
    )
    hb = Heartbeat(startup_grace=1, clock=clock)
    notifier = FakeNotifier()
    slept: list[float] = []
    mailbox = FakeMailbox({1: b"x"})
    sessions: list[FakeMailbox] = []

    class DroppingMailbox(FakeMailbox):
        """IDLE raises once so run_forever has to reconnect."""

        def idle_wait(self, timeout):
            if not self.idle_events:
                raise MailboxError("dropped")
            return self.idle_events.pop(0)

    def factory():
        mb = DroppingMailbox(dict(mailbox.inbox))
        mb.idle_events = [True]
        sessions.append(mb)
        return mb

    def sleep(seconds):
        slept.append(seconds)
        clock.advance(seconds)

    from mailtransporter.watcher import run_forever

    run_forever(settings, notifier, heartbeat=hb, mailbox_factory=factory, sleep=sleep, max_sessions=2)

    assert len(sessions) == 2            # reconnected after the IMAP drop
    assert notifier.calls == 1           # the loop survives reconnects: same UID is not re-reported
    assert hb.ok()                       # heartbeat was refreshed before every blocking call
    assert slept and slept[0] == 5.0     # reconnect backoff
