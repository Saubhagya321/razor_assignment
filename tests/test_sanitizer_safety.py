"""SPEC.md section 9, tests 9/10/11: PII never leaks, injection strings in
dropped fields never leak (and the one field that legitimately passes
through -- line_item name -- is documented, not hidden), and secrets never
appear in logs or error output.

Uses mock/data.py's PII_INJECTION_ORDER and CUSTOMERS fixtures, which carry
a fake address, phone, IP, and injection strings in every free-text field
(SPEC.md section 8's requirement that the mock exercise these fields).
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx

from mock import data as mock_data
from wc_connector import tools
from wc_connector.client import WooClient

from conftest import make_config

PII_MARKERS = [
    mock_data.PII_INJECTION_ORDER["billing"]["address_1"],
    mock_data.PII_INJECTION_ORDER["billing"]["city"],
    mock_data.PII_INJECTION_ORDER["billing"]["phone"],
    mock_data.PII_INJECTION_ORDER["billing"]["email"],
    mock_data.PII_INJECTION_ORDER["customer_ip_address"],
    mock_data.PII_INJECTION_ORDER["customer_user_agent"],
    mock_data.PII_INJECTION_ORDER["payment_method"],
    mock_data.PII_INJECTION_ORDER["transaction_id"],
    mock_data.PII_INJECTION_ORDER["order_key"],
    mock_data.PII_INJECTION_ORDER["cart_hash"],
    mock_data.PII_INJECTION_ORDER["customer_note"],
]

INJECTION_MARKERS_THAT_MUST_NEVER_APPEAR = [
    mock_data.PII_INJECTION_ORDER["customer_note"],
    mock_data.PII_INJECTION_ORDER["billing"]["first_name"],
    mock_data.PII_INJECTION_ORDER["billing"]["last_name"],
    mock_data.PII_INJECTION_ORDER["meta_data"][0]["value"],
    mock_data.PII_INJECTION_ORDER["line_items"][0]["meta_data"][0]["value"],
]

# Documented exception: line_item.name is an allowlisted, kept field (capped
# at MAX_TEXT_LENGTH, not content-filtered), so injection text placed there
# is expected to pass through unmodified. This is the sanitizer's one
# acknowledged injection surface -- SPEC.md section 5 says to cap length,
# not to blocklist content.
LINE_ITEM_NAME_PASSES_THROUGH = mock_data.PII_INJECTION_ORDER["line_items"][0]["name"]


class _FakeClient:
    """Mimics WooClient's two call shapes (get_json for get_order,
    get -> Response(.data) for find_customer) without a real HTTP layer."""

    def __init__(self, order=None, customers=None):
        self._order = order
        self._customers = customers

        class _Config:
            page_size = 20

        self.config = _Config()

    async def get_json(self, path, params=None, deadline=None):
        return self._order

    async def get(self, path, params=None, deadline=None):
        class _Response:
            def __init__(self, data):
                self.data = data
                self.headers = {}

        return _Response(self._customers)


async def test_get_order_output_contains_no_pii():
    client = _FakeClient(order=mock_data.PII_INJECTION_ORDER)
    order = await tools.get_order(client, 52)
    serialized = order.model_dump_json()

    for marker in PII_MARKERS:
        assert marker not in serialized, f"PII leaked: {marker!r}"


async def test_list_orders_summary_contains_no_pii():
    response = tools.sanitize_order_summary(mock_data.PII_INJECTION_ORDER)
    serialized = response.model_dump_json()

    for marker in PII_MARKERS:
        assert marker not in serialized, f"PII leaked: {marker!r}"


async def test_find_customer_masks_email_and_drops_everything_else():
    client = _FakeClient(customers=[mock_data.CUSTOMERS[0]])
    result = await tools.find_customer(client, "injection.target@example.com")
    serialized = result.model_dump_json()

    raw_email = mock_data.CUSTOMERS[0]["email"]
    assert raw_email not in serialized
    assert result.items[0].masked_email == "i***@example.com"
    # Billing address/phone on the customer record must never appear.
    assert mock_data.CUSTOMERS[0]["billing"]["address_1"] not in serialized
    assert mock_data.CUSTOMERS[0]["billing"]["phone"] not in serialized


async def test_injection_strings_in_dropped_fields_never_appear():
    """Every free-text field that SPEC.md marks 'drop entirely' (customer
    notes, billing/shipping, internal meta_data) must never surface, even
    though it carries a prompt-injection payload in this fixture."""
    client = _FakeClient(order=mock_data.PII_INJECTION_ORDER)
    order = await tools.get_order(client, 52)
    serialized = order.model_dump_json()

    for marker in INJECTION_MARKERS_THAT_MUST_NEVER_APPEAR:
        assert marker not in serialized, f"Injection payload leaked via a dropped field: {marker!r}"


async def test_line_item_name_is_the_one_documented_pass_through_field():
    """Documents the sanitizer's one acknowledged injection surface: line
    item names are kept (and length-capped) but not content-filtered, per
    SPEC.md section 5 ('cap string lengths ... to limit injection surface',
    not a blocklist). This test fails loudly if that design changes without
    updating the documentation here."""
    client = _FakeClient(order=mock_data.PII_INJECTION_ORDER)
    order = await tools.get_order(client, 52)

    assert order.line_items[0].name == LINE_ITEM_NAME_PASSES_THROUGH


async def test_secret_never_appears_in_logs_or_error_output(caplog):
    """A real 401 (wrong key) round-trip: the client's Basic Auth header
    carries the consumer key/secret, but neither the resulting
    ConnectorError's message/str() nor any log record may contain them."""
    fake_key = "ck_super_secret_key_marker"
    fake_secret = "cs_super_secret_value_marker"
    config = make_config(consumer_key=fake_key, consumer_secret=fake_secret)
    client = WooClient(config, jitter_fn=lambda a, b: 0)

    try:
        with caplog.at_level(logging.DEBUG):
            with respx.mock(base_url="https://example.test") as router:
                router.get("/wp-json/wc/v3/orders/1").mock(return_value=httpx.Response(401))
                with pytest.raises(Exception) as exc_info:
                    await client.get("/wp-json/wc/v3/orders/1")
    finally:
        await client.aclose()

    error_str = str(exc_info.value)
    error_json = json.dumps(exc_info.value.to_dict())
    assert fake_key not in error_str
    assert fake_secret not in error_str
    assert fake_key not in error_json
    assert fake_secret not in error_json

    for record in caplog.records:
        message = record.getMessage()
        assert fake_key not in message
        assert fake_secret not in message
