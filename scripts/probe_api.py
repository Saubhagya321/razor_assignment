"""Probes the real local WooCommerce API to confirm facts SPEC.md marks
[VERIFY], before the connector's mock server and tools are built from memory.

Uses the Read-only key in .env for all read-only checks. One check (whether
status=any includes trashed orders) needs to create+restore a trashed order,
which requires write access, so it borrows the Read/Write key from seed.env
for that single step only, and restores the order's original status
afterwards.

Saves real (fake-data) responses into mock/fixtures/ so the mock server is
built from observed behavior, not assumption. Prints findings as plain text;
never prints the key or secret from either env file.

Usage: python scripts/probe_api.py
"""

import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = ROOT / "mock" / "fixtures"


def load_env(path: Path) -> dict:
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


class WooClient:
    def __init__(self, base_url: str, consumer_key: str, consumer_secret: str, ca_bundle: str | None):
        import base64

        self.base_url = base_url.rstrip("/")
        token = base64.b64encode(f"{consumer_key}:{consumer_secret}".encode()).decode()
        self.auth_header = f"Basic {token}"
        self.ssl_context = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()

    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self.auth_header)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, context=self.ssl_context) as resp:
                headers = dict(resp.headers.items())
                return resp.status, headers, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            headers = dict(e.headers.items()) if e.headers else {}
            try:
                body_out = json.loads(e.read().decode())
            except Exception:
                body_out = None
            return e.code, headers, body_out

    def raw_url_request(self, method: str, url: str):
        """For testing exact query strings urlencode would otherwise mangle (e.g. status[]=a&status[]=b)."""
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", self.auth_header)
        try:
            with urllib.request.urlopen(req, context=self.ssl_context) as resp:
                headers = dict(resp.headers.items())
                return resp.status, headers, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            headers = dict(e.headers.items()) if e.headers else {}
            try:
                body_out = json.loads(e.read().decode())
            except Exception:
                body_out = None
            return e.code, headers, body_out


def save_fixture(name: str, data) -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / name
    path.write_text(json.dumps(data, indent=2))
    print(f"  saved fixture: mock/fixtures/{name}")


def finding(title: str) -> None:
    print(f"\n=== {title} ===")


def probe_status_encoding(client: WooClient):
    finding("1. How is the orders `status` array encoded?")
    base = f"{client.base_url}/wp-json/wc/v3/orders"

    url_comma = f"{base}?{urllib.parse.urlencode({'status': 'pending,processing,on-hold', 'per_page': 100})}"
    status_a, _, body_a = client.raw_url_request("GET", url_comma)
    ids_a = sorted(o["id"] for o in body_a) if isinstance(body_a, list) else None

    url_array = f"{base}?status%5B%5D=pending&status%5B%5D=processing&status%5B%5D=on-hold&per_page=100"
    status_b, _, body_b = client.raw_url_request("GET", url_array)
    ids_b = sorted(o["id"] for o in body_b) if isinstance(body_b, list) else None

    print(f"  comma form   (status=a,b,c):      HTTP {status_a}, {len(ids_a) if ids_a is not None else 'n/a'} orders, ids={ids_a}")
    print(f"  array form   (status[]=a&...):    HTTP {status_b}, {len(ids_b) if ids_b is not None else 'n/a'} orders, ids={ids_b}")

    if ids_a and not ids_b:
        print("  CONCLUSION: comma-separated form works; array form does not.")
        return "comma"
    if ids_b and not ids_a:
        print("  CONCLUSION: array form works; comma-separated form does not.")
        return "array"
    if ids_a and ids_b and ids_a == ids_b:
        print("  CONCLUSION: both forms return the same result (API accepts either).")
        return "both"
    print("  CONCLUSION: inconclusive, inspect saved fixtures manually.")
    save_fixture("status_encoding_comma.json", body_a)
    save_fixture("status_encoding_array.json", body_b)
    return "unclear"


def probe_pagination(client: WooClient):
    finding("2. Pagination headers and max per_page")
    status, headers, body = client.request("GET", "/wp-json/wc/v3/orders", params={"per_page": 1, "page": 1})
    link = headers.get("Link") or headers.get("link")
    total = headers.get("X-WP-Total")
    total_pages = headers.get("X-WP-TotalPages")
    print(f"  per_page=1: HTTP {status}")
    print(f"  Link header present: {bool(link)} -> {link}")
    print(f"  X-WP-Total: {total}, X-WP-TotalPages: {total_pages}")

    status2, _, body2 = client.request("GET", "/wp-json/wc/v3/orders", params={"per_page": 100})
    count_100 = len(body2) if isinstance(body2, list) else None
    print(f"  per_page=100: HTTP {status2}, returned {count_100} orders")

    status3, _, body3 = client.request("GET", "/wp-json/wc/v3/orders", params={"per_page": 150})
    count_150 = len(body3) if isinstance(body3, list) else body3
    print(f"  per_page=150: HTTP {status3}, result: {count_150 if not isinstance(count_150, list) else len(count_150)}")
    if status3 == 400:
        print("  CONCLUSION: per_page is capped; 150 is rejected with 400.")
    elif isinstance(count_150, list) and len(count_150) <= 100:
        print(f"  CONCLUSION: per_page silently capped at {len(count_150)} (requested 150).")
    else:
        print("  CONCLUSION: per_page=150 accepted as-is (no cap observed at this size).")


