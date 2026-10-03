# WooCommerce MCP Connector: Build Spec

A read-only MCP server that lets an agent (Razorpay Agent Studio or any MCP host) read orders and customers from a WooCommerce store.

Running example used throughout: **"find open orders for customer X."**

Items marked **[VERIFY]** are not confirmed. Check them against the official docs (developer.woocommerce.com) or a saved real response before relying on them.

---

## 1. Decisions already made

| Topic | Decision |
|---|---|
| Merchant tool | WooCommerce (REST API `wc/v3`) |
| Access | **Read-only** (the API key itself has Read permission only) |
| MCP transport | **stdio** (local). Remote HTTP + MCP OAuth 2.1 is documented as the long-term fix |
| Upstream auth (server -> WooCommerce) | HTTP Basic Auth with consumer key/secret, **over HTTPS** |
| Primary backend | **Real WooCommerce store** (local Docker, see INSTALL.md). The connector is built and demonstrated against it |
| Test and convenience backend | **Mock server**, built from real fixtures. Used for failure modes the real store will not produce on demand (401/429/5xx) and as a zero-setup option for reviewers. Selected only by config (base URL) |
| Language/stack | Python, official MCP Python SDK (`FastMCP`), `httpx`, `pydantic`, `pytest`, `respx`; mock server in FastAPI |
| Safety model | Allowlist output, mask what is kept, drop free text, cap volume |

Two auth hops, kept separate:
1. **Agent host -> MCP server:** stdio pipes on one machine. No TLS and no login; the OS is the boundary.
2. **MCP server -> WooCommerce:** HTTPS + Basic Auth. Key comes from environment variables only.

---

## 2. Architecture

```
Agent host -> MCP client -> [ MCP server (this repo) ] -> WooCommerce REST API
                                |- tools (list/search/get/find)
                                |- sanitizer (allowlist + masking)
                                |- HTTP client (auth, retry, 429 handling)
                                `- config (base URL, key, secret from env)
```

**One codebase, two backends.** The mock-vs-real choice lives **only in the base URL** the HTTP client uses. There must be no `if mock:` branch in tool code. Otherwise the tests would exercise different code than production runs.

**stdio rule (confirmed):** with the stdio transport, stdout carries only MCP protocol messages. Never use `print()` or any logger that writes to stdout. All logs go to **stderr**. A stray stdout write can corrupt the protocol stream and break the host connection. Proven end-to-end against a real subprocess by `tests/test_stdio_protocol.py`.

---

## 3. Configuration (`.env.example`, placeholders only)

```
WC_BASE_URL=http://localhost:8080        # mock default; real: https://your-store.example
WC_CONSUMER_KEY=ck_replace_me
WC_CONSUMER_SECRET=cs_replace_me
WC_CA_BUNDLE=                            # optional: path to a CA certificate to trust (local store behind a self-signed proxy, see INSTALL.md)
WC_PAGE_SIZE=20                          # default page size
WC_PAGE_SIZE_MAX=50                      # hard cap
WC_MAX_RETRIES=3
WC_TIMEOUT_SECONDS=5                     # per-request timeout (must be well below the total budget)
WC_MAX_TOTAL_WAIT_SECONDS=20             # TOTAL elapsed time one tool call may take, including request time and retry sleeps
WC_MAX_CONCURRENT_REQUESTS=2             # client-side concurrency cap to the store
WC_MIN_REQUEST_INTERVAL_MS=200           # client-side spacing between upstream calls
WC_ALLOW_INSECURE_LOCALHOST=true         # allows http:// ONLY for localhost/127.0.0.1
```

Rules:
- Refuse to start if the base URL is `http://` and the host is not localhost, unless explicitly overridden.
- `.env` is in `.gitignore`. Only `.env.example` is committed.
- Secrets are never logged, never returned in errors, never put in URLs.

---

## 4. Tools

All tools are read-only. Descriptions are written for the model: purpose, **when to use this vs. the sibling tool**, what is returned, and one example call.

