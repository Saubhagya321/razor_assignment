"""Manual smoke test + demo script against the real store configured in .env.

Not part of the automated pytest suite: it makes real HTTP calls to a live
WooCommerce store, so it isn't run in CI and needs a seeded store (see
scripts/seed_store.py) to show non-empty results. It exercises the exact same
tools.py functions the MCP server calls, exactly as a host application would,
and prints each result so it can double as a live demo.

Never prints the consumer key or secret -- only load_config()'s parsed,
non-secret fields are ever logged, and the wrong-credentials demo overrides
the secret with a literal placeholder, never reads or prints the real one.

Usage:
    python scripts/smoke_real.py
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv  # noqa: E402

from wc_connector import errors, tools  # noqa: E402
from wc_connector.client import WooClient  # noqa: E402
from wc_connector.config import load_config  # noqa: E402


def _print_header(title: str) -> None:
    print(f"\n=== {title} ===")


def _print_model(label: str, model) -> None:
    print(f"{label}:")
    print(json.dumps(model.model_dump(), indent=2, default=str))


async def demo_list_orders(client: WooClient) -> int | None:
    _print_header("list_orders(page=1)")
    start = time.monotonic()
    page = await tools.list_orders(client, page=1)
    elapsed = time.monotonic() - start
    _print_model("Result", page)
    print(f"({len(page.items)} item(s), has_more={page.has_more}, {elapsed:.2f}s)")
    return page.items[0].id if page.items else None


async def demo_search_orders_open(client: WooClient) -> None:
    _print_header('search_orders(status="open")')
    page = await tools.search_orders(client, status="open")
    _print_model("Result", page)
    print(f"({len(page.items)} open order(s): pending/processing/on-hold expansion)")


async def demo_get_order(client: WooClient, order_id: int) -> None:
    _print_header(f"get_order(order_id={order_id})")
    order = await tools.get_order(client, order_id)
    _print_model("Result", order)


async def demo_get_order_not_found(client: WooClient) -> None:
    _print_header("get_order(order_id=999999999)  # expected: NOT_FOUND")
    try:
        await tools.get_order(client, 999999999)
        print("UNEXPECTED: no error raised")
    except errors.ConnectorError as e:
        print(f"ConnectorError as expected: {e}")


async def demo_find_customer_no_match(client: WooClient) -> None:
    _print_header('find_customer(email="definitely-not-seeded@example.com")')
    result = await tools.find_customer(client, "definitely-not-seeded@example.com")
    _print_model("Result", result)


async def demo_find_customer_likely_seeded(client: WooClient) -> None:
    """Tries the first email scripts/seed_store.py would have created.
    Prints whichever result comes back (match or the "not found" note) --
    this is a demo of the masking behavior, not an assertion."""
    email = "asha.sharma0@example.com"
    _print_header(f"find_customer(email={email!r})  # from scripts/seed_store.py, if seeded")
    result = await tools.find_customer(client, email)
    _print_model("Result", result)


async def demo_input_validation_errors() -> None:
    """No network calls: these fail before any upstream request is made."""
    _print_header("Input validation (no upstream call)")
    for label, kwargs in (
        ("search_orders() with no filters", {}),
        ("search_orders() with both customer_id and customer_email", {"customer_id": 1, "customer_email": "a@example.com"}),
    ):
        try:
            await tools.search_orders(None, **kwargs)  # type: ignore[arg-type]
            print(f"{label}: UNEXPECTED: no error raised")
        except errors.ConnectorError as e:
            print(f"{label}: {e}")


async def demo_wrong_credentials(config) -> None:
    """Proves AUTH_FAILED + zero retries against the real store, using a
    literal placeholder secret -- never the real one."""
    _print_header("get_order with a deliberately wrong consumer secret  # expected: AUTH_FAILED")
    bad_config = dataclasses.replace(config, consumer_secret="cs_deliberately_wrong_placeholder")
    bad_client = WooClient(bad_config)
    try:
        start = time.monotonic()
        try:
            await tools.get_order(bad_client, 1)
            print("UNEXPECTED: no error raised")
        except errors.ConnectorError as e:
            elapsed = time.monotonic() - start
            print(f"ConnectorError as expected ({elapsed:.2f}s, no retries): {e}")
    finally:
        await bad_client.aclose()


async def main() -> None:
    load_dotenv()
    config = load_config()
    print("Config loaded (non-secret fields only):")
    print(
        json.dumps(
            {
                "base_url": config.base_url,
                "page_size": config.page_size,
                "max_retries": config.max_retries,
                "timeout_seconds": config.timeout_seconds,
                "max_total_wait_seconds": config.max_total_wait_seconds,
                "max_concurrent_requests": config.max_concurrent_requests,
                "min_request_interval_ms": config.min_request_interval_ms,
            },
            indent=2,
        )
    )

    await demo_input_validation_errors()

    client = WooClient(config)
    try:
        first_order_id = await demo_list_orders(client)
        await demo_search_orders_open(client)
        if first_order_id is not None:
            await demo_get_order(client, first_order_id)
        else:
            print("\n(No orders found -- run scripts/seed_store.py first to see get_order's happy path.)")
        await demo_get_order_not_found(client)
        await demo_find_customer_no_match(client)
        await demo_find_customer_likely_seeded(client)
    finally:
        await client.aclose()

    await demo_wrong_credentials(config)

    print("\nSmoke run complete.")


if __name__ == "__main__":
    asyncio.run(main())