def probe_status_any_trash(client: WooClient, write_client: WooClient | None, sample_order_id: int):
    finding("3. Does status=any include trash?")
    if write_client is None:
        print("  SKIPPED: no write-capable key available (seed.env missing or empty).")
        return

    status_before, _, before_body = write_client.request("GET", f"/wp-json/wc/v3/orders/{sample_order_id}")
    original_status = before_body.get("status") if isinstance(before_body, dict) else None
    if not original_status:
        print(f"  SKIPPED: could not read order {sample_order_id} to trash it.")
        return

    trash_status, _, trash_body = write_client.request("DELETE", f"/wp-json/wc/v3/orders/{sample_order_id}")
    print(f"  Trashed order {sample_order_id} (was '{original_status}'): HTTP {trash_status}, new status={trash_body.get('status') if isinstance(trash_body, dict) else trash_body}")

    status_any, _, body_any = client.request("GET", "/wp-json/wc/v3/orders", params={"status": "any", "per_page": 100})
    ids_any = [o["id"] for o in body_any] if isinstance(body_any, list) else []
    includes_trash = sample_order_id in ids_any
    print(f"  status=any returned {len(ids_any)} orders; trashed order {'IS' if includes_trash else 'is NOT'} included")

    status_trash_filter, _, body_trash_filter = client.request("GET", "/wp-json/wc/v3/orders", params={"status": "trash", "per_page": 100})
    ids_trash = [o["id"] for o in body_trash_filter] if isinstance(body_trash_filter, list) else []
    print(f"  status=trash explicit filter: HTTP {status_trash_filter}, ids={ids_trash}")

    restore_status, _, restore_body = write_client.request(
        "PUT", f"/wp-json/wc/v3/orders/{sample_order_id}", body={"status": original_status}
    )
    print(f"  Restored order {sample_order_id} to '{original_status}': HTTP {restore_status}")

    if includes_trash:
        print("  CONCLUSION: status=any INCLUDES trashed orders. Must not expose 'any' unfiltered.")
    else:
        print("  CONCLUSION: status=any does NOT include trashed orders.")


def probe_search(client: WooClient):
    finding("4. What does the orders `search` parameter match?")
    probes = {
        "billing_email": "customer0@example.com",
        "guest_email": "guest0@example.com",
        "first_name": "Asha",
    }
    for label, term in probes.items():
        status, _, body = client.request("GET", "/wp-json/wc/v3/orders", params={"search": term, "per_page": 100})
        ids = [o["id"] for o in body] if isinstance(body, list) else None
        print(f"  search='{term}' ({label}): HTTP {status}, {len(ids) if ids is not None else 'n/a'} matches, ids={ids}")
    print("  CONCLUSION: see matches above; empty results across all probes means search is not useful for our filters.")


def probe_guest_order_and_customer_lookup(client: WooClient):
    finding("5. Guest order shape and customers?email= for a guest email")
    status, _, body = client.request("GET", "/wp-json/wc/v3/orders", params={"customer": 0, "per_page": 5})
    guest_orders = body if isinstance(body, list) else []
    print(f"  orders?customer=0: HTTP {status}, {len(guest_orders)} guest orders found")
    if guest_orders:
        sample = guest_orders[0]
        print(f"  sample guest order id={sample['id']}, customer_id={sample.get('customer_id')}, billing.email={sample.get('billing', {}).get('email')}")
        save_fixture("guest_order_sample.json", sample)
        guest_email = sample.get("billing", {}).get("email")
    else:
        guest_email = "guest0@example.com"

    status2, _, body2 = client.request("GET", "/wp-json/wc/v3/customers", params={"email": guest_email})
    print(f"  customers?email={guest_email}: HTTP {status2}, result: {body2}")
    save_fixture("customer_email_lookup_guest.json", body2)

    if isinstance(body2, list) and len(body2) == 0:
        print("  CONCLUSION: guest emails return no customer match (expected: guests have no customer account).")
    else:
        print("  CONCLUSION: unexpected non-empty result for a guest email, inspect fixture.")


def main():
    env = load_env(ROOT / ".env")
    required = ["WC_BASE_URL", "WC_CONSUMER_KEY", "WC_CONSUMER_SECRET"]
    missing = [k for k in required if not env.get(k)]
    if missing:
        print(f"Missing required keys in .env: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    client = WooClient(
        base_url=env["WC_BASE_URL"],
        consumer_key=env["WC_CONSUMER_KEY"],
        consumer_secret=env["WC_CONSUMER_SECRET"],
        ca_bundle=env.get("WC_CA_BUNDLE") or None,
    )

    write_client = None
    seed_env_path = ROOT / "seed.env"
    if seed_env_path.exists():
        seed_env = load_env(seed_env_path)
        if seed_env.get("WC_CONSUMER_KEY") and seed_env.get("WC_CONSUMER_SECRET"):
            write_client = WooClient(
                base_url=seed_env.get("WC_BASE_URL", env["WC_BASE_URL"]),
                consumer_key=seed_env["WC_CONSUMER_KEY"],
                consumer_secret=seed_env["WC_CONSUMER_SECRET"],
                ca_bundle=seed_env.get("WC_CA_BUNDLE") or None,
            )

    print("Probing live local WooCommerce API (fake seeded data only)...")

    probe_status_encoding(client)
    probe_pagination(client)

    status, _, orders = client.request("GET", "/wp-json/wc/v3/orders", params={"per_page": 1, "orderby": "date", "order": "asc"})
    sample_id = orders[0]["id"] if isinstance(orders, list) and orders else None
    if sample_id:
        probe_status_any_trash(client, write_client, sample_id)
    else:
        print("\n=== 3. Does status=any include trash? ===\n  SKIPPED: no order available to trash.")

    probe_search(client)
    probe_guest_order_and_customer_lookup(client)

    print("\nDone. Review findings above before building the connector's tools or mock server.")


if __name__ == "__main__":
    main()
