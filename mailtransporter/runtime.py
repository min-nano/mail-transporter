"""Wiring: build a Forwarder from settings (shared by the server and the CLI)."""

from __future__ import annotations

import logging
import os

from .config import ForwarderSettings
from .forwarder import Forwarder, ForwarderOptions
from .gmail_client import GmailClient, build_credentials
from .secrets import RedactingFilter
from .imap_client import ICloudMailbox


def configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)


def build_forwarder(settings: ForwarderSettings | None = None) -> Forwarder:
    settings = settings or ForwarderSettings.from_env()
    credentials = build_credentials(
        settings.gmail.client_id, settings.gmail.client_secret, settings.gmail.refresh_token
    )

    def mailbox_factory() -> ICloudMailbox:
        return ICloudMailbox(
            settings.icloud.host,
            settings.icloud.port,
            settings.icloud.user,
            settings.icloud.password,
            timeout=settings.icloud.timeout,
        )

    def gmail_factory() -> GmailClient:
        return GmailClient(credentials)

    options = ForwarderOptions(
        label=settings.gmail.label,
        failed_folder=settings.failed_folder,
        unverified_folder=settings.unverified_folder,
        inserted_keyword=settings.inserted_keyword,
        time_budget_seconds=settings.time_budget_seconds,
        rejection_threshold=settings.rejection_threshold,
    )
    return Forwarder(mailbox_factory, gmail_factory, options)
