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


def _forwarder_env(monkeypatch):
    monkeypatch.setenv("ICLOUD_USER", "me@icloud.com")
    monkeypatch.setenv("ICLOUD_PASSWORD", "app-pass")
    monkeypatch.setenv("GMAIL_OAUTH_JSON", json.dumps({"client_id": "a", "client_secret": "b", "refresh_token": "c"}))


def test_forwarder_settings(monkeypatch):
    _forwarder_env(monkeypatch)
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


def test_forwarder_settings_quarantine_defaults(monkeypatch):
    _forwarder_env(monkeypatch)
    s = ForwarderSettings.from_env()
    assert s.quarantine_mode == "keyword"
    assert s.quarantine_keywords == ("$GmailFailed", "$GmailUnverified")
    # the folders stay configured as the fallback for a server refusing keywords
    assert (s.failed_folder, s.unverified_folder) == ("Forward-Failed", "Forward-Unverified")


def test_forwarder_settings_rejects_bad_quarantine_settings(monkeypatch):
    _forwarder_env(monkeypatch)
    monkeypatch.setenv("QUARANTINE_MODE", "delete")
    with pytest.raises(ConfigError, match="QUARANTINE_MODE"):
        ForwarderSettings.from_env()
    monkeypatch.setenv("QUARANTINE_MODE", "folder")
    assert ForwarderSettings.from_env().quarantine_mode == "folder"

    monkeypatch.setenv("FAILED_KEYWORD", "$Junk")  # Apple Mail sets this itself
    with pytest.raises(ConfigError, match="FAILED_KEYWORD"):
        ForwarderSettings.from_env()

    # the same keyword for two meanings would hide mail that still needs forwarding
    monkeypatch.setenv("FAILED_KEYWORD", "$GmailInserted")
    with pytest.raises(ConfigError, match="must all differ"):
        ForwarderSettings.from_env()


def test_watcher_settings_skip_quarantined_mail(monkeypatch):
    monkeypatch.setenv("ICLOUD_USER", "me@icloud.com")
    monkeypatch.setenv("ICLOUD_PASSWORD", "app-pass")
    monkeypatch.setenv("FORWARDER_URL", "https://x.a.run.app")
    monkeypatch.setenv("FAILED_KEYWORD", "$Nope")
    # without this the INBOX never looks empty and the forwarder is poked forever
    assert WatcherSettings.from_env().quarantine_keywords == ("$Nope", "$GmailUnverified")


def test_watcher_settings_requires_url(monkeypatch):
    monkeypatch.setenv("ICLOUD_USER", "me@icloud.com")
    monkeypatch.setenv("ICLOUD_PASSWORD", "app-pass")
    monkeypatch.delenv("FORWARDER_URL", raising=False)
    with pytest.raises(ConfigError):
        WatcherSettings.from_env()
    monkeypatch.setenv("FORWARDER_URL", "https://x.a.run.app/")
    assert WatcherSettings.from_env().forwarder_url == "https://x.a.run.app"


def test_quarantine_keywords_must_differ_for_the_watcher_too(monkeypatch):
    """The watcher reads them through this function alone, so it validates here."""
    from mailtransporter.config import quarantine_keywords_from_env

    monkeypatch.setenv("FAILED_KEYWORD", "$Same")
    monkeypatch.setenv("UNVERIFIED_KEYWORD", "$same")
    with pytest.raises(ConfigError, match="must differ"):
        quarantine_keywords_from_env()

    monkeypatch.setenv("ICLOUD_USER", "me@icloud.com")
    monkeypatch.setenv("ICLOUD_PASSWORD", "app-pass")
    monkeypatch.setenv("FORWARDER_URL", "https://x.a.run.app")
    with pytest.raises(ConfigError, match="must differ"):
        WatcherSettings.from_env()
