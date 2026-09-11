"""GCE entrypoint: watch the iCloud INBOX and poke the Cloud Run forwarder.

The watcher keeps no durable state.  It holds one IMAP session (using IDLE
when the server supports it, polling otherwise) and, whenever the INBOX
contains messages it has not yet reported, calls the forwarder's ``/sync``
endpoint with an OIDC identity token minted by the VM's service account.
As long as anything is still sitting in INBOX it re-triggers periodically,
so an outage of Gmail / Cloud Run simply delays delivery instead of losing
mail.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from .config import WatcherSettings
from .health import Heartbeat, serve as serve_health
from .imap_client import ICloudMailbox, MailboxError

log = logging.getLogger(__name__)

SHORT_DELAY = 5.0      # forwarder reported remaining work: call again soon
BUSY_DELAY = 20.0      # forwarder is mid-run: check back shortly
BASE_BACKOFF = 30.0
MAX_BACKOFF = 600.0
HEARTBEAT_MARGIN = 120.0  # slack added to every blocking operation's budget


@dataclass(frozen=True)
class TriggerResult:
    ok: bool
    busy: bool = False
    remaining: int = 0
    error: str | None = None


class CloudRunNotifier:
    """Calls the forwarder over HTTPS with a Google-signed identity token."""

    def __init__(self, base_url: str, *, timeout: int = 900) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _token(self) -> str:
        import google.auth.transport.requests
        import google.oauth2.id_token

        request = google.auth.transport.requests.Request()
        return google.oauth2.id_token.fetch_id_token(request, self.base_url)

    def trigger(self) -> TriggerResult:
        import requests

        try:
            token = self._token()
            response = requests.post(
                f"{self.base_url}/sync",
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            return TriggerResult(ok=False, error=f"request failed: {exc}")
        if response.status_code == 409:
            return TriggerResult(ok=False, busy=True)
        if response.status_code != 200:
            body = response.text[:500]
            return TriggerResult(ok=False, error=f"HTTP {response.status_code}: {body}")
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return TriggerResult(ok=True, remaining=int(payload.get("remaining", 0) or 0))


class WatchLoop:
    def __init__(
        self,
        notifier,
        *,
        retrigger_interval: float = 600.0,
        request_timeout: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
        heartbeat: Heartbeat | None = None,
    ) -> None:
        self._notifier = notifier
        self.retrigger_interval = retrigger_interval
        self.request_timeout = request_timeout
        self._clock = clock
        self._heartbeat = heartbeat or Heartbeat(clock=clock)
        self._notified: set[int] = set()
        self._last_trigger_at: float | None = None
        self._not_before: float = 0.0
        self._failures = 0

    def step(self, mailbox: ICloudMailbox) -> float | None:
        """Inspect INBOX once.

        Returns the number of seconds the caller should sleep before calling
        again, or ``None`` when it is fine to block in IDLE / the regular poll
        interval.
        """
        now = self._clock()
        uids = set(mailbox.list_inbox_uids())
        self._notified &= uids
        if not uids:
            return None

        new_uids = uids - self._notified
        stale = self._last_trigger_at is None or now - self._last_trigger_at >= self.retrigger_interval
        if not new_uids and not stale:
            return None
        if now < self._not_before:
            return self._not_before - now

        log.info("Triggering forwarder: %d message(s) in INBOX (%d new)", len(uids), len(new_uids))
        self._heartbeat.touch(self.request_timeout + HEARTBEAT_MARGIN, "calling forwarder")
        result = self._notifier.trigger()
        self._heartbeat.touch(HEARTBEAT_MARGIN, "forwarder answered")
        if result.ok:
            self._failures = 0
            self._notified = uids
            self._last_trigger_at = now
            self._not_before = now
            if result.remaining > 0:
                log.info("Forwarder has %d message(s) left; re-triggering shortly", result.remaining)
                self._last_trigger_at = None
                return SHORT_DELAY
            return None
        if result.busy:
            log.info("Forwarder busy; retrying in %.0fs", BUSY_DELAY)
            self._not_before = now + BUSY_DELAY
            return BUSY_DELAY
        self._failures += 1
        delay = min(BASE_BACKOFF * 2 ** (self._failures - 1), MAX_BACKOFF)
        log.warning("Forwarder trigger failed (%s); retrying in %.0fs", result.error, delay)
        self._not_before = now + delay
        return delay


def default_mailbox_factory(settings: WatcherSettings) -> Callable[[], ICloudMailbox]:
    def factory() -> ICloudMailbox:
        return ICloudMailbox(
            settings.icloud.host,
            settings.icloud.port,
            settings.icloud.user,
            settings.icloud.password,
            timeout=settings.icloud.timeout,
            readonly=True,
        )

    return factory


def run_forever(
    settings: WatcherSettings,
    notifier,
    *,
    heartbeat: Heartbeat,
    mailbox_factory: Callable[[], ICloudMailbox] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_sessions: int | None = None,
) -> None:
    """Keep one IMAP session alive and react to INBOX changes.

    ``max_sessions`` bounds the number of (re)connect attempts and exists
    only for tests; production runs until the process is killed.
    """
    mailbox_factory = mailbox_factory or default_mailbox_factory(settings)
    loop = WatchLoop(
        notifier,
        retrigger_interval=settings.retrigger_interval,
        request_timeout=settings.request_timeout,
        heartbeat=heartbeat,
    )
    connect_budget = settings.icloud.timeout * 3 + HEARTBEAT_MARGIN
    backoff = 5.0
    sessions = 0
    while max_sessions is None or sessions < max_sessions:
        sessions += 1
        try:
            heartbeat.touch(connect_budget, "connecting to IMAP")
            with mailbox_factory() as mailbox:
                backoff = 5.0
                while True:
                    heartbeat.touch(settings.icloud.timeout + HEARTBEAT_MARGIN, "listing INBOX")
                    delay = loop.step(mailbox)
                    if delay is not None:
                        heartbeat.touch(delay + HEARTBEAT_MARGIN, "sleeping")
                        sleep(delay)
                        continue
                    if mailbox.supports_idle:
                        heartbeat.touch(settings.idle_timeout + settings.icloud.timeout + HEARTBEAT_MARGIN, "IMAP IDLE")
                        mailbox.idle_wait(settings.idle_timeout)
                    else:
                        heartbeat.touch(settings.poll_interval + HEARTBEAT_MARGIN, "polling")
                        sleep(settings.poll_interval)
        except MailboxError as exc:
            log.warning("IMAP session lost (%s); reconnecting in %.0fs", exc, backoff)
        except Exception:  # noqa: BLE001 - keep the daemon alive
            log.exception("Unexpected watcher error; reconnecting in %.0fs", backoff)
        heartbeat.touch(backoff + HEARTBEAT_MARGIN, "reconnect backoff")
        sleep(backoff)
        backoff = min(backoff * 2, 300.0)


def main() -> None:
    from .runtime import configure_logging

    configure_logging()
    settings = WatcherSettings.from_env()
    heartbeat = Heartbeat(startup_grace=settings.health_startup_grace)
    serve_health(heartbeat, settings.health_port)
    notifier = CloudRunNotifier(settings.forwarder_url, timeout=settings.request_timeout)
    log.info("Watcher starting: forwarder=%s retrigger=%ss", settings.forwarder_url, settings.retrigger_interval)
    run_forever(settings, notifier, heartbeat=heartbeat)


if __name__ == "__main__":
    main()
