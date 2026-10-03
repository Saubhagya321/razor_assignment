"""Explicit scenario test: mock always returns 429 with Retry-After: 2,
WC_MAX_RETRIES=3, a 20s budget. Asserts the request count, total sleep time,
and the exact error -- confirming WC_MAX_RETRIES (not the wait budget) is
what bounds retries here, since 3 retries * 2s Retry-After = 6s is well
inside the 20s budget.

Prediction made before running this test:
- 4 upstream requests (1 initial + 3 retries, the configured cap)
- ~6.0s of actual sleep (3 retries x Retry-After: 2s; the 4th 429 exceeds
  the retry cap and raises immediately, with no further sleep)
- ConnectorError(code=RATE_LIMITED, retryable=True, retry_after_seconds=2.0)
"""

import time

import httpx
import pytest
import respx

from wc_connector import errors
from wc_connector.client import WooClient

from conftest import make_config


async def test_429_always_with_retry_after_2_hits_max_retries_cap():
    client = WooClient(make_config(max_retries=3, max_total_wait_seconds=20), jitter_fn=lambda a, b: 0)
    try:
        with respx.mock(base_url="https://example.test") as router:
            route = router.get("/wp-json/wc/v3/orders/1").mock(
                return_value=httpx.Response(429, headers={"Retry-After": "2"})
            )
            start = time.monotonic()
            with pytest.raises(errors.ConnectorError) as exc_info:
                await client.get("/wp-json/wc/v3/orders/1")
            elapsed = time.monotonic() - start

        assert route.call_count == 4  # 1 initial + 3 retries (WC_MAX_RETRIES)
        assert 5.9 <= elapsed <= 6.5  # 3 sleeps of 2s each, well under the 20s budget

        assert exc_info.value.code == "RATE_LIMITED"
        assert exc_info.value.retryable is True
        assert exc_info.value.retry_after_seconds == 2.0
    finally:
        await client.aclose()