**Annotations on every tool (confirmed against the installed `mcp` SDK, see `tests/test_server_protocol.py::test_tool_annotations_present_on_every_tool`):**
`readOnlyHint: true`, `destructiveHint: false`, `idempotentHint: true`, `openWorldHint: true` (it talks to an external system), plus a human-readable `title`.
These are **hints only**. The MCP spec says clients should treat annotations as untrusted unless the server is trusted, so they do not replace enforcement. The real enforcement is the Read-only API key.

**Structured output:** declare an `outputSchema` per tool and return results as `structuredContent` (an object, never a bare list), with the same JSON also serialized in a text content block for older clients. Pin the SDK version (`mcp[cli]<2`, see `requirements.txt`). **Confirmed**: FastMCP auto-generates `outputSchema` from the tool's return type annotation, and `structuredContent` is validated against it on every successful call (`tests/test_server_protocol.py::test_successful_results_are_not_errors_and_match_output_schema`; drift from `docs/tool-spec.json` is caught by `tests/test_tool_spec_matches_server.py`). Error results do not carry `structuredContent` (see section 6).

### 4.1 `list_orders`
**Purpose:** Browse the most recent orders with no conditions.

**Args**
- `page` (int, default 1, min 1)

**Behavior:** Orders sorted newest first by creation date. Page size is `WC_PAGE_SIZE`.

**Description (draft):**
> Lists the most recently created orders, newest first. Use this to browse recent activity when the user gives no condition. To find orders for a specific customer or status, use `search_orders` instead. Returns up to 20 orders per page as summaries (id, status, total, currency, created date, item count, customer id) plus `has_more`. Use `get_order` for one order's details. Example: `list_orders(page=2)`.

### 4.2 `search_orders`
**Purpose:** Find orders matching conditions.

**Args**
- `status` (enum, optional): `open`, `pending`, `processing`, `on-hold`, `completed`, `cancelled`, `refunded`, `failed`
  - **`open` is an alias** the server expands to `pending` + `processing` + `on-hold`. This is a business definition and must be stated in the description and the capabilities doc. Status slugs are confirmed in the official orders reference (which also lists `trash` and `any`; neither is exposed).
- `customer_id` (int, optional)
- `customer_email` (str, optional). Server resolves it to a customer id via the customers endpoint (one extra upstream API call that counts against the rate limit), then filters by customer id.
- `page` (int, default 1)

**Rules**
- At least one of `status`, `customer_id`, `customer_email` is required (otherwise the model should use `list_orders`).
- Supplying both `customer_id` and `customer_email` is an input error.
- If an email matches no customer, return an empty result with a clear message (not an error).
- **All-or-nothing:** `search_orders` may make up to two upstream calls (customer lookup by email, then the orders call). If any call fails after its retries, return one `isError: true` result and **discard** the results of the calls that succeeded. Never return a partial result as if it were complete. The error message says the search is incomplete and no results are included, without PII. Result size is a separate concern, handled by the page-size cap and `has_more`.
- **One shared deadline:** the total time budget (`WC_MAX_TOTAL_WAIT_SECONDS`) starts when the tool call starts and is shared by every upstream call inside it. It is not reset per request.
- **Guest checkout (confirmed):** the orders reference says `customer_id` is `0` for guests, so filtering by customer misses guest orders. State this limitation in the tool description and the doc.
- **Orders filters (confirmed in the reference):** `status` is an **array**, so "open" is **one** upstream call with `pending, processing, on-hold`; `customer` takes an integer customer id. There is no email filter on orders. **Encoding confirmed empirically** (see section 4.5): both `status=a,b` and `status[]=a&status[]=b` are accepted and return identical results; the mock accepts both forms.

**Description (draft):**
> Finds orders by condition: status and/or customer. Use this when the request includes a condition ("open orders for customer X"). For browsing recent orders with no condition, use `list_orders`. "open" means pending, processing, or on-hold. Customers can be given by id or by email; guest orders may not be matched to a customer. Returns up to 20 order summaries per page plus `has_more`. Example: `search_orders(status="open", customer_email="a@example.com")`.

### 4.3 `get_order`
**Args:** `order_id` (int)

**Returns:** One order with line items (name, quantity, line total), status, totals, dates, customer id, and masked contact info if kept by policy. Same allowlist as the summaries, plus line items.

