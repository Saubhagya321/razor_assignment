"""SPEC.md section 9, tests 1-5 (minus what test_client.py/test_mock_server.py
already cover): list_orders ordering/page size, search_orders status
expansion, customer-filter validation (id/email/both/neither), email with no
match, and get_order's 404 mapping -- all exercised through tools.py against
a real WooClient with respx-mocked HTTP.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from wc_connector import errors, tools
from wc_connector.client import WooClient

from conftest import make_config


@pytest.fixture
async def client():
    c = WooClient(make_config(page_size=15), jitter_fn=lambda a, b: 0)
    yield c
    await c.aclose()


async def test_list_orders_requests_newest_first_with_configured_page_size(client):
    """Spec test 1."""
    with respx.mock(base_url="https://example.test") as router:
        route = router.get("/wp-json/wc/v3/orders").mock(return_value=httpx.Response(200, json=[]))
        await tools.list_orders(client, page=2)

    params = httpx.QueryParams(route.calls.last.request.url.query)
    assert params["order"] == "desc"
    assert params["orderby"] == "date"
    assert params["per_page"] == "15"
    assert params["page"] == "2"


async def test_search_orders_open_status_expands_to_pending_processing_on_hold(client):
    """Spec test 2."""
    with respx.mock(base_url="https://example.test") as router:
        route = router.get("/wp-json/wc/v3/orders").mock(return_value=httpx.Response(200, json=[]))
        await tools.search_orders(client, status="open")

    params = httpx.QueryParams(route.calls.last.request.url.query)
    assert params["status"] == "pending,processing,on-hold"


async def test_search_orders_by_customer_id_sends_customer_param(client):
    """Spec test 3 (by customer id)."""
    with respx.mock(base_url="https://example.test") as router:
        route = router.get("/wp-json/wc/v3/orders").mock(return_value=httpx.Response(200, json=[]))
        await tools.search_orders(client, customer_id=99)

    params = httpx.QueryParams(route.calls.last.request.url.query)
    assert params["customer"] == "99"


async def test_search_orders_by_email_resolves_customer_then_filters(client):
    """Spec test 3 (by email): resolves the customer id first, then filters orders by it."""
    with respx.mock(base_url="https://example.test") as router:
        router.get("/wp-json/wc/v3/customers").mock(return_value=httpx.Response(200, json=[{"id": 7}]))
        orders_route = router.get("/wp-json/wc/v3/orders").mock(return_value=httpx.Response(200, json=[]))
        await tools.search_orders(client, customer_email="a@example.com")

    params = httpx.QueryParams(orders_route.calls.last.request.url.query)
    assert params["customer"] == "7"


async def test_search_orders_rejects_both_customer_id_and_email(client):
    """Spec test 3 (by both -> error)."""
    with pytest.raises(errors.ConnectorError) as exc_info:
        await tools.search_orders(client, customer_id=1, customer_email="a@example.com")
    assert exc_info.value.code == "BAD_REQUEST"
    assert exc_info.value.retryable is False


async def test_search_orders_rejects_no_filters(client):
    """Spec test 3 (by neither -> error)."""
    with pytest.raises(errors.ConnectorError) as exc_info:
        await tools.search_orders(client)
    assert exc_info.value.code == "BAD_REQUEST"
    assert exc_info.value.retryable is False


async def test_search_orders_email_with_no_match_returns_empty_with_note(client):
    """Spec test 4."""
    with respx.mock(base_url="https://example.test") as router:
        router.get("/wp-json/wc/v3/customers").mock(return_value=httpx.Response(200, json=[]))
        result = await tools.search_orders(client, customer_email="nobody@example.com")

    assert result.items == []
    assert result.has_more is False
    assert result.note == "No customer found with that email."


async def test_list_orders_has_more_true_when_link_header_has_next_rel(client):
    """Spec test 13."""
    with respx.mock(base_url="https://example.test") as router:
        router.get("/wp-json/wc/v3/orders").mock(
            return_value=httpx.Response(
                200,
                json=[],
                headers={"Link": '<https://example.test/wp-json/wc/v3/orders?page=2>; rel="next"'},
            )
        )
        result = await tools.list_orders(client, page=1)

    assert result.has_more is True


async def test_list_orders_has_more_false_when_link_header_absent(client):
    """Spec test 13."""
    with respx.mock(base_url="https://example.test") as router:
        router.get("/wp-json/wc/v3/orders").mock(return_value=httpx.Response(200, json=[]))
        result = await tools.list_orders(client, page=1)

    assert result.has_more is False


async def test_get_order_404_raises_not_found(client):
    """Spec test 5 (404 path; happy path and the trashed-order case are
    already covered in test_mock_server.py)."""
    with respx.mock(base_url="https://example.test") as router:
        router.get("/wp-json/wc/v3/orders/404404").mock(return_value=httpx.Response(404))
        with pytest.raises(errors.ConnectorError) as exc_info:
            await tools.get_order(client, 404404)

    assert exc_info.value.code == "NOT_FOUND"
    assert exc_info.value.retryable is False
