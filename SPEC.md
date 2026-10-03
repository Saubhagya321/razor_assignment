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
| Default backend for reviewers | **Mock server** (zero setup, no secrets) |
| Optional backend | Real store, selected only by config (base URL + credentials) |
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

**stdio rule [VERIFY in the transport spec]:** with the stdio transport, stdout carries only MCP protocol messages. Never use `print()` or any logger that writes to stdout. All logs go to **stderr**. A stray stdout write can corrupt the protocol stream and break the host connection.

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

**Annotations on every tool [VERIFY against the spec version your SDK targets]:**
`readOnlyHint: true`, `destructiveHint: false`, `idempotentHint: true`, `openWorldHint: true` (it talks to an external system), plus a human-readable `title`.
These are **hints only**. The MCP spec says clients should treat annotations as untrusted unless the server is trusted, so they do not replace enforcement. The real enforcement is the Read-only API key.

**Structured output:** declare an `outputSchema` per tool and return results as `structuredContent` (an object, never a bare list), with the same JSON also serialized in a text content block for older clients. Pin the SDK version and record in the README which MCP spec revision you targeted. **[VERIFY]** how your SDK version generates `outputSchema` from return types. Error results do not carry `structuredContent` (see section 6).

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
- **Orders filters (confirmed in the reference):** `status` is an **array**, so "open" is **one** upstream call with `pending, processing, on-hold`; `customer` takes an integer customer id. There is no email filter on orders. **[VERIFY empirically]** how the array is encoded in the URL (`status=a,b` vs `status[]=a&status[]=b`) against the local store; the mock must use whichever form the real store accepts.

**Description (draft):**
> Finds orders by condition: status and/or customer. Use this when the request includes a condition ("open orders for customer X"). For browsing recent orders with no condition, use `list_orders`. "open" means pending, processing, or on-hold. Customers can be given by id or by email; guest orders may not be matched to a customer. Returns up to 20 order summaries per page plus `has_more`. Example: `search_orders(status="open", customer_email="a@example.com")`.

### 4.3 `get_order`
**Args:** `order_id` (int)

**Returns:** One order with line items (name, quantity, line total), status, totals, dates, customer id, and masked contact info if kept by policy. Same allowlist as the summaries, plus line items.

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

**Not documented in the pasted pages [VERIFY on the real store]:** the `Link` / total-count pagination headers, the maximum allowed `per_page`, whether `any` includes `trash`, and whether orders `search` matches billing email.

**Mock must reproduce:** status as an array (real encoding), `customer` filter, `customers?email=` exact match, `customer_id: 0` guest orders in the fixtures, `per_page` and paging headers as observed on the real store.

### Pagination contract (all list-like tools)
Return `{ items: [...], page, has_more, note? }`. Derive `has_more` from the response's `Link: rel="next"` header (confirmed in docs). **[VERIFY]** also whether total-count headers exist and are reliable. If results exceed what can be returned, `note` tells the model to narrow the query.

---

## 5. Output safety: sanitizer

**Principle: allowlist, never blocklist.** Build a new small object from each upstream record; never delete fields from the raw one.

| Treatment | Fields |
|---|---|
| **Return** | order id, status, total, currency, created date, line items (name, quantity), customer id |
| **Mask** | email -> `a***@domain.com`; phone -> last 4 digits only. Only if useful to the agent |
| **Never return** | `billing`/`shipping` objects, `customer_ip_address`, `customer_user_agent`, `payment_method*`, `transaction_id`, `order_key`, `cart_hash`, `meta_data` (order and line-item level), coupon/tax/fee/shipping lines |
| **Drop entirely** | free-text fields (customer notes, order notes). Do not try to regex-mask them |

