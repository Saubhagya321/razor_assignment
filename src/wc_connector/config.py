"""Loads and validates connector configuration from environment variables.

Only reads from the environment (populated from .env by the process launcher,
e.g. python-dotenv in server.py's entrypoint). Never hardcodes or logs secrets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    base_url: str
    consumer_key: str
    consumer_secret: str
    ca_bundle: str | None
    page_size: int
    page_size_max: int
    max_retries: int
    timeout_seconds: float
    max_total_wait_seconds: float
    max_concurrent_requests: int
    min_request_interval_ms: int
    allow_insecure_localhost: bool


def _get_bool(env: dict, key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _get_int(env: dict, key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _get_float(env: dict, key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _is_localhost(base_url: str) -> bool:
    host = base_url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    return host in ("localhost", "127.0.0.1", "::1")


def load_config(env: dict | None = None) -> Config:
    """Builds a Config from the given mapping (defaults to os.environ)."""
    env = env if env is not None else os.environ

    base_url = env.get("WC_BASE_URL", "").strip().rstrip("/")
    consumer_key = env.get("WC_CONSUMER_KEY", "").strip()
    consumer_secret = env.get("WC_CONSUMER_SECRET", "").strip()

    missing = [
        name
        for name, value in (
            ("WC_BASE_URL", base_url),
            ("WC_CONSUMER_KEY", consumer_key),
            ("WC_CONSUMER_SECRET", consumer_secret),
        )
        if not value
    ]
    if missing:
        raise ConfigError(f"Missing required config: {', '.join(missing)}")

    allow_insecure_localhost = _get_bool(env, "WC_ALLOW_INSECURE_LOCALHOST", True)

    if base_url.startswith("http://") and not (allow_insecure_localhost and _is_localhost(base_url)):
        raise ConfigError(
            "WC_BASE_URL uses http:// but the host is not localhost. "
            "Refusing to send credentials over plaintext HTTP to a non-local host."
        )

    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ConfigError("WC_BASE_URL must start with http:// or https://")

    return Config(
        base_url=base_url,
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        ca_bundle=env.get("WC_CA_BUNDLE") or None,
        page_size=_get_int(env, "WC_PAGE_SIZE", 20),
        page_size_max=_get_int(env, "WC_PAGE_SIZE_MAX", 50),
        max_retries=_get_int(env, "WC_MAX_RETRIES", 3),
        timeout_seconds=_get_float(env, "WC_TIMEOUT_SECONDS", 5),
        max_total_wait_seconds=_get_float(env, "WC_MAX_TOTAL_WAIT_SECONDS", 20),
        max_concurrent_requests=_get_int(env, "WC_MAX_CONCURRENT_REQUESTS", 2),
        min_request_interval_ms=_get_int(env, "WC_MIN_REQUEST_INTERVAL_MS", 200),
        allow_insecure_localhost=allow_insecure_localhost,
    )
