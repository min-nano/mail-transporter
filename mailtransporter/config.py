"""Settings objects, populated from environment variables."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .secrets import ConfigError, env_int, require_env, resolve_secret_env

DEFAULT_IMAP_HOST = "imap.mail.me.com"
DEFAULT_IMAP_PORT = 993


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
    inserted_keyword: str = "$GmailInserted"
    time_budget_seconds: int = 480

    @classmethod
    def from_env(cls) -> "ForwarderSettings":
        keyword = os.environ.get("INSERTED_KEYWORD", "$GmailInserted").strip()
        if not keyword or any(c in keyword for c in ' ()\\{"%*]') or not keyword.isascii():
            raise ConfigError("INSERTED_KEYWORD must be a plain ASCII IMAP atom such as $GmailInserted")
        return cls(
            icloud=ICloudSettings.from_env(),
            gmail=GmailSettings.from_env(),
            failed_folder=os.environ.get("FAILED_FOLDER", "Forward-Failed"),
            inserted_keyword=keyword,
            time_budget_seconds=env_int("TIME_BUDGET_SECONDS", 480),
        )


@dataclass(frozen=True)
class WatcherSettings:
    icloud: ICloudSettings
    forwarder_url: str
    retrigger_interval: int = 600
    idle_timeout: int = 240
    poll_interval: int = 60
    request_timeout: int = 900
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "WatcherSettings":
        return cls(
            icloud=ICloudSettings.from_env(),
            forwarder_url=require_env("FORWARDER_URL").rstrip("/"),
            retrigger_interval=env_int("RETRIGGER_INTERVAL", 600),
            idle_timeout=env_int("IDLE_TIMEOUT", 240),
            poll_interval=env_int("POLL_INTERVAL", 60),
            request_timeout=env_int("REQUEST_TIMEOUT", 900),
        )
