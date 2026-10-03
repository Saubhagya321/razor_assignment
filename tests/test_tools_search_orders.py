"""Targeted test for client.py review item 6 (tools.py half): search_orders
must pass one shared deadline to both of its upstream calls (the customer
lookup and the orders listing), not a fresh deadline per call -- otherwise
the two calls' timeouts wouldn't be coordinated against one overall budget.
"""

from wc_connector import tools


class _FakeResponse:
    def __init__(self, data):
        self.data = data
        self.headers = {}


class _FakeClient:
    def __init__(self, customer_response, orders_response):
        self._customer_response = customer_response
        self._orders_response = orders_response
        self.deadlines_seen = []

        class _Config:
            max_total_wait_seconds = 5
            page_size = 20

        self.config = _Config()

    async def get(self, path, params=None, deadline=None):
        self.deadlines_seen.append(deadline)
        if path == "/wp-json/wc/v3/customers":
            return self._customer_response
        return self._orders_response


async def test_search_orders_by_email_passes_one_deadline_to_both_calls():
    client = _FakeClient(
        customer_response=_FakeResponse([{"id": 42}]),
        orders_response=_FakeResponse([]),
    )

    await tools.search_orders(client, customer_email="a@example.com")

    assert len(client.deadlines_seen) == 2
    assert client.deadlines_seen[0] is not None
    # Same deadline value shared across both upstream calls, not two
    # independently-derived ones.
    assert client.deadlines_seen[0] == client.deadlines_seen[1]
