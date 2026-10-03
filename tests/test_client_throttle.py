"""SPEC.md section 9 test 17: N concurrent tool calls never exceed
WC_MAX_CONCURRENT_REQUESTS upstream requests in flight, and calls respect the
minimum interval between requests.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import respx

from wc_connector.client import WooClient

from conftest import make_config


async def test_max_concurrent_requests_caps_upstream_inflight():
    client = WooClient(
        make_config(max_concurrent_requests=2, min_request_interval_ms=0, max_total_wait_seconds=5),
        jitter_fn=lambda a, b: 0,
    )
    in_flight = 0
    max_seen = 0

    async def responder(request):
        nonlocal in_flight, max_seen
        in_flight += 1
        max_seen = max(max_seen, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return httpx.Response(200, json={"id": 1})

    try:
        with respx.mock(base_url="https://example.test") as router:
            router.get("/wp-json/wc/v3/orders/1").mock(side_effect=responder)
            await asyncio.gather(*[client.get("/wp-json/wc/v3/orders/1") for _ in range(5)])
        assert max_seen <= 2, f"saw {max_seen} concurrent upstream requests, cap was 2"
    finally:
        await client.aclose()


async def test_min_request_interval_enforced_between_requests():
    client = WooClient(
        make_config(max_concurrent_requests=5, min_request_interval_ms=100, max_total_wait_seconds=5),
        jitter_fn=lambda a, b: 0,
    )
    timestamps: list[float] = []

    async def responder(request):
        timestamps.append(time.monotonic())
        return httpx.Response(200, json={"id": 1})

    try:
        with respx.mock(base_url="https://example.test") as router:
            router.get("/wp-json/wc/v3/orders/1").mock(side_effect=responder)
            await asyncio.gather(*[client.get("/wp-json/wc/v3/orders/1") for _ in range(3)])

        timestamps.sort()
        gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
        assert all(gap >= 0.09 for gap in gaps), f"gaps too small: {gaps}"
    finally:
        await client.aclose()
