"""Helpers for resolving configuration values from env vars or Secret Manager."""

from __future__ import annotations

import os


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def fetch_secret(resource: str) -> str:
    """Fetch a secret payload from Google Secret Manager.

    ``resource`` is ``projects/<p>/secrets/<name>`` or a full version path.
    When the version is omitted, ``latest`` is used.
    """
    from google.cloud import secretmanager  # imported lazily: not needed in tests

    if "/versions/" not in resource:
        resource = f"{resource.rstrip('/')}/versions/latest"
    client = secretmanager.SecretManagerServiceClient()
    response = client.access_secret_version(name=resource)
    return response.payload.data.decode("utf-8")


def resolve_secret_env(name: str, *, required: bool = True) -> str | None:
    """Return the value of env ``NAME``.

    If ``NAME`` is unset but ``NAME_SECRET`` names a Secret Manager resource,
    the secret is fetched instead.  Cloud Run injects secrets directly as env
    vars; the GCE watcher container fetches them itself using the VM's
    service account, so both layouts are supported here.
    """
    value = os.environ.get(name)
    if value:
        return value.strip()
    secret_ref = os.environ.get(f"{name}_SECRET")
    if secret_ref:
        return fetch_secret(secret_ref).strip()
    if required:
        raise ConfigError(f"Missing configuration: set {name} or {name}_SECRET")
    return None


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing configuration: {name}")
    return value


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