**Trashed orders (confirmed):** moving an order to the WordPress trash sets its `status` field to `"trash"` (verified via a fresh GET after DELETE, not the DELETE response itself, which can show the pre-trash status). `list_orders`/`search_orders` never surface trashed orders (not even `status="any"`), so `get_order` must be consistent: if the upstream record's `status` is `"trash"`, treat it as `NOT_FOUND`, not as a normal order. Add a test for this (get a trashed order id, expect `NOT_FOUND`).

### 4.4 `find_customer`
**Args:** `email` (str)

**Returns:** Matching customers as `{id, first_name_initial, masked_email, order_count?}` (only fields on the allowlist). Used when the user supplies an email and the model needs an id, or to answer "who is this customer?".

### 4.5 API facts confirmed from the official orders and customers references

**Endpoints used (all GET, Basic Auth with consumer key and secret):**
- `GET /wp-json/wc/v3/orders` (list), `GET /wp-json/wc/v3/orders/<id>` (one)
- `GET /wp-json/wc/v3/customers` (list/lookup)

**Orders list parameters we use:** `page`, `per_page` (API default is 10; we set it explicitly), `status` (array), `customer` (integer id), `order` and `orderby` (defaults are `desc` and `date`; set them explicitly so "newest first" never depends on defaults).
**Not used:** `search` on orders. The reference only says "matching a string", not which fields. See the experiment in the notes below.

**Customers lookup:** use the `email` parameter (documented as "specific email"), not `search`. The `role` filter defaults to `customer`, so users with other roles are not returned; state this in the doc. The customer object contains billing/shipping addresses, username, and avatar URL, so `find_customer` must go through the allowlist and return only the id, a first-name initial, and a masked email.

**Orders are returned with `line_items`** (name, quantity, sku, totals). Keep name and quantity (and totals if useful); drop line-item `meta_data`.

**Write endpoints exist and must never be exposed:** create, update, delete, batch, order notes (create/delete), and order actions such as `send_order_details` and `send_email`, which **email customers**. Enforce this twice: the connector has no write tools, and the API key has Read permission only.

