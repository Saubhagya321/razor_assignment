"""Targeted test for client.py review item 4: httpx/httpcore must not leak
PII (emails in query strings) or full URLs into logs.

client.py sets both loggers to WARNING at import time specifically because
customer lookups put emails in the query string (?email=...), and httpx/
httpcore log the full request line (method + URL) at INFO by default.
"""

import logging

import httpx
import respx

from wc_connector.client import WooClient

from conftest import make_config


def test_httpx_and_httpcore_loggers_are_set_to_warning():
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


async def test_no_log_record_contains_email_or_full_url(caplog):
    """A real request carrying an email in the query string, with the root
    logger at INFO (as server.py configures it), must not emit any log
    record containing the email or the request URL."""
    config = make_config()
    client = WooClient(config, jitter_fn=lambda a, b: 0)
    email = "secret.customer@example.com"

    try:
        with caplog.at_level(logging.INFO):
            with respx.mock(base_url="https://example.test") as router:
                router.get("/wp-json/wc/v3/customers").mock(
                    return_value=httpx.Response(200, json=[])
                )
                await client.get("/wp-json/wc/v3/customers", params={"email": email})
    finally:
        await client.aclose()

    for record in caplog.records:
        message = record.getMessage()
        assert email not in message
        assert "/wp-json/wc/v3/customers" not in message
