"""Tool implementations. Each function returns a sanitized dict or raises ConnectorError.

server.py adapts these into MCP tool results (structuredContent on success,
isError: true with the ConnectorError's to_dict() on failure).
"""

from __future__ import annotations

import time

from . import errors
from .client import WooClient, has_next_page
from .sanitize import (
    CustomersResult,
    Order,
    OrdersPage,
    sanitize_customer,
    sanitize_order,
    sanitize_order_summary,
)

OPEN_STATUSES = ("pending", "processing", "on-hold")
VALID_STATUSES = {"open", "pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed"}


async def get_order(client: WooClient, order_id: int) -> Order:
    """Fetches one order by id and returns its sanitized view.

    Trashed orders (status "trash") are treated as NOT_FOUND, matching
    list_orders/search_orders, which never surface trashed orders either.
    """
    order = await client.get_json(f"/wp-json/wc/v3/orders/{order_id}")
    if not isinstance(order, dict) or order.get("status") == "trash":
        raise errors.not_found("order")
    return sanitize_order(order)


async def list_orders(client: WooClient, page: int = 1) -> OrdersPage:
    """Lists the most recently created orders, newest first.

    Trashed orders are never surfaced: the store excludes them unless the
    caller explicitly passes status=any or status=trash, which this tool
    never does.
    """
    page = max(page, 1)
    params = {
        "page": page,
        "per_page": client.config.page_size,
        "order": "desc",
        "orderby": "date",
    }
    response = await client.get("/wp-json/wc/v3/orders", params=params)
    orders = response.data if isinstance(response.data, list) else []
    return OrdersPage(
        items=[sanitize_order_summary(order) for order in orders],
        page=page,
        has_more=has_next_page(response.headers),
    )


async def find_customer(client: WooClient, email: str) -> CustomersResult:
    """Looks up customers by exact email. Confirmed: this is the store's only
    reliable lookup field; `search` on /customers is not used (see SPEC.md 4.5).
    """
    response = await client.get("/wp-json/wc/v3/customers", params={"email": email})
    customers = response.data if isinstance(response.data, list) else []
    if not customers:
        return CustomersResult(items=[], note="No customer found with that email.")
    return CustomersResult(items=[sanitize_customer(c) for c in customers])


async def _resolve_customer_id_by_email(client: WooClient, email: str, deadline: float) -> int | None:
    response = await client.get("/wp-json/wc/v3/customers", params={"email": email}, deadline=deadline)
    customers = response.data if isinstance(response.data, list) else []
    return customers[0]["id"] if customers else None


def _expand_status(status: str | None) -> list[str] | None:
    if status is None:
        return None
    if status not in VALID_STATUSES:
        raise errors.input_error(
            f"Invalid status '{status}'. Expected one of: {', '.join(sorted(VALID_STATUSES))}."
        )
    if status == "open":
        return list(OPEN_STATUSES)
    return [status]


async def search_orders(
    client: WooClient,
    status: str | None = None,
    customer_id: int | None = None,
    customer_email: str | None = None,
    page: int = 1,
) -> OrdersPage:
    """Finds orders by status and/or customer. See SPEC.md 4.2 for the rules
    this enforces: at least one filter, not both customer args, all-or-nothing
    on partial upstream failure, one shared deadline across both calls.
    """
    if status is None and customer_id is None and customer_email is None:
        raise errors.input_error("Provide at least one of status, customer_id, or customer_email.")
    if customer_id is not None and customer_email is not None:
        raise errors.input_error("Provide only one of customer_id or customer_email, not both.")

    page = max(page, 1)
    statuses = _expand_status(status)
    deadline = time.monotonic() + client.config.max_total_wait_seconds

    try:
        resolved_customer_id = customer_id
        if customer_email is not None:
            resolved_customer_id = await _resolve_customer_id_by_email(client, customer_email, deadline)
            if resolved_customer_id is None:
                return OrdersPage(items=[], page=page, has_more=False, note="No customer found with that email.")

        params = {
            "page": page,
            "per_page": client.config.page_size,
            "order": "desc",
            "orderby": "date",
        }
        if statuses is not None:
            params["status"] = ",".join(statuses)
        if resolved_customer_id is not None:
            params["customer"] = resolved_customer_id

        response = await client.get("/wp-json/wc/v3/orders", params=params, deadline=deadline)
    except errors.ConnectorError as e:
        raise errors.ConnectorError(
            code=e.code,
            message=f"Search is incomplete; no results are included. {e.message}",
            retryable=e.retryable,
            retry_after_seconds=e.retry_after_seconds,
        ) from e

    orders = response.data if isinstance(response.data, list) else []
    return OrdersPage(
        items=[sanitize_order_summary(order) for order in orders],
        page=page,
        has_more=has_next_page(response.headers),
    )
