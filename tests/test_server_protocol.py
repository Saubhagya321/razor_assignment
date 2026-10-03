"""SPEC.md section 9, tests 14/15/18/19: every failure path returns
isError: true and every success isError false/absent, input validation
failures are isError: true with an actionable message, tool annotations are
present on every tool, and successful results validate against each tool's
outputSchema.

Drives the real FastMCP server (server.mcp) over an in-memory MCP session
(mcp.shared.memory), with load_config patched to a test config and upstream
HTTP mocked via respx -- this is the real protocol layer, not a bypass of it,
so a missing annotation or a schema violation would fail here the same way it
would against a real MCP host.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import jsonschema
import respx
from mcp.shared.memory import create_connected_server_and_client_session

from mock import data as mock_data
from wc_connector import server

from conftest import make_config


def _error_payload(result) -> dict:
    """FastMCP prefixes the exception's str() with "Error executing tool
    <name>: ", so the ConnectorError JSON has to be carved out rather than
    parsed directly."""
    text = result.content[0].text
    return json.loads(text[text.index("{") :])


@asynccontextmanager
async def connected_session(monkeypatch):
    monkeypatch.setattr(server, "load_config", lambda: make_config())
    async with create_connected_server_and_client_session(server.mcp) as session:
        yield session


async def test_tool_annotations_present_on_every_tool(monkeypatch):
    """Spec test 18."""
    async with connected_session(monkeypatch) as session:
        result = await session.list_tools()

    assert {t.name for t in result.tools} == {"get_order", "list_orders", "search_orders", "find_customer"}
    for tool in result.tools:
        assert tool.annotations is not None, f"{tool.name} is missing annotations"
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.idempotentHint is True


async def test_successful_results_are_not_errors_and_match_output_schema(monkeypatch):
    """Spec test 19 (and the success half of test 14)."""
    async with connected_session(monkeypatch) as session:
        tools_result = await session.list_tools()
        schemas = {t.name: t.outputSchema for t in tools_result.tools}

        with respx.mock(base_url="https://example.test") as router:
            router.get("/wp-json/wc/v3/orders").mock(
                return_value=httpx.Response(200, json=[mock_data.GUEST_ORDER])
            )
            list_result = await session.call_tool("list_orders", {})

        with respx.mock(base_url="https://example.test") as router:
            router.get(f"/wp-json/wc/v3/orders/{mock_data.GUEST_ORDER['id']}").mock(
                return_value=httpx.Response(200, json=mock_data.GUEST_ORDER)
            )
            get_result = await session.call_tool("get_order", {"order_id": mock_data.GUEST_ORDER["id"]})

        with respx.mock(base_url="https://example.test") as router:
            router.get("/wp-json/wc/v3/customers").mock(return_value=httpx.Response(200, json=[]))
            find_result = await session.call_tool("find_customer", {"email": "nobody@example.com"})

    for name, result in (("list_orders", list_result), ("get_order", get_result), ("find_customer", find_result)):
        assert result.isError in (False, None), f"{name} unexpectedly errored: {result}"
        assert result.structuredContent is not None
        jsonschema.validate(instance=result.structuredContent, schema=schemas[name])


async def test_every_failure_path_sets_iserror_true(monkeypatch):
    """Spec test 14 (failure half) and 15: input validation failures are
    isError: true with an actionable message."""
    async with connected_session(monkeypatch) as session:
        # Our own manual validation (order_id <= 0).
        bad_order_id = await session.call_tool("get_order", {"order_id": -1})
        assert bad_order_id.isError is True
        payload = _error_payload(bad_order_id)
        assert payload["error"]["code"] == "BAD_REQUEST"
        assert "positive integer" in payload["error"]["message"]

        # Our own manual validation (page <= 0).
        bad_page = await session.call_tool("list_orders", {"page": 0})
        assert bad_page.isError is True
        payload = _error_payload(bad_page)
        assert "positive integer" in payload["error"]["message"]

        # Both customer_id and customer_email given.
        both = await session.call_tool(
            "search_orders", {"customer_id": 1, "customer_email": "a@example.com"}
        )
        assert both.isError is True
        payload = _error_payload(both)
        assert "only one of" in payload["error"]["message"]

        # Neither status nor customer_id nor customer_email given.
        neither = await session.call_tool("search_orders", {})
        assert neither.isError is True
        payload = _error_payload(neither)
        assert "at least one" in payload["error"]["message"]

        # Bad enum for status: rejected by the framework's own jsonschema
        # input validation (Literal -> enum in the generated inputSchema)
        # before our tool code even runs; still isError: true with an
        # actionable message.
        bad_enum = await session.call_tool("search_orders", {"status": "not-a-real-status"})
        assert bad_enum.isError is True
        assert "not-a-real-status" in bad_enum.content[0].text

        # Upstream failure path: a 404 from the store.
        with respx.mock(base_url="https://example.test") as router:
            router.get("/wp-json/wc/v3/orders/999999").mock(return_value=httpx.Response(404))
            not_found = await session.call_tool("get_order", {"order_id": 999999})
        assert not_found.isError is True
        payload = _error_payload(not_found)
        assert payload["error"]["code"] == "NOT_FOUND"
