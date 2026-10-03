"""HTTP client to the WooCommerce REST API: auth, throttling, retries, error mapping.

SPEC.md section 6.1. All tools are reads (GET), which is what makes blind
retries safe here; if writes are ever added, retries need idempotency keys.
"""

from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import random
import ssl
import time
from dataclasses import dataclass

import httpx

from . import errors
from .config import Config

# httpx/httpcore log HTTP request lines (method + full URL, including query
# strings) at INFO. Our customer lookups put emails in the query string
# (?email=...), so leaving these at INFO would leak PII into logs the moment
# the root logger is set to INFO (as server.py does). WARNING suppresses the
# per-request line entirely; only genuine warnings/errors get through.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 10.0

# SPEC.md section 6.1's retry policy table caps connection errors, timeouts,
# and 502/503/504 at 2 retries specifically -- a fixed, smaller number than
# WC_MAX_RETRIES, which governs 429 handling instead. A struggling store
# fails fast past this point rather than eating the whole time budget.
NETWORK_AND_5XX_MAX_RETRIES = 2


@dataclass(frozen=True)
class Response:
    data: dict | list
    headers: httpx.Headers


def has_next_page(headers: httpx.Headers) -> bool:
    link = headers.get("Link") or headers.get("link") or ""
    return 'rel="next"' in link


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    delta = parsed.timestamp() - time.time()
    return max(delta, 0.0)


