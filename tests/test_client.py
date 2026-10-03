"""SPEC.md section 9, tests 6/7/8/16: 429 retry/backoff, 401 zero-retry, wait budget.

These are written against the client directly (no tool/sanitizer involved)
since they exercise retry/error-mapping behavior that lives in client.py.
"""

import time

import httpx
import pytest
import respx

from wc_connector import errors
from wc_connector.client import WooClient

from conftest import make_config


@pytest.fixture
async def client_factory():
    created = []

    def _make(**config_overrides):
        config = make_config(**config_overrides)
        client = WooClient(config, jitter_fn=lambda a, b: 0)
        created.append(client)
        return client

    yield _make
    for client in created:
        await client.aclose()


async def test_429_then_success_honors_retry_after(client_factory):
    """Spec test 6: 429 then success: retries, honors Retry-After."""
    client = client_factory(max_retries=3, max_total_wait_seconds=5)
    with respx.mock(base_url="https://example.test") as router:
        route = router.get("/wp-json/wc/v3/orders/1").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(200, json={"id": 1, "status": "processing"}),
            ]
        )
        result = await client.get("/wp-json/wc/v3/orders/1")
        assert result.data == {"id": 1, "status": "processing"}
        assert route.call_count == 2


async def test_429_forever_returns_rate_limited_no_partial_data(client_factory):
    """Spec test 7: 429 forever returns RATE_LIMITED, retryable: true, no partial data."""
    client = client_factory(max_retries=2, max_total_wait_seconds=5)
    with respx.mock(base_url="https://example.test") as router:
        router.get("/wp-json/wc/v3/orders/1").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "0"})
        )
        with pytest.raises(errors.ConnectorError) as exc_info:
            await client.get("/wp-json/wc/v3/orders/1")
        assert exc_info.value.code == "RATE_LIMITED"
        assert exc_info.value.retryable is True


async def test_401_zero_retries(client_factory):
    """Spec test 8: 401 returns AUTH_FAILED, retryable: false, zero retries."""
    client = client_factory(max_retries=3, max_total_wait_seconds=5)
    with respx.mock(base_url="https://example.test") as router:
        route = router.get("/wp-json/wc/v3/orders/1").mock(return_value=httpx.Response(401))
        with pytest.raises(errors.ConnectorError) as exc_info:
            await client.get("/wp-json/wc/v3/orders/1")
        assert exc_info.value.code == "AUTH_FAILED"
        assert exc_info.value.retryable is False
        assert route.call_count == 1


async def test_wait_budget_does_not_sleep_past_it(client_factory):
    """Spec test 16: Retry-After: 60 with a small budget returns RATE_LIMITED
    immediately (no 60-second sleep) with retry_after_seconds."""
    client = client_factory(max_retries=5, max_total_wait_seconds=1)
    with respx.mock(base_url="https://example.test") as router:
        route = router.get("/wp-json/wc/v3/orders/1").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "60"})
        )
        start = time.monotonic()
        with pytest.raises(errors.ConnectorError) as exc_info:
            await client.get("/wp-json/wc/v3/orders/1")
        elapsed = time.monotonic() - start

        assert exc_info.value.code == "RATE_LIMITED"
        assert exc_info.value.retry_after_seconds == 60.0
        assert elapsed < 0.5, f"should not have slept close to 60s, took {elapsed}s"
        assert route.call_count == 1
