"""Targeted test for client.py review item 6 (server.py half): app_lifespan
must construct exactly one WooClient per server lifespan, shared via
AppContext by all tool handlers -- not one per tool call.
"""

from wc_connector import server

from conftest import make_config


async def test_app_lifespan_creates_exactly_one_shared_woo_client(monkeypatch):
    monkeypatch.setattr(server, "load_config", lambda: make_config())

    construct_count = 0
    real_init = server.WooClient.__init__

    def counting_init(self, *args, **kwargs):
        nonlocal construct_count
        construct_count += 1
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(server.WooClient, "__init__", counting_init)

    async with server.app_lifespan(server.mcp) as app_ctx:
        assert construct_count == 1
        first = app_ctx.woo_client
        # Simulate a second, independent tool call within the same lifespan:
        # it must observe the identical client instance, not a new one.
        second = app_ctx.woo_client
        assert first is second
        assert construct_count == 1
