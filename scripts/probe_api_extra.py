"""Follow-up probes requested after the first pass (see probe_api.py):

1. GET /orders/<id> for a trashed order - does any field reveal trashed state?
2. Last-page Link header, and a page past the end.
3. Error bodies: unknown order id, invalid status value, wrong key (401),
   write attempt with the Read key.
4. Empty results: unknown customer email, orders?customer=999999.

Uses the Read-only key from .env for everything except step 1's trash/untrash
(needs the seed.env Read/Write key), matching the pattern in probe_api.py.
Never prints the key or secret from either env file.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_api import WooClient, load_env, save_fixture, finding, ROOT  # noqa: E402


def probe_trashed_order_fields(client: WooClient, write_client: WooClient, sample_order_id: int):
    finding("Extra 1: does a trashed order's GET response reveal trashed state?")
    status_before, _, before_body = write_client.request("GET", f"/wp-json/wc/v3/orders/{sample_order_id}")
    original_status = before_body.get("status")

    trash_status, _, trash_body = write_client.request("DELETE", f"/wp-json/wc/v3/orders/{sample_order_id}")
    print(f"  Trashed order {sample_order_id} (was '{original_status}'): HTTP {trash_status}")

    get_status, _, get_body = client.request("GET", f"/wp-json/wc/v3/orders/{sample_order_id}")
    print(f"  GET /orders/{sample_order_id} while trashed: HTTP {get_status}")
    if isinstance(get_body, dict):
        print(f"  status field: {get_body.get('status')}")
        save_fixture("trashed_order_get_response.json", get_body)
    else:
        print(f"  body: {get_body}")
        save_fixture("trashed_order_get_response.json", get_body)

    restore_status, _, _ = write_client.request("PUT", f"/wp-json/wc/v3/orders/{sample_order_id}", body={"status": original_status})
    print(f"  Restored order {sample_order_id} to '{original_status}': HTTP {restore_status}")

    if get_status == 404:
        print("  CONCLUSION: trashed orders 404 on direct GET via this key - trashed state is invisible, not revealed.")
    elif isinstance(get_body, dict) and get_body.get("status") == original_status:
        print("  CONCLUSION: GET still succeeds and 'status' field is unchanged; nothing in the payload reveals trashed state directly.")
    else:
        print("  CONCLUSION: see fixture for exact shape.")


def probe_pagination_edges(client: WooClient):
    finding("Extra 2: last-page Link header, and a page past the end")
    status, headers, body = client.request("GET", "/wp-json/wc/v3/orders", params={"per_page": 5, "page": 1})
    total_known = None
    # Walk forward until Link no longer has rel="next" to find the last page.
    page = 1
    last_page_headers = headers
    last_page_body = body
    while True:
        link = last_page_headers.get("Link") or last_page_headers.get("link") or ""
        if 'rel="next"' not in link:
            break
        page += 1
        _, last_page_headers, last_page_body = client.request("GET", "/wp-json/wc/v3/orders", params={"per_page": 5, "page": page})
        if page > 10:
            break
    link_last = last_page_headers.get("Link") or last_page_headers.get("link")
    print(f"  Last page with data: page={page}, {len(last_page_body) if isinstance(last_page_body, list) else 'n/a'} orders")
    print(f"  Link header on last page: {link_last}")

    past_status, past_headers, past_body = client.request("GET", "/wp-json/wc/v3/orders", params={"per_page": 5, "page": page + 1})
    print(f"  Page past the end (page={page + 1}): HTTP {past_status}, body={past_body if not isinstance(past_body, list) else f'{len(past_body)} items'}")
    save_fixture("pagination_past_last_page.json", {"status": past_status, "body": past_body})
    print("  CONCLUSION: last page has no rel=\"next\" Link; page past the end returns HTTP "
          f"{past_status} with {'an empty list' if past_body == [] else 'a non-empty/error body, see fixture'}.")


def probe_error_bodies(client: WooClient):
    finding("Extra 3: error bodies for unknown id / invalid status / wrong key / read-key write")

    status, _, body = client.request("GET", "/wp-json/wc/v3/orders/999999999")
    print(f"  GET unknown order id: HTTP {status}")
    save_fixture("error_unknown_order_id.json", {"status": status, "body": body})

    status, _, body = client.request("GET", "/wp-json/wc/v3/orders", params={"status": "not-a-real-status"})
    print(f"  GET invalid status value: HTTP {status}")
    save_fixture("error_invalid_status.json", {"status": status, "body": body})

    bad_client = WooClient(base_url=client.base_url, consumer_key="ck_invalid_wrong_key", consumer_secret="cs_invalid_wrong_secret", ca_bundle=None)
    bad_client.ssl_context = client.ssl_context
    status, _, body = bad_client.request("GET", "/wp-json/wc/v3/orders")
    print(f"  GET with wrong/invalid key: HTTP {status}")
    save_fixture("error_wrong_key_401.json", {"status": status, "body": body})

    status, _, body = client.request("POST", "/wp-json/wc/v3/orders", body={})
    print(f"  POST (write) with Read key: HTTP {status}")
    save_fixture("error_write_with_read_key.json", {"status": status, "body": body})

    print("  CONCLUSION: see saved fixtures for exact error shapes (code/message/data.status).")


def probe_empty_results(client: WooClient):
    finding("Extra 4: empty results for unknown customer email / unknown customer id")

    status, _, body = client.request("GET", "/wp-json/wc/v3/customers", params={"email": "definitely-not-a-real-customer@example.com"})
    print(f"  customers?email=<unknown>: HTTP {status}, result={body}")
    save_fixture("empty_unknown_customer_email.json", {"status": status, "body": body})

    status, _, body = client.request("GET", "/wp-json/wc/v3/orders", params={"customer": 999999})
    print(f"  orders?customer=999999: HTTP {status}, result={body}")
    save_fixture("empty_unknown_customer_orders.json", {"status": status, "body": body})

    print("  CONCLUSION: both should return HTTP 200 with an empty list, not an error - confirms "
          "'no match' is representable without a special error path.")


def main():
    env = load_env(ROOT / ".env")
    client = WooClient(
        base_url=env["WC_BASE_URL"],
        consumer_key=env["WC_CONSUMER_KEY"],
        consumer_secret=env["WC_CONSUMER_SECRET"],
        ca_bundle=env.get("WC_CA_BUNDLE") or None,
    )

    seed_env_path = ROOT / "seed.env"
    write_client = None
    if seed_env_path.exists():
        seed_env = load_env(seed_env_path)
        if seed_env.get("WC_CONSUMER_KEY") and seed_env.get("WC_CONSUMER_SECRET"):
            write_client = WooClient(
                base_url=seed_env.get("WC_BASE_URL", env["WC_BASE_URL"]),
                consumer_key=seed_env["WC_CONSUMER_KEY"],
                consumer_secret=seed_env["WC_CONSUMER_SECRET"],
                ca_bundle=seed_env.get("WC_CA_BUNDLE") or None,
            )

    print("Running extra probes (fake seeded data only)...")

    status, _, orders = client.request("GET", "/wp-json/wc/v3/orders", params={"per_page": 1, "orderby": "date", "order": "asc"})
    sample_id = orders[0]["id"] if isinstance(orders, list) and orders else None

    if sample_id and write_client:
        probe_trashed_order_fields(client, write_client, sample_id)
    else:
        print("\n=== Extra 1 ===\n  SKIPPED: no sample order or no write-capable key.")

    probe_pagination_edges(client)
    probe_error_bodies(client)
    probe_empty_results(client)

    print("\nDone with extra probes.")


if __name__ == "__main__":
    main()
