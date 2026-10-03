"""Smoke tests for the mock WooCommerce server (SPEC.md section 8): auth,
the four saved error fixtures reproduced byte-for-byte, pagination via the
Link header, status encoding (comma-joined and repeated-param), and the
on-demand failure switch ("fail N times, then succeed").
"""

import base64

import pytest
from fastapi.testclient import TestClient

from mock import data
from mock.app import MOCK_CONSUMER_KEY, MOCK_CONSUMER_SECRET, app, mock_reset

client = TestClient(app)
AUTH = (MOCK_CONSUMER_KEY, MOCK_CONSUMER_SECRET)


@pytest.fixture(autouse=True)
async def reset_mock_config():
    await mock_reset()
    yield
    await mock_reset()


def test_wrong_key_returns_error_fixture_exactly():
    response = client.get("/wp-json/wc/v3/orders/1", auth=("bad_key", "bad_secret"))
    assert response.status_code == data.ERROR_WRONG_KEY_401["status"]
    assert response.json() == data.ERROR_WRONG_KEY_401["body"]


def test_missing_auth_header_returns_error_fixture_exactly():
    response = client.get("/wp-json/wc/v3/orders/1")
    assert response.status_code == data.ERROR_WRONG_KEY_401["status"]
    assert response.json() == data.ERROR_WRONG_KEY_401["body"]


def test_unknown_order_id_returns_error_fixture_exactly():
    response = client.get("/wp-json/wc/v3/orders/999999", auth=AUTH)
    assert response.status_code == data.ERROR_UNKNOWN_ORDER_ID["status"]
    assert response.json() == data.ERROR_UNKNOWN_ORDER_ID["body"]


def test_invalid_status_returns_error_fixture_exactly():
    response = client.get("/wp-json/wc/v3/orders", params={"status": "not-a-real-status"}, auth=AUTH)
    assert response.status_code == data.ERROR_INVALID_STATUS["status"]
    assert response.json() == data.ERROR_INVALID_STATUS["body"]


def test_write_attempt_with_read_key_returns_error_fixture_exactly():
    response = client.post("/wp-json/wc/v3/orders", json={}, auth=AUTH)
    assert response.status_code == data.ERROR_WRITE_WITH_READ_KEY["status"]
    assert response.json() == data.ERROR_WRITE_WITH_READ_KEY["body"]


def test_get_order_happy_path():
    response = client.get(f"/wp-json/wc/v3/orders/{data.GUEST_ORDER['id']}", auth=AUTH)
    assert response.status_code == 200
    assert response.json()["id"] == data.GUEST_ORDER["id"]


def test_get_trashed_order_returns_trash_status():
    response = client.get(f"/wp-json/wc/v3/orders/{data.TRASHED_ORDER['id']}", auth=AUTH)
    assert response.status_code == 200
    assert response.json()["status"] == "trash"


def test_status_filter_accepts_comma_joined_and_repeated_param_forms():
    comma = client.get("/wp-json/wc/v3/orders", params={"status": "pending,processing"}, auth=AUTH)
    repeated = client.get(
        "/wp-json/wc/v3/orders?status=pending&status=processing", auth=AUTH
    )
    assert comma.status_code == repeated.status_code == 200
    assert {o["id"] for o in comma.json()} == {o["id"] for o in repeated.json()}


def test_trash_excluded_unless_explicitly_requested():
    default_list = client.get("/wp-json/wc/v3/orders", auth=AUTH)
    assert data.TRASHED_ORDER["id"] not in {o["id"] for o in default_list.json()}

    trash_list = client.get("/wp-json/wc/v3/orders", params={"status": "trash"}, auth=AUTH)
    assert data.TRASHED_ORDER["id"] in {o["id"] for o in trash_list.json()}


def test_per_page_cap_returns_400():
    response = client.get("/wp-json/wc/v3/orders", params={"per_page": "101"}, auth=AUTH)
    assert response.status_code == 400
    assert response.json()["code"] == "rest_invalid_param"


def test_pagination_past_last_page_returns_empty_200():
    response = client.get("/wp-json/wc/v3/orders", params={"page": "999", "per_page": "10"}, auth=AUTH)
    assert response.status_code == 200
    assert response.json() == []
    assert "Link" not in response.headers


def test_customer_email_lookup_exact_match_and_empty():
    found = client.get("/wp-json/wc/v3/customers", params={"email": "injection.target@example.com"}, auth=AUTH)
    assert found.status_code == 200
    assert len(found.json()) == 1

    not_found = client.get("/wp-json/wc/v3/customers", params={"email": "nobody@example.com"}, auth=AUTH)
    assert not_found.status_code == 200
    assert not_found.json() == []


def test_forced_failure_then_success_after_fail_times_exhausted():
    config_response = client.post("/__mock/config", json={"fail": 503, "times": 2})
    assert config_response.status_code == 200

    first = client.get("/wp-json/wc/v3/orders/1", auth=AUTH)
    second = client.get("/wp-json/wc/v3/orders/1", auth=AUTH)
    third = client.get(f"/wp-json/wc/v3/orders/{data.GUEST_ORDER['id']}", auth=AUTH)

    assert first.status_code == 503
    assert second.status_code == 503
    assert third.status_code == 200


def test_forced_failure_429_honors_retry_after_header():
    client.post("/__mock/config", json={"fail": 429, "times": 5, "retry_after": 7})
    response = client.get("/wp-json/wc/v3/orders/1", auth=AUTH)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "7.0"


def test_mock_config_can_be_cleared_via_fail_null():
    client.post("/__mock/config", json={"fail": 429, "times": 5})
    client.post("/__mock/config", json={"fail": None})
    response = client.get(f"/wp-json/wc/v3/orders/{data.GUEST_ORDER['id']}", auth=AUTH)
    assert response.status_code == 200


def test_mock_config_endpoint_is_readable():
    client.post("/__mock/config", json={"fail": 500, "times": 3, "retry_after": None})
    response = client.get("/__mock/config")
    assert response.json() == {"fail": 500, "remaining": 3, "retry_after": None}
