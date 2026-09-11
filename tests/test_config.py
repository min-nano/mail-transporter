from __future__ import annotations

import json

import pytest

from mailtransporter.config import ForwarderSettings, GmailSettings, WatcherSettings
from mailtransporter.secrets import ConfigError


def test_gmail_settings_flat_json(monkeypatch):
    monkeypatch.setenv("GMAIL_OAUTH_JSON", json.dumps({"client_id": "a", "client_secret": "b", "refresh_token": "c"}))
    s = GmailSettings.from_env()
    assert (s.client_id, s.client_secret, s.refresh_token, s.label) == ("a", "b", "c", "iCloud")


def test_gmail_settings_installed_layout_and_label(monkeypatch):
    monkeypatch.setenv(
        "GMAIL_OAUTH_JSON",
        json.dumps({"installed": {"client_id": "a", "client_secret": "b"}, "refresh_token": "c"}),
    )
    monkeypatch.setenv("GMAIL_LABEL", "")
    s = GmailSettings.from_env()
    assert s.client_id == "a" and s.label is None


def test_gmail_settings_missing_keys(monkeypatch):
    monkeypatch.setenv("GMAIL_OAUTH_JSON", json.dumps({"client_id": "a"}))
    with pytest.raises(ConfigError):
        GmailSettings.from_env()


def test_forwarder_settings(monkeypatch):
    monkeypatch.setenv("ICLOUD_USER", "me@icloud.com")
    monkeypatch.setenv("ICLOUD_PASSWORD", "app-pass")
    monkeypatch.setenv("GMAIL_OAUTH_JSON", json.dumps({"client_id": "a", "client_secret": "b", "refresh_token": "c"}))
    monkeypatch.setenv("TIME_BUDGET_SECONDS", "42")
    s = ForwarderSettings.from_env()
    assert s.icloud.host == "imap.mail.me.com"
    assert s.time_budget_seconds == 42
    assert s.inserted_keyword == "$GmailInserted"


def test_forwarder_settings_rejects_bad_keyword(monkeypatch):
    monkeypatch.setenv("ICLOUD_USER", "me@icloud.com")
    monkeypatch.setenv("ICLOUD_PASSWORD", "app-pass")
    monkeypatch.setenv("GMAIL_OAUTH_JSON", json.dumps({"client_id": "a", "client_secret": "b", "refresh_token": "c"}))
    monkeypatch.setenv("INSERTED_KEYWORD", "\\Seen")
    with pytest.raises(ConfigError):
        ForwarderSettings.from_env()
    monkeypatch.setenv("INSERTED_KEYWORD", "has space")
    with pytest.raises(ConfigError):
        ForwarderSettings.from_env()
    # keywords Apple Mail / iCloud set themselves would trash unforwarded mail
    for reserved in ("$Forwarded", "$junk", "$MailFlagBit0"):
        monkeypatch.setenv("INSERTED_KEYWORD", reserved)
        with pytest.raises(ConfigError):
            ForwarderSettings.from_env()


def test_watcher_settings_requires_url(monkeypatch):
    monkeypatch.setenv("ICLOUD_USER", "me@icloud.com")
    monkeypatch.setenv("ICLOUD_PASSWORD", "app-pass")
    monkeypatch.delenv("FORWARDER_URL", raising=False)
    with pytest.raises(ConfigError):
        WatcherSettings.from_env()
    monkeypatch.setenv("FORWARDER_URL", "https://x.a.run.app/")
    assert WatcherSettings.from_env().forwarder_url == "https://x.a.run.app"