**Confirmed empirically against the local store (see `scripts/probe_api.py`, `scripts/probe_api_extra.py`, fixtures in `mock/fixtures/`):**
- **Status encoding:** both `status=a,b,c` (comma-joined) and `status[]=a&status[]=b&status[]=c` (repeated array param) are accepted and return identical results. The mock must accept both forms.
- **`per_page` is capped at 100.** Requesting more returns `HTTP 400` with a `rest_invalid_param` error (see `mock/fixtures/error_invalid_status.json`-style shape); the mock must enforce the same cap and error.
- **No total-count headers.** `X-WP-Total` / `X-WP-TotalPages` are **absent** on the orders endpoint. Pagination must rely solely on the `Link` header's `rel="next"`.
- **`status=any` excludes trashed orders.** An explicit `status=trash` filter does return them. Trashing an order is a destructive operation from the connector's point of view: once trashed, an order is invisible to every list/search/get path unless the caller explicitly filters `status=trash` (which this connector's tools never do).
- **Trashed order status (confirmed, corrects an earlier wrong read):** a fresh `GET /orders/<id>` after trashing shows `"status": "trash"`. (The DELETE response itself can still show the pre-trash status due to response timing — don't rely on it.) WordPress separately preserves the prior status internally via `_wp_trash_meta_status` in `meta_data`, but that's never exposed to list/search and is irrelevant to the connector.
- **Orders `search` matches both billing email and billing first/last name** (broader than the original guess of "some unspecified string match"). Still out of scope for v1's tool surface (no free-text search tool), but documented here so a future `[VERIFY]` doesn't reopen this.
- **Guest orders (`customer_id: 0`) cannot be matched to a customer via `customers?email=`.** This is a real, by-design gap; state it in the tool description for `search_orders`. An exact-match guest-order fallback (matching `billing.email` on the order list directly) is documented as a stretch goal only, out of scope for v1.
- **Pagination edges:** the last page with data has no `rel="next"` in its `Link` header. A page requested past the last page returns `HTTP 200` with an empty list `[]`, not an error.
- **Empty-result lookups are not errors:** `customers?email=<unknown>` and `orders?customer=<unknown id>` both return `HTTP 200` with an empty list. "No match" is representable without a special error path; tools should treat this as the normal empty case, not `NOT_FOUND`.
- **Error bodies (fixtures saved for the mock to reproduce exactly):** unknown order id (`error_unknown_order_id.json`), invalid `status` value (`error_invalid_status.json`), wrong/invalid API key (`error_wrong_key_401.json`), and a write attempt (`POST /orders`) using the Read-only key (`error_write_with_read_key.json`). **Confirmed: WooCommerce returns `401` both for a wrong/invalid key and for a valid key that lacks permission to perform the requested action** (e.g. the Read-key's write attempt also came back `401`, not `403`). There is no reliable way to distinguish "bad credentials" from "credentials valid but insufficient" from the status code alone — see the error-model wording below.

**Mock must reproduce:** status as an array (both comma-joined and repeated-param encodings), `customer` filter, `customers?email=` exact match, `customer_id: 0` guest orders in the fixtures, `per_page` cap of 100 with its `400` error, absence of total-count headers, trashed-order behavior (`status="trash"`, excluded from `any`), pagination edge cases (last page, past-the-end), and the saved error fixtures reproduced byte-for-byte (status code and JSON body shape).

### Pagination contract (all list-like tools)
Return `{ items: [...], page, has_more, note? }`. Derive `has_more` from the response's `Link: rel="next"` header (confirmed in docs; confirmed empirically that the last page omits it). Total-count headers (`X-WP-Total`/`X-WP-TotalPages`) are confirmed **absent** on this store — do not depend on them. If results exceed what can be returned, `note` tells the model to narrow the query.

---

## 5. Output safety: sanitizer

**Principle: allowlist, never blocklist.** Build a new small object from each upstream record; never delete fields from the raw one.

| Treatment | Fields |
|---|---|
| **Return** | order id, status, total, currency, created date, line items (name, quantity), customer id |
| **Mask** | email -> `a***@domain.com`; phone -> last 4 digits only. Only if useful to the agent |
| **Never return** | `billing`/`shipping` objects, `customer_ip_address`, `customer_user_agent`, `payment_method*`, `transaction_id`, `order_key`, `cart_hash`, `meta_data` (order and line-item level), coupon/tax/fee/shipping lines. Confirmed: `meta_data` can carry internal WordPress bookkeeping keys (e.g. `_wp_trash_meta_status`, `_wp_trash_meta_time`) that have nothing to do with the order's business data — another reason `meta_data` is a blanket never-return, not something to selectively filter |
| **Drop entirely** | free-text fields (customer notes, order notes). Do not try to regex-mask them |

Additional rules:
- Cap string lengths on any outsider-written text that is kept (product names, customer names) to limit injection surface.
- Treat all store data as **data, not instructions**. Say so in tool descriptions, but remember this is weak; not returning the field is the strong defense.
- Logs contain order/customer **ids**, never emails or phone numbers.
- **Volume cap:** page size is capped by `WC_PAGE_SIZE_MAX`.
- Real field names confirmed from saved live responses (`mock/fixtures/*.json`); the allowlist above matches the actual order/customer shapes returned by this store.

---

## 6. Error model

Tools never raise raw exceptions or upstream bodies to the model. Every failure is returned as a normal tool result with **`isError: true`** (MCP "tool execution error"). The spec says clients should pass these to the model so it can self-correct, whereas JSON-RPC protocol errors are for malformed requests and unknown tools. Input-validation failures (bad enum, missing required arg) are also `isError: true` results with an actionable message. The text content of the result is this JSON (error results carry no `structuredContent`):

```json
{ "error": { "code": "RATE_LIMITED", "message": "...", "retryable": true, "retry_after_seconds": 30 } }
```

| Upstream | Code | retryable | Behavior |
|---|---|---|---|
| 401 | `AUTH_FAILED` | **false** | No retry. **Confirmed:** WooCommerce returns 401 both for a wrong/invalid key and for a valid key lacking permission for the attempted action (a Read-only key's write attempt also comes back 401, not 403); the two cases can't be told apart from the status code alone. Message: "credentials missing, invalid, or lacking permission"; tell the user. Never echo headers or keys |
| 403 | `FORBIDDEN` | false | Key is valid but lacks permission |
| 404 | `NOT_FOUND` | false | e.g. unknown order id |
| 400 | `BAD_REQUEST` | false | Safe summary only, not the raw body |
| 429 | `RATE_LIMITED` | true | Retry internally (the model is not involved) up to `WC_MAX_RETRIES` with exponential backoff + jitter, **honoring `Retry-After`** if present, within the `WC_MAX_TOTAL_WAIT_SECONDS` budget. If `Retry-After` exceeds the remaining budget, do **not** sleep: return the error immediately with `retry_after_seconds`. If retries are exhausted, return the error (never partial data presented as success) |
| 5xx / timeout | `UPSTREAM_UNAVAILABLE` | true | Limited retry, then structured error |

Never include in any error: keys, secrets, Authorization headers, URLs with credentials, stack traces, raw upstream bodies.

### 6.1 Rate limiting: what the connector implements

Rate-limit **enforcement** belongs to the store (or whatever sits in front of it: hosting, CDN/WAF, a plugin). **Handling** it is the connector's job, in three layers:

1. **Proactive throttle (client side):** cap concurrent upstream requests (`WC_MAX_CONCURRENT_REQUESTS`) and space calls (`WC_MIN_REQUEST_INTERVAL_MS`) so an agent loop cannot hammer the merchant's store. This applies even when the store never sends a 429.
2. **Reactive retry (inside the HTTP client):** on 429 and transient 5xx/timeouts, retry with backoff + jitter, honoring `Retry-After`, within the total wait budget. The model never sees these retries.
3. **Telling the model (last resort):** only when retries are exhausted or the wait would exceed the budget, return the structured `RATE_LIMITED` error so the model can tell the user or try later.

**Retry policy**

| Situation | Retry? | Policy |
|---|---|---|
| 429 | yes | honor `Retry-After` (seconds or HTTP date; fall back to backoff if missing/unparseable), within the budget |
| 502 / 503 / 504, connection error, timeout | yes | up to 2 retries with backoff + jitter; honor `Retry-After` on 503 |
| 500 | once | a single retry, then return `UPSTREAM_UNAVAILABLE` (retryable: true); 500 is often a real bug, not a blip |
| 400 / 401 / 403 / 404 | no | return the structured error immediately |

- Backoff: `min(BASE * 2 ** (attempt - 1), CAP)` with BASE=1s, plus random jitter (fixed to a constant in tests). Growth gives a struggling store more room to recover each time.
- **The budget (`WC_MAX_TOTAL_WAIT_SECONDS`) is total elapsed time for the tool call**, including time spent waiting on responses, not only sleeps. Check it before every sleep and every retry.
- Retries are safe **only because every tool is a read (GET)**. If writes were ever added, retries would need idempotency keys. State this in the README.
- When the outcome is unknown and the store may be down for a while, do not wait long: return a retryable error and let the model or user retry later.

Also count the hidden extra upstream call in `search_orders` when `customer_email` is used. Optional: a short-TTL in-memory cache for `get_order` and `find_customer` to cut repeat calls.

**Honesty note for the README:** a default WooCommerce install may never return 429 (as far as I know it has no built-in limit **[VERIFY]**), so layers 2 and 3 are exercised against the mock. Layer 1 works on both.

---

## 7. Repo layout

```
.
|- README.md                 # setup, run, assumptions, limitations
|- docs/
|  |- capabilities.md        # what the agent can and cannot do
|  |- tool-spec.json         # generated from tools/list (see below)
|  `- limitations.md         # limitations + long-term fix (may live in README)
|- src/wc_connector/
|  |- server.py              # FastMCP app, tool registration
|  |- tools.py               # list/search/get/find
|  |- client.py              # httpx client, auth, retry, error mapping
|  |- sanitize.py            # allowlist + masking
|  |- config.py              # env loading + validation
|  `- errors.py
|- mock/
|  |- app.py                 # FastAPI mock WooCommerce
|  `- fixtures/              # built from real responses (fake data)
|- scripts/
|  |- dump_tool_spec.py      # writes docs/tool-spec.json from the live server
|  |- seed_store.py          # seeds a real store (optional path)
|  `- demo.py                # scripted demo against the mock
|- docker-compose.yml        # real-store path (WordPress + MySQL + WooCommerce)
|- tests/
|- .env.example
`- .gitignore
```

---

## 8. Mock server requirements

Reproduce **only what the connector depends on**:
- Endpoints used: orders list, single order, customers lookup
- Response fields the sanitizer reads, **plus PII and injection-test fields** (fake address, phone, IP, notes containing an injection string)
- `Link` pagination header
- Error shapes for 401 and 429 (with `Retry-After`)
- A way to force failures on demand (for example a header `X-Mock-Fail: 429|401|500`, or a switch via env). The failing mode must also support "fail N times, then succeed" to test retries

**Drift guard:** run the connector once against a real local store, save a few real responses (fake data only) as fixtures, and build the mock from them. Do not write the mock from memory. **The mock must reproduce the saved error fixtures exactly** (`mock/fixtures/error_*.json`): same HTTP status code and same JSON body shape as the real store returned for unknown order id, invalid `status` value, wrong/invalid key (401), and a write attempt with the Read-only key (401). Tests assert against these exact fixtures, not an approximation of what WooCommerce "probably" returns.

---

## 9. Tests (run against the mock; no secrets needed)

1. `list_orders` returns newest first and respects page size
2. `search_orders(status="open")` expands to pending + processing + on-hold
3. `search_orders` by customer id, by email, by both (error), by neither (error)
4. Email with no match returns empty with a message
5. `get_order` happy path and 404; **trashed order:** `get_order` on an order whose upstream `status` is `"trash"` returns `NOT_FOUND`, not the order data
6. **429 then success:** retries, honors `Retry-After`
7. **429 forever:** returns `RATE_LIMITED`, `retryable: true`, no partial data
8. **401:** returns `AUTH_FAILED`, `retryable: false`, **zero retries**
9. **PII test:** fixture contains fake address, phone, IP, notes; assert none appear in any tool output, and emails/phones are masked
10. **Injection test:** injection strings in every text field of a fixture; assert the dropped free-text field never appears; list which kept fields still pass through
11. **Secret test:** assert key/secret never appear in logs or error output
12. Config: refuses `http://` for non-localhost hosts
13. Pagination: `has_more` true/false from the `Link` header
14. Every failure path returns `isError: true`; every success returns `isError` false/absent
15. Input validation failures (bad enum, both customer args, no filters) return `isError: true` with an actionable message
16. **Wait budget:** mock sends `Retry-After: 60`; the tool returns `RATE_LIMITED` immediately (no 60-second sleep) with `retry_after_seconds`
17. **Throttle:** N concurrent tool calls never exceed `WC_MAX_CONCURRENT_REQUESTS` upstream requests in flight, and calls respect the minimum interval
18. Tool annotations present on every tool (`readOnlyHint: true` etc.) in `tools/list`
19. Successful results validate against each tool's `outputSchema`
20. Nothing is written to stdout except protocol messages (logs go to stderr)

---

## 10. MCP tool specification deliverable

`scripts/dump_tool_spec.py` starts the server, performs `tools/list`, and writes the result to `docs/tool-spec.json`. This is the "MCP tool specification" for the submission (names, descriptions, JSON-schema inputs). The README links to it.

**Note on Agent Studio:** no public spec for third-party connectors was found. Target standard MCP and state in the README that an adapter may be needed.

**MCP features deliberately not used (state this in the README as design decisions):**
- **Resources / prompts:** tools only. Tools are model-controlled, which fits "agent decides to look up orders". Resources (application-controlled) could expose `order/{id}` later.
- **Sampling** (server asks the client's model to generate text): not needed; the connector is a plain data reader.
- **Roots** (file-system access grants): not needed; no file access.
- **Notifications / progress:** optional nicety (for example, "rate limited, retrying") but not required. If added, send them over the logging channel, never stdout.
- **Streamable HTTP:** out of scope for v1. It is the long-term transport for a remote, multi-client deployment, and it brings session handling, scaling, and the OAuth 2.1 flow.

---

## 11. Real-store path (primary; must be reproducible from the repo)

- `docker-compose.yml`: WordPress + MySQL; install the WooCommerce plugin
- Enable pretty permalinks so `/wp-json/` routes work (confirmed — see INSTALL.md section 3)
- Create a **Read-only** REST API key (WooCommerce -> Settings -> Advanced -> REST API)
- `scripts/seed_store.py`: ~10 fake customers and ~20 orders across statuses (fake data only)
- HTTPS: Basic Auth on a real store needs HTTPS. Options: local reverse proxy with a local cert, or a tunnel (ngrok / cloudflared). WordPress needs to recognize the proxied request as HTTPS (`X-Forwarded-Proto`) — confirmed working via the Caddy proxy + `WORDPRESS_CONFIG_EXTRA` setup in INSTALL.md
- Stretch (only if time remains): OAuth 1.0a for plain HTTP. Note that WooCommerce's usage is a request-signing scheme with no user-consent step, so it may not count as a full OAuth flow

---

## 12. Documents to write

### README.md
- What it is, architecture diagram
- Quick start with the mock (3 commands max), then run the tests
- How to connect an MCP host (example config for stdio)
- Real store (primary): how to run it (INSTALL.md), what to set in `.env`, and what the demo shows. The mock is the two-minute alternative
- Assumptions and limitations (see below)

### docs/capabilities.md (what the agent can and cannot do)
**Can:** list recent orders; find orders by status and customer; get one order; look up a customer by email; receive masked, minimal data; get clear retryable/non-retryable errors.
**Cannot:** create, update, or delete anything; see addresses, payment details, IP, or free-text notes; filter by date, amount, or product (not exposed); reliably match guest orders to customers **[VERIFY]**; retrieve more than the capped results per call; act on behalf of a specific end user (one shared key).
**Defined by us:** "open" = pending + processing + on-hold.

### Limitations and long-term fix
- stdio is a local-development choice; a production connector should be a **remote HTTP server** with **MCP OAuth 2.1** so the platform can verify callers
- Shared API key: no per-user permissions or audit trail; long-term fix is per-merchant credentials stored in a secret manager, with rotation
- Masking and the allowlist reduce exposure but do not control what the platform logs or stores
- Structured filters only (no date/amount/product); extend schema as needs appear
- No default rate limiting in WooCommerce; 429 behavior is tested against the mock
- Mock can drift from the real API; mitigated by fixtures from real responses

---

## 13. Build order (about 5 to 7 hours)

1. **(0:00-0:45)** Scaffold repo, config, `.env.example`, `.gitignore`. Minimal FastMCP server with `get_order`. Call it from a host.
2. **(0:45-2:00)** HTTP client with Basic Auth against the **real store**. `list_orders` and `get_order` working end to end on real data. Start the minimal mock (from the saved fixtures) only when you reach the retry/429 work in step 4.
3. **(2:00-3:15)** `search_orders`, `find_customer`, status alias, pagination.
4. **(3:15-4:15)** Retry/429/401 handling + structured errors + tests 6-8.
5. **(4:15-5:00)** Sanitizer + PII/injection/secret tests.
6. **(5:00-6:00)** Docs, `dump_tool_spec.py`, README, demo script.
7. **Buffer (1:00)** Record the demo on the **real store** (required deliverable) and do the fresh-clone check.

If time is short, cut stretch features (guest-order fallback, caching) and polish. Do not cut the real-store demo, the tests, or the sanitizer.

---

## 14. Submission checklist

- [ ] No real customer data, passwords, or API keys anywhere (including **git history**)
- [ ] `.env` ignored; `.env.example` present
- [ ] Real-store path is reproducible from the repo (compose file, seed script, INSTALL.md), and the mock path works from a fresh clone in about 10 minutes
- [ ] Tests pass
- [ ] `docs/tool-spec.json` generated
- [ ] README has setup/run steps, assumptions, limitations
- [ ] Capabilities doc present
- [ ] Repo is public, or private with access granted to the account named in the form
- [ ] Demo video on the **real store** linked (a recorded mock run covers only the failure modes the real store cannot produce)
- [ ] Every **[VERIFY]** item resolved or listed as an assumption in the README