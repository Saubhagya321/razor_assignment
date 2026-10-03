# Capabilities: what the agent can and cannot do

## Can

- List recent orders, newest first (`list_orders`).
- Find orders by status and/or customer, including the derived `"open"` status (`search_orders`).
- Get one order's full (sanitized) details by id (`get_order`).
- Look up a customer by exact email (`find_customer`).
- Receive masked, minimal data: emails as `i***@example.com`, no addresses, phone numbers, payment details, or free-text notes.
- Get clear, structured retryable/non-retryable errors (`RATE_LIMITED`, `AUTH_FAILED`, `NOT_FOUND`, `BAD_REQUEST`, `UPSTREAM_UNAVAILABLE`, `CONFIG_ERROR`, `FORBIDDEN`) instead of raw HTTP failures.

## Cannot

- Create, update, or delete anything — there is no write path in the code; the API key itself is also Read-only.
- See billing/shipping addresses, payment method details, IP addresses, user agents, or free-text order/customer notes — these are dropped entirely by the sanitizer, never partially masked.
- Filter by date, amount, or product — not exposed as search parameters (only status and customer id/email).
- Reliably match a guest order (`customer_id: 0`) to a customer by email **[VERIFY against your store's actual guest-checkout behavior]**.
- Retrieve more than the capped number of results per call (`WC_PAGE_SIZE`, hard-capped at `WC_PAGE_SIZE_MAX`) — use `has_more` and `page` to paginate.
- Act on behalf of a specific end user: one shared API key is used for every caller, so there's no per-user scoping or audit trail at the connector layer.

## Defined by us

- `"open"` means `pending` + `processing` + `on-hold` (SPEC.md's definition, not a WooCommerce built-in).
- One kept field is intentionally **not** content-filtered: `line_items[].name` is allowlisted and length-capped (200 chars) but passed through as-is, since it's a product name rather than user-authored free text. See `tests/test_sanitizer_safety.py`'s `test_line_item_name_is_the_one_documented_pass_through_field` for the explicit test documenting this as a deliberate, acknowledged injection surface rather than an oversight.

## One-field injection surface, in detail

Everything else that could carry an attacker- or customer-controlled string (`customer_note`, billing/shipping names and addresses, internal `meta_data`) is dropped entirely, not sanitized-in-place — "drop," not "blocklist," because blocklisting text content is unreliable. `line_items[].name` is the sole exception, accepted because:

- It's attacker-reachable only if someone can create a product with an adversarial name in the merchant's own catalog — a much narrower threat than arbitrary customer-entered free text.
- Capping its length (not its content) limits how much an agent could be asked to "follow" from a single field.
- Hiding it isn't realistic either way: product names are core to what the tool is *for* ("what did customer X order?").

Any agent host consuming this connector's output should still treat all returned data as data, not instructions — the tool descriptions (see `docs/tool-spec.json`) say this explicitly.
