"""Mock WooCommerce REST API (SPEC.md section 8).

Reproduces only what the connector depends on: GET orders list, GET one
order, GET customers lookup; Link pagination; the four saved error fixtures
byte-for-byte; and an on-demand failure switch for retry tests.

Built from scripts/probe_api.py's saved fixtures (mock/fixtures/*.json), not
from memory, per SPEC.md's drift guard.

The failure switch is a control endpoint (`POST /__mock/config`), not a
request header: src/wc_connector's WooClient has no mechanism to attach
custom per-request headers (and must not grow mock-specific code just to
support this), so a header-based switch would only ever be reachable from a
test that talks to this FastAPI app directly, never through the real
connector. A reviewer running the mock standalone (`python -m mock.app`)
can `curl -X POST http://127.0.0.1:8800/__mock/config -d '{"fail": "429",
"times": 3, "retry_after": 2}'` and then exercise the real connector/tools
end to end against it -- no code changes to src/ required.
"""

from __future__ import annotations

import base64
import os
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import data

MOCK_CONSUMER_KEY = os.environ.get("MOCK_CONSUMER_KEY", "ck_mock_readonly")
MOCK_CONSUMER_SECRET = os.environ.get("MOCK_CONSUMER_SECRET", "cs_mock_readonly")

app = FastAPI()


class _FailureState:
    """Global, process-wide failure scenario. Deliberately not per-caller:
    the control endpoint is meant to be poked by hand (curl) or by a test
    fixture, then exercised through a separate client (the real connector,
    or a test's own httpx client) that has no way to tag its requests."""

    def __init__(self) -> None:
        self.status: int | None = None
        self.remaining: int = 0
        self.retry_after: float | None = None

    def configure(self, fail: int | None, times: int, retry_after: float | None) -> None:
        self.status = fail
        self.remaining = times if fail is not None else 0
        self.retry_after = retry_after

    def reset(self) -> None:
        self.configure(fail=None, times=0, retry_after=None)

    def as_dict(self) -> dict:
        return {"fail": self.status, "remaining": self.remaining, "retry_after": self.retry_after}


_failure_state = _FailureState()


def _error_response(status: int, body: dict) -> JSONResponse:
    return JSONResponse(status_code=status, content=body)


