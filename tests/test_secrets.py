from __future__ import annotations

import logging

from mailtransporter import secrets


def test_redacting_filter_masks_message_and_traceback(monkeypatch):
    monkeypatch.setattr(secrets, "_SECRET_VALUES", set())
    secrets.register_secret("s3cret-token-value")
    secrets.register_secret("abc")  # too short to be worth masking

    logger = logging.getLogger("test.redact")
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    handler.addFilter(secrets.RedactingFilter())
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.error("login with %s failed", "s3cret-token-value")
        try:
            raise ValueError("server said: s3cret-token-value")
        except ValueError:
            logger.exception("boom")
    finally:
        logger.removeHandler(handler)

    formatted = [logging.Formatter().format(r) for r in records]
    assert formatted[0] == "login with *** failed"
    assert "s3cret" not in formatted[1] and "ValueError: server said: ***" in formatted[1]


def test_resolve_secret_env_registers_value(monkeypatch):
    monkeypatch.setattr(secrets, "_SECRET_VALUES", set())
    monkeypatch.setenv("ICLOUD_PASSWORD", "  app-specific-pw  ")
    assert secrets.resolve_secret_env("ICLOUD_PASSWORD") == "app-specific-pw"
    assert secrets.redact("pw=app-specific-pw") == "pw=***"
