"""Settings objects, populated from environment variables."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import NamedTuple

from .secrets import ConfigError, env_int, register_secret, require_env, resolve_secret_env

DEFAULT_IMAP_HOST = "imap.mail.me.com"
DEFAULT_IMAP_PORT = 993

DEFAULT_INSERTED_KEYWORD = "$GmailInserted"
DEFAULT_FAILED_KEYWORD = "$GmailFailed"
DEFAULT_UNVERIFIED_KEYWORD = "$GmailUnverified"

# How a message that must leave the queue is marked. "keyword" keeps it in the
# INBOX and tags it, leaving the iCloud folder tree untouched; "folder" moves it
# into a dedicated folder (the previous behaviour).
QUARANTINE_MODES = ("keyword", "folder")

# Keywords iCloud / Apple Mail (and other clients) set themselves. Using one of
# them as INSERTED_KEYWORD would make existing mail look "already forwarded"
# and send it to the Trash without ever reaching Gmail; using one as a
# quarantine keyword would hide existing mail from the queue for good.
RESERVED_KEYWORDS = {
    "$forwarded", "$junk", "$notjunk", "$mdnsent", "$phishing", "$important",
    "$label1", "$label2", "$label3", "$label4", "$label5",
    "$mailflagbit0", "$mailflagbit1", "$mailflagbit2",
    "junk", "nonjunk", "forwarded", "redirected",
}


def keyword_from_env(name: str, default: str, *, reserved_hint: str) -> str:
    """Read and validate a custom IMAP keyword from the environment.

    IMAP keywords are ASCII atoms (RFC 3501), and a keyword mail clients set
    themselves must never be reused: existing mail already carrying it would
    be misread as progress this tool recorded.
    """
    keyword = os.environ.get(name, default).strip()
    if not keyword or any(c in keyword for c in ' ()\\{"%*]') or not keyword.isascii():
        raise ConfigError(f"{name} must be a plain ASCII IMAP atom such as {default}")
    if keyword.lower() in RESERVED_KEYWORDS or keyword.lower().startswith("\\"):
        raise ConfigError(
            f"{name}={keyword!r} is a keyword mail clients set themselves; {reserved_hint}"
        )
    return keyword


class Keywords(NamedTuple):
    """The three keywords this tool writes on iCloud messages."""

    inserted: str
    failed: str
    unverified: str


_QUARANTINE_HINT = "existing mail carrying it would silently drop out of the forwarding queue"


def quarantine_keywords_from_env() -> tuple[str, str]:
    """The (failed, unverified) keywords, shared by the forwarder and watcher.

    Both must be excluded from the INBOX listing: the forwarder would otherwise
    re-send rejected mail to Gmail on every run, and the watcher would keep
    re-triggering it forever because the INBOX never looks empty. They are
    validated here rather than only in :class:`ForwarderSettings` so that the
    watcher, which reads them through this function alone, fails closed too.
    """
    failed = keyword_from_env("FAILED_KEYWORD", DEFAULT_FAILED_KEYWORD, reserved_hint=_QUARANTINE_HINT)
    unverified = keyword_from_env("UNVERIFIED_KEYWORD", DEFAULT_UNVERIFIED_KEYWORD, reserved_hint=_QUARANTINE_HINT)
    if failed.lower() == unverified.lower():
        raise ConfigError(
            "FAILED_KEYWORD and UNVERIFIED_KEYWORD must differ; one means the message is NOT in "
            f"Gmail and the other that it IS, and both are {failed!r}"
        )
    return failed, unverified


def keywords_from_env() -> Keywords:
    """All three keywords, validated to be distinct from one another."""
    inserted = keyword_from_env(
        "INSERTED_KEYWORD",
        DEFAULT_INSERTED_KEYWORD,
        reserved_hint="existing mail carrying it would be trashed without being forwarded",
    )
    failed, unverified = quarantine_keywords_from_env()
    names = [inserted.lower(), failed.lower(), unverified.lower()]
    if len(set(names)) != len(names):
        raise ConfigError(
            "INSERTED_KEYWORD, FAILED_KEYWORD and UNVERIFIED_KEYWORD must all differ; "
            f"got {inserted!r}, {failed!r}, {unverified!r}"
        )
    return Keywords(inserted, failed, unverified)


@dataclass(frozen=True)
class ICloudSettings:
    user: str
    password: str
    host: str = DEFAULT_IMAP_HOST
    port: int = DEFAULT_IMAP_PORT
    timeout: int = 60

    @classmethod
    def from_env(cls) -> "ICloudSettings":
        return cls(
            user=require_env("ICLOUD_USER"),
            password=resolve_secret_env("ICLOUD_PASSWORD"),
            host=os.environ.get("IMAP_HOST", DEFAULT_IMAP_HOST),
            port=env_int("IMAP_PORT", DEFAULT_IMAP_PORT),
            timeout=env_int("IMAP_TIMEOUT", 60),
        )


@dataclass(frozen=True)
class GmailSettings:
    client_id: str
    client_secret: str
    refresh_token: str
    label: str | None = "iCloud"

    @classmethod
    def from_env(cls) -> "GmailSettings":
        raw = resolve_secret_env("GMAIL_OAUTH_JSON")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError("GMAIL_OAUTH_JSON is not valid JSON") from exc
        # Accept both the flat layout produced by scripts/gmail_oauth.py and the
        # "installed" layout of a downloaded client_secret.json (+ refresh_token).
        if "installed" in data:
            data = {**data["installed"], **{k: v for k, v in data.items() if k != "installed"}}
        missing = [k for k in ("client_id", "client_secret", "refresh_token") if not data.get(k)]
        if missing:
            raise ConfigError(f"GMAIL_OAUTH_JSON is missing keys: {', '.join(missing)}")
        label = os.environ.get("GMAIL_LABEL", "iCloud").strip() or None
        register_secret(data["client_secret"])
        register_secret(data["refresh_token"])
        return cls(
            client_id=data["client_id"],
            client_secret=data["client_secret"],
            refresh_token=data["refresh_token"],
            label=label,
        )


@dataclass(frozen=True)
class ForwarderSettings:
    icloud: ICloudSettings
    gmail: GmailSettings
    failed_folder: str = "Forward-Failed"
    unverified_folder: str = "Forward-Unverified"
    inserted_keyword: str = DEFAULT_INSERTED_KEYWORD
    failed_keyword: str = DEFAULT_FAILED_KEYWORD
    unverified_keyword: str = DEFAULT_UNVERIFIED_KEYWORD
    quarantine_mode: str = "keyword"
    time_budget_seconds: int = 480
    rejection_threshold: int = 3

    @property
    def quarantine_keywords(self) -> tuple[str, str]:
        return (self.failed_keyword, self.unverified_keyword)

    @classmethod
    def from_env(cls) -> "ForwarderSettings":
        keywords = keywords_from_env()
        mode = os.environ.get("QUARANTINE_MODE", "keyword").strip().lower() or "keyword"
        if mode not in QUARANTINE_MODES:
            raise ConfigError(f"QUARANTINE_MODE must be one of {', '.join(QUARANTINE_MODES)}; got {mode!r}")
        return cls(
            icloud=ICloudSettings.from_env(),
            gmail=GmailSettings.from_env(),
            failed_folder=os.environ.get("FAILED_FOLDER", "Forward-Failed"),
            unverified_folder=os.environ.get("UNVERIFIED_FOLDER", "Forward-Unverified"),
            inserted_keyword=keywords.inserted,
            failed_keyword=keywords.failed,
            unverified_keyword=keywords.unverified,
            quarantine_mode=mode,
            time_budget_seconds=env_int("TIME_BUDGET_SECONDS", 480),
            rejection_threshold=env_int("REJECTION_THRESHOLD", 3),
        )


@dataclass(frozen=True)
class WatcherSettings:
    icloud: ICloudSettings
    forwarder_url: str
    retrigger_interval: int = 600
    idle_timeout: int = 240
    poll_interval: int = 60
    request_timeout: int = 900
    health_port: int = 8080
    health_startup_grace: int = 300
    # Quarantined mail stays in the INBOX under a keyword, so the watcher has
    # to skip it too or it would re-trigger the forwarder forever.
    quarantine_keywords: tuple[str, ...] = ()
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "WatcherSettings":
        return cls(
            icloud=ICloudSettings.from_env(),
            forwarder_url=require_env("FORWARDER_URL").rstrip("/"),
            quarantine_keywords=quarantine_keywords_from_env(),
            retrigger_interval=env_int("RETRIGGER_INTERVAL", 600),
            idle_timeout=env_int("IDLE_TIMEOUT", 240),
            poll_interval=env_int("POLL_INTERVAL", 60),
            request_timeout=env_int("REQUEST_TIMEOUT", 900),
            health_port=env_int("HEALTH_PORT", 8080),
            health_startup_grace=env_int("HEALTH_STARTUP_GRACE", 300),
        )