def _check_auth(request: Request) -> JSONResponse | None:
    """Returns an error response if auth fails, else None."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return _error_response(data.ERROR_WRONG_KEY_401["status"], data.ERROR_WRONG_KEY_401["body"])
    try:
        decoded = base64.b64decode(header[len("Basic "):]).decode()
        key, _, secret = decoded.partition(":")
    except Exception:
        return _error_response(data.ERROR_WRONG_KEY_401["status"], data.ERROR_WRONG_KEY_401["body"])

    if not (secrets.compare_digest(key, MOCK_CONSUMER_KEY) and secrets.compare_digest(secret, MOCK_CONSUMER_SECRET)):
        return _error_response(data.ERROR_WRONG_KEY_401["status"], data.ERROR_WRONG_KEY_401["body"])
    return None


def _forced_failure() -> JSONResponse | None:
    """Applies the scenario set via POST /__mock/config, if one is active and
    has failures remaining; decrements the counter each time so the scenario
    self-expires ("fail N times, then succeed")."""
    if _failure_state.status is None or _failure_state.remaining <= 0:
        return None

    _failure_state.remaining -= 1
    status = _failure_state.status
    headers = {}
    if _failure_state.retry_after is not None and status in (429, 503):
        headers["Retry-After"] = str(_failure_state.retry_after)

    if status == 401:
        body = data.ERROR_WRONG_KEY_401["body"]
    elif status == 429:
        body = {"code": "woocommerce_rest_too_many_requests", "message": "Too many requests.", "data": {"status": 429}}
    else:
        body = {"code": "woocommerce_rest_internal_error", "message": "Internal server error.", "data": {"status": status}}

    return JSONResponse(status_code=status, content=body, headers=headers)


@app.post("/__mock/config")
async def set_mock_config(request: Request) -> dict:
    """Sets (or clears, with `{"fail": null}`) the active failure scenario.

    Body: {"fail": "429"|"401"|"500"|"502"|"503"|"504"|null, "times": int,
    "retry_after": number|null}. Applies to every GET endpoint below until
    `times` is exhausted or a new config is posted.
    """
    payload = await request.json()
    fail = payload.get("fail")
    times = int(payload.get("times", 1))
    retry_after = payload.get("retry_after")
    _failure_state.configure(
        fail=int(fail) if fail is not None else None,
        times=times,
        retry_after=float(retry_after) if retry_after is not None else None,
    )
    return _failure_state.as_dict()


@app.get("/__mock/config")
async def get_mock_config() -> dict:
    return _failure_state.as_dict()


@app.post("/__mock/reset")
async def mock_reset() -> dict:
    """Test-isolation / manual-reset helper: clears the active failure scenario."""
    _failure_state.reset()
    return _failure_state.as_dict()


@app.api_route("/wp-json/wc/v3/{path:path}", methods=["POST", "PUT", "PATCH", "DELETE"])
async def reject_writes(path: str) -> JSONResponse:
    """The mock only ever represents the Read-only key: any write verb on any
    wc/v3 path reproduces the saved write-with-read-key fixture exactly."""
    return _error_response(data.ERROR_WRITE_WITH_READ_KEY["status"], data.ERROR_WRITE_WITH_READ_KEY["body"])


@app.get("/wp-json/wc/v3/orders/{order_id}")
async def get_order(order_id: int, request: Request):
    auth_error = _check_auth(request)
    if auth_error:
        return auth_error
    forced = _forced_failure()
    if forced:
        return forced

    order = data.ORDERS_BY_ID.get(order_id)
    if order is None:
        return _error_response(data.ERROR_UNKNOWN_ORDER_ID["status"], data.ERROR_UNKNOWN_ORDER_ID["body"])
    return JSONResponse(status_code=200, content=order)


def _parse_status_param(request: Request) -> list[str]:
    """Accepts both comma-joined (`status=a,b,c`) and repeated-param
    (`status[]=a&status[]=b` or `status=a&status=b`) encodings -- both are
    confirmed accepted by the real store (SPEC.md section 4.5)."""
    values = request.query_params.getlist("status") or request.query_params.getlist("status[]")
    statuses: list[str] = []
    for value in values:
        statuses.extend(part for part in value.split(",") if part)
    return statuses


@app.get("/wp-json/wc/v3/orders")
async def list_orders(request: Request):
    auth_error = _check_auth(request)
    if auth_error:
        return auth_error
    forced = _forced_failure()
    if forced:
        return forced

    statuses = _parse_status_param(request)
    if statuses:
        unknown = [s for s in statuses if s not in data.VALID_STATUSES]
        if unknown:
            return _error_response(data.ERROR_INVALID_STATUS["status"], data.ERROR_INVALID_STATUS["body"])

    per_page = int(request.query_params.get("per_page", "10"))
    if per_page > 100:
        return _error_response(
            400,
            {
                "code": "rest_invalid_param",
                "message": "Invalid parameter(s): per_page",
                "data": {
                    "status": 400,
                    "params": {"per_page": "per_page must be between 1 and 100."},
                },
            },
        )
    page = max(int(request.query_params.get("page", "1")), 1)
    customer = request.query_params.get("customer")

    orders = list(data.ORDERS_BY_ID.values())

    if statuses:
        orders = [o for o in orders if o.get("status") in statuses]
    else:
        # No explicit status filter behaves like the real store's default
        # (equivalent to the common statuses, excluding trash).
        orders = [o for o in orders if o.get("status") != "trash"]

    if customer is not None:
        orders = [o for o in orders if o.get("customer_id") == int(customer)]

    orders = sorted(orders, key=lambda o: o.get("date_created", ""), reverse=True)

    start = (page - 1) * per_page
    page_items = orders[start : start + per_page]
    has_next = start + per_page < len(orders)

    headers = {}
    if has_next:
        headers["Link"] = f'<https://example.test/wp-json/wc/v3/orders?page={page + 1}>; rel="next"'

    return JSONResponse(status_code=200, content=page_items, headers=headers)


@app.get("/wp-json/wc/v3/customers")
async def list_customers(request: Request):
    auth_error = _check_auth(request)
    if auth_error:
        return auth_error
    forced = _forced_failure()
    if forced:
        return forced

    email = request.query_params.get("email")
    customers = data.CUSTOMERS
    if email is not None:
        customers = [c for c in customers if c.get("email", "").lower() == email.lower()]
    return JSONResponse(status_code=200, content=customers)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8800)
