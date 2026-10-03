"""Output safety: allowlist + masking (SPEC.md section 5).

Builds new small objects from upstream records. Never deletes fields from
the raw one; fields not explicitly copied here are never returned.
"""

from __future__ import annotations

from pydantic import BaseModel

MAX_TEXT_LENGTH = 200


class LineItem(BaseModel):
    name: str
    quantity: int | None = None
    total: str | None = None


class Order(BaseModel):
    id: int
    status: str
    total: str | None = None
    currency: str | None = None
    date_created: str | None = None
    customer_id: int | None = None
    line_items: list[LineItem] = []


class OrderSummary(BaseModel):
    id: int
    status: str
    total: str | None = None
    currency: str | None = None
    date_created: str | None = None
    item_count: int = 0
    customer_id: int | None = None


class OrdersPage(BaseModel):
    items: list[OrderSummary] = []
    page: int
    has_more: bool
    note: str | None = None


class CustomerMatch(BaseModel):
    id: int
    first_name_initial: str | None = None
    masked_email: str | None = None


class CustomersResult(BaseModel):
    items: list[CustomerMatch] = []
    note: str | None = None


def _cap(text: str, limit: int = MAX_TEXT_LENGTH) -> str:
    return text if len(text) <= limit else text[:limit]


def mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    local, _, domain = email.partition("@")
    if not local:
        return f"***@{domain}"
    return f"{local[0]}***@{domain}"


def mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = "".join(c for c in phone if c.isdigit())
    if not digits:
        return None
    return digits[-4:]


def sanitize_line_item(item: dict) -> LineItem:
    return LineItem(
        name=_cap(str(item.get("name", ""))),
        quantity=item.get("quantity"),
        total=item.get("total"),
    )


def sanitize_order(order: dict) -> Order:
    """Allowlisted view of one order, for get_order."""
    return Order(
        id=order.get("id"),
        status=order.get("status"),
        total=order.get("total"),
        currency=order.get("currency"),
        date_created=order.get("date_created"),
        customer_id=order.get("customer_id"),
        line_items=[sanitize_line_item(item) for item in order.get("line_items", [])],
    )


def sanitize_order_summary(order: dict) -> OrderSummary:
    """Allowlisted view of one order for list_orders/search_orders results."""
    return OrderSummary(
        id=order.get("id"),
        status=order.get("status"),
        total=order.get("total"),
        currency=order.get("currency"),
        date_created=order.get("date_created"),
        item_count=len(order.get("line_items", [])),
        customer_id=order.get("customer_id"),
    )


def sanitize_customer(customer: dict) -> CustomerMatch:
    """Allowlisted view of one customer, for find_customer."""
    first_name = str(customer.get("first_name") or "")
    return CustomerMatch(
        id=customer.get("id"),
        first_name_initial=first_name[:1] or None,
        masked_email=mask_email(customer.get("email")),
    )