Additional rules:
- Cap string lengths on any outsider-written text that is kept (product names, customer names) to limit injection surface.
- Treat all store data as **data, not instructions**. Say so in tool descriptions, but remember this is weak; not returning the field is the strong defense.
- Logs contain order/customer **ids**, never emails or phone numbers.
- **Volume cap:** page size is capped by `WC_PAGE_SIZE_MAX`.
- **Verify real field names [VERIFY]** from a saved live response before writing the allowlist.

---

## 6. Error model

Tools never raise raw exceptions or upstream bodies to the model. Every failure is returned as a normal tool result with **`isError: true`** (MCP "tool execution error"). The spec says clients should pass these to the model so it can self-correct, whereas JSON-RPC protocol errors are for malformed requests and unknown tools. Input-validation failures (bad enum, missing required arg) are also `isError: true` results with an actionable message. The text content of the result is this JSON (error results carry no `structuredContent`):

```json
{ "error": { "code": "RATE_LIMITED", "message": "...", "retryable": true, "retry_after_seconds": 30 } }
```

| Upstream | Code | retryable | Behavior |
|---|---|---|---|
| 401 | `AUTH_FAILED` | **false** | No retry. Message: credentials need checking; tell the user. Never echo headers or keys |
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

**Drift guard:** run the connector once against a real local store, save a few real responses (fake data only) as fixtures, and build the mock from them. Do not write the mock from memory.

---

## 9. Tests (run against the mock; no secrets needed)

1. `list_orders` returns newest first and respects page size
2. `search_orders(status="open")` expands to pending + processing + on-hold
3. `search_orders` by customer id, by email, by both (error), by neither (error)
4. Email with no match returns empty with a message
5. `get_order` happy path and 404
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

## 11. Optional real-store path (documented, not required)

- `docker-compose.yml`: WordPress + MySQL; install the WooCommerce plugin
- Enable pretty permalinks so `/wp-json/` routes work **[VERIFY]**
- Create a **Read-only** REST API key (WooCommerce -> Settings -> Advanced -> REST API)
- `scripts/seed_store.py`: ~10 fake customers and ~20 orders across statuses (fake data only)
- HTTPS: Basic Auth on a real store needs HTTPS. Options: local reverse proxy with a local cert, or a tunnel (ngrok / cloudflared). WordPress may need to recognize the proxied request as HTTPS (`X-Forwarded-Proto`) **[VERIFY]**
- Stretch (only if time remains): OAuth 1.0a for plain HTTP. Note that WooCommerce's usage is a request-signing scheme with no user-consent step, so it may not count as a full OAuth flow

---

## 12. Documents to write

### README.md
- What it is, architecture diagram
- Quick start with the mock (3 commands max), then run the tests
- How to connect an MCP host (example config for stdio)
- Optional: switching to a real store (what to set in `.env`)
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
2. **(0:45-2:00)** Mock server + fixtures. HTTP client with Basic Auth. `list_orders`, `get_order` against the mock.
3. **(2:00-3:15)** `search_orders`, `find_customer`, status alias, pagination.
4. **(3:15-4:15)** Retry/429/401 handling + structured errors + tests 6-8.
5. **(4:15-5:00)** Sanitizer + PII/injection/secret tests.
6. **(5:00-6:00)** Docs, `dump_tool_spec.py`, README, demo script.
7. **Buffer (1:00)** Real-store run: compose, seed, read-only key, record a short demo video.

If time is short, cut the real-store demo before cutting tests or the sanitizer.

---

## 14. Submission checklist

- [ ] No real customer data, passwords, or API keys anywhere (including **git history**)
- [ ] `.env` ignored; `.env.example` present
- [ ] Mock path works from a fresh clone in about 10 minutes
- [ ] Tests pass
- [ ] `docs/tool-spec.json` generated
- [ ] README has setup/run steps, assumptions, limitations
- [ ] Capabilities doc present
- [ ] Repo is public, or private with access granted to the account named in the form
- [ ] Demo video (real store) or recorded mock run linked
- [ ] Every **[VERIFY]** item resolved or listed as an assumption in the README