def _looks_like_tls_verification_error(exc: BaseException) -> bool:
    return isinstance(exc, ssl.SSLCertVerificationError) or (
        isinstance(exc, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(exc)
    )


def _is_tls_verification_failure(exc: BaseException) -> bool:
    """Walks the full wrapping chain looking for the underlying SSL error.

    Confirmed against the real store that httpx/httpcore's wrapping isn't
    consistent: httpx.ConnectError chains via __cause__, but
    httpcore.ConnectError instead stores the ssl.SSLCertVerificationError as
    a plain constructor argument (`ConnectError(ssl_error)`), not as
    __cause__. So every node in the chain must be checked both directly and
    via its .args, not just exc.__cause__.
    """
    seen: set[int] = set()
    stack = [exc]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if _looks_like_tls_verification_error(node):
            return True
        if node.__cause__ is not None:
            stack.append(node.__cause__)
        for arg in getattr(node, "args", ()):
            if isinstance(arg, BaseException):
                stack.append(arg)
    return False


class WooClient:
    """Wraps one httpx.AsyncClient to the configured WooCommerce store."""

    def __init__(self, config: Config, jitter_fn=random.uniform):
        verify = config.ca_bundle if config.ca_bundle else True
        self._config = config
        self._jitter_fn = jitter_fn
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            auth=(config.consumer_key, config.consumer_secret),
            timeout=config.timeout_seconds,
            verify=verify,
        )
        self._semaphore = asyncio.Semaphore(config.max_concurrent_requests)
        self._min_interval = config.min_request_interval_ms / 1000.0
        self._last_request_at: float | None = None
        self._throttle_lock = asyncio.Lock()

    @property
    def config(self) -> Config:
        return self._config

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        async with self._throttle_lock:
            if self._last_request_at is not None:
                elapsed = time.monotonic() - self._last_request_at
                wait = self._min_interval - elapsed
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()

    def _backoff_delay(self, attempt: int) -> float:
        base = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_CAP_SECONDS)
        return base + self._jitter_fn(0, base * 0.25)

    async def _send_once(self, path: str, params: dict | None, remaining: float) -> httpx.Response:
        # Only the request itself holds a concurrency slot. Backoff sleeps
        # happen after this returns/raises, outside the semaphore: a request
        # that's backing off from a struggling upstream shouldn't also block
        # a sibling tool call from using that concurrency slot in the
        # meantime. WC_MAX_CONCURRENT_REQUESTS caps requests in flight, not
        # the number of calls waiting on a timer.
        async with self._semaphore:
            await self._throttle()
            return await self._client.get(path, params=params, timeout=min(self._config.timeout_seconds, max(remaining, 0.01)))

    async def get(self, path: str, params: dict | None = None, deadline: float | None = None) -> Response:
        """GETs path, retrying per SPEC.md's policy, honoring a shared deadline.

        `deadline` is an absolute `time.monotonic()` value for when the whole
        tool call (which may include other upstream calls) must give up. If
        None, a fresh deadline is derived from WC_MAX_TOTAL_WAIT_SECONDS.
        """
        if deadline is None:
            deadline = time.monotonic() + self._config.max_total_wait_seconds

        attempt = 0
        # Tracks what the last-seen failure was, so that if the budget runs
        # out between attempts (checked at the top of the loop) we raise the
        # error that matches what actually happened, not a generic one.
        last_failure: str | None = None
        last_retry_after: float | None = None

        while True:
            attempt += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if last_failure == "rate_limited":
                    raise errors.rate_limited(retry_after_seconds=last_retry_after)
                raise errors.upstream_unavailable()

            try:
                response = await self._send_once(path, params, remaining)
            except httpx.TransportError as e:
                if _is_tls_verification_failure(e):
                    raise errors.config_error(
                        "TLS certificate verification failed. Check WC_CA_BUNDLE."
                    )
                if attempt > NETWORK_AND_5XX_MAX_RETRIES:
                    raise errors.upstream_unavailable()
                last_failure = "upstream_unavailable"
                await self._sleep_for_retry(None, deadline, default_attempt=attempt)
                continue

            status = response.status_code

            if status == 200:
                try:
                    return Response(data=response.json(), headers=response.headers)
                except (json.JSONDecodeError, ValueError):
                    raise errors.upstream_unavailable()

            if status == 401:
                raise errors.auth_failed()
            if status == 403:
                raise errors.forbidden()
            if status == 404:
                raise errors.not_found()

            if status == 429:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                last_failure = "rate_limited"
                last_retry_after = retry_after
                if attempt > self._config.max_retries:
                    raise errors.rate_limited(retry_after_seconds=retry_after)
                if not self._sleep_fits_budget(retry_after, deadline):
                    raise errors.rate_limited(retry_after_seconds=retry_after)
                await self._sleep_for_retry(retry_after, deadline, default_attempt=attempt)
                continue

            if status == 500:
                last_failure = "upstream_unavailable"
                if attempt > 1:
                    raise errors.upstream_unavailable()
                await self._sleep_for_retry(None, deadline, default_attempt=attempt)
                continue

            if status in (502, 503, 504):
                last_failure = "upstream_unavailable"
                if attempt > NETWORK_AND_5XX_MAX_RETRIES:
                    raise errors.upstream_unavailable()
                retry_after = _parse_retry_after(response.headers.get("Retry-After")) if status == 503 else None
                if retry_after is not None and not self._sleep_fits_budget(retry_after, deadline):
                    raise errors.upstream_unavailable()
                await self._sleep_for_retry(retry_after, deadline, default_attempt=attempt)
                continue

            if 400 <= status < 500:
                # Any 4xx WooCommerce/WordPress can return that we don't
                # special-case above (405, 409, 422, ...): not retryable,
                # and not worth modeling as a distinct code for v1.
                raise errors.bad_request()

            # Unrecognized 5xx (501, 505, ...): no retry policy defined for
            # these in the spec, so fail without retrying rather than guess.
            raise errors.upstream_unavailable()

    async def get_json(self, path: str, params: dict | None = None, deadline: float | None = None) -> dict | list:
        """Convenience wrapper over get() for callers that don't need headers."""
        return (await self.get(path, params=params, deadline=deadline)).data

    def _sleep_fits_budget(self, retry_after: float | None, deadline: float) -> bool:
        if retry_after is None:
            return True
        return time.monotonic() + retry_after <= deadline

    async def _sleep_for_retry(self, retry_after: float | None, deadline: float, default_attempt: int = 1) -> None:
        delay = retry_after if retry_after is not None else self._backoff_delay(default_attempt)
        remaining = deadline - time.monotonic()
        delay = max(0.0, min(delay, remaining))
        if delay > 0:
            await asyncio.sleep(delay)
