"""Wiring: build a Forwarder from settings (shared by the server and the CLI)."""

from __future__ import annotations

import logging
import os

from .config import ForwarderSettings
from .forwarder import Forwarder, ForwarderOptions
from .gmail_client import GmailClient, build_credentials
from .imap_client import ICloudMailbox
from .store import build_store


def configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)


def build_forwarder(settings: ForwarderSettings | None = None) -> Forwarder:
    settings = settings or ForwarderSettings.from_env()
    credentials = build_credentials(
        settings.gmail.client_id, settings.gmail.client_secret, settings.gmail.refresh_token
    )
    store = build_store(
        settings.store_backend,
        collection=settings.firestore_collection,
        retention_days=settings.retention_days,
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
        max_attempts=settings.max_attempts,
        lease_seconds=settings.lease_seconds,
        time_budget_seconds=settings.time_budget_seconds,
    )
    return Forwarder(mailbox_factory, gmail_factory, store, options)
