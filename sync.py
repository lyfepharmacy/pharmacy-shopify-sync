import os
import re
import sys
import time

import requests

SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN")
SHOPIFY_CLIENT_ID = os.environ.get("SHOPIFY_CLIENT_ID")
SHOPIFY_CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET")
SHOPIFY_STORE_DOMAIN = os.environ.get("SHOPIFY_STORE_DOMAIN")
API_URL = "http://137.59.222.96:8085/myapi/LocationWiseProductInventory"

API_VERSION = "2024-01"
WRITE_DELAY = 0.5
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

if not SHOPIFY_STORE_DOMAIN:
    print("ERROR: SHOPIFY_STORE_DOMAIN environment variable is missing")
    sys.exit(1)

SHOPIFY_STORE_DOMAIN = SHOPIFY_STORE_DOMAIN.replace("https://", "").replace("http://", "").strip("/")
BASE = "https://%s/admin/api/%s" % (SHOPIFY_STORE_DOMAIN, API_VERSION)
HEADERS = {}


def get_access_token():
    """Return an Admin API token.

    Preferred: mint a fresh token each run via the client credentials grant using
    the app's permanent Client ID + Client secret, so no token ever needs to be
    created or rotated by hand. Falls back to a static SHOPIFY_ACCESS_TOKEN if set.
    """
    if SHOPIFY_ACCESS_TOKEN:
        return SHOPIFY_ACCESS_TOKEN
    if not (SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET):
        print("ERROR: set SHOPIFY_ACCESS_TOKEN, or SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET")
        sys.exit(1)
    res = requests.post(
        "https://%s/admin/oauth/access_token" % SHOPIFY_STORE_DOMAIN,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "client_id": SHOPIFY_CLIENT_ID,
            "client_secret": SHOPIFY_CLIENT_SECRET,
        },
        timeout=60,
    )
    if res.status_code != 200:
        print("ERROR fetching access token: %s %s" % (res.status_code, res.text[:300]))
        sys.exit(1)
    token = res.json().get("access_token")
    if not token:
        print("ERROR: token endpoint returned no access_token")
        sys.exit(1)
    return token


def normalize(name):
    """Case-insensitive, whitespace-collapsed key (handles the &nbsp; seen on the site)."""
    if name is None:
        return ""
    name = str(name).replace("\u00a0", " ")
    name = re.sub(r"\s+", " ", name)
    return name.strip().lower()


def parse_qty(val):
    try:
        return int(float(str(val).strip()))
    except (ValueError, TypeError):
        return 0


def next_page_url(res):
    link = res.headers.get("Link", "")
    if 'rel="next"' not in link:
        return None
    for part in link.split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip().strip("<> ")
    return None


def get_shopify_products():
    products = {}
    duplicates = 0
    url = "%s/products.json?limit=250" % BASE
    while url:
        res = requests.get(url, headers=HEADERS, timeout=60)
        if res.status_code != 200:
            print("ERROR fetching Shopify products: %s %s" % (res.status_code, res.text[:300]))
            sys.exit(1)
        for p in res.json().get("products", []):
            key = normalize(p.get("title"))
            if not key:
                continue
            variants = p.get("variants") or []
            if not variants:
                continue
            v = variants[0]
            if key in products:
                duplicates += 1
                continue
            products[key] = {
                "product_id": p.get("id"),
                "variant_id": v.get("id"),
                "inventory_item_id": v.get("inventory_item_id"),
                "tracked": v.get("inventory_management") == "shopify",
            }
        url = next_page_url(res)
    return products, duplicates


def get_server_inventory():
    res = requests.get(API_URL, timeout=60)
    res.raise_for_status()
    data = res.json()
    rows = data[0].get("locationWiseProductInventoryDetail", []) if isinstance(data, list) and data else []
    inventory = {}
    for r in rows:
        if str(r.get("LocationId", "")).strip() != "1":
            continue
        key = normalize(r.get("ProductName"))
        if not key:
            continue
        inventory[key] = parse_qty(r.get("LocationStock", "0"))
    return inventory


def detect_location_id(inventory_item_id):
    url = "%s/inventory_levels.json?inventory_item_ids=%s" % (BASE, inventory_item_id)
    res = requests.get(url, headers=HEADERS, timeout=60)
    if res.status_code == 200:
        levels = res.json().get("inventory_levels", [])
        if levels:
            return levels[0]["location_id"]
    return None


def enable_tracking(inventory_item_id):
    url = "%s/inventory_items/%s.json" % (BASE, inventory_item_id)
    res = requests.put(url, headers=HEADERS, json={"inventory_item": {"tracked": True}}, timeout=60)
    return res.status_code == 200


def get_current_levels(location_id):
    levels = {}
    url = "%s/inventory_levels.json?location_ids=%s&limit=250" % (BASE, location_id)
    while url:
        res = requests.get(url, headers=HEADERS, timeout=60)
        if res.status_code != 200:
            print("WARNING: could not pre-read inventory levels (%s); will write every matched item." % res.status_code)
            return None
        for lv in res.json().get("inventory_levels", []):
            levels[lv["inventory_item_id"]] = lv.get("available", 0)
        url = next_page_url(res)
    return levels


def set_inventory(location_id, inventory_item_id, qty):
    payload = {
        "location_id": location_id,
        "inventory_item_id": inventory_item_id,
        "available": qty,
    }
    for _ in range(4):
        res = requests.post("%s/inventory_levels/set.json" % BASE, headers=HEADERS, json=payload, timeout=60)
        if res.status_code == 429:
            wait = float(res.headers.get("Retry-After", "2"))
            time.sleep(max(wait, 2.0))
            continue
        if res.status_code in (200, 201):
            return True, res.status_code, ""
        return False, res.status_code, res.text[:200]
    return False, 429, "rate limited"


def main():
    print("=== LYFE Shopify Inventory Sync ===")
    if DRY_RUN:
        print("DRY RUN enabled: no writes will be performed")

    HEADERS["X-Shopify-Access-Token"] = get_access_token()
    HEADERS["Content-Type"] = "application/json"
    print("Authenticated with Shopify Admin API")

    shopify_products, duplicates = get_shopify_products()
    print("Shopify products: %d unique titles (%d duplicate titles skipped)" % (len(shopify_products), duplicates))

    server_inventory = get_server_inventory()
    print("Server inventory (LocationId 1): %d unique products" % len(server_inventory))
    if not server_inventory:
        print("ERROR: no server data returned; aborting")
        sys.exit(1)

    matched = {k: v for k, v in server_inventory.items() if k in shopify_products}
    unmatched = len(server_inventory) - len(matched)
    print("Matched to website: %d  |  Unmatched (skipped): %d" % (len(matched), unmatched))

    location_id = None
    for key in matched:
        location_id = detect_location_id(shopify_products[key]["inventory_item_id"])
        if location_id:
            break
    if not location_id:
        print("ERROR: could not determine inventory location id; aborting")
        sys.exit(1)
    print("Using location_id: %s" % location_id)

    current = get_current_levels(location_id)

    updated = unchanged = tracking_enabled = failed = 0
    failures = []

    for key, qty in matched.items():
        info = shopify_products[key]
        item_id = info["inventory_item_id"]
        need_track = not info["tracked"]

        if not need_track and current is not None and item_id in current and int(current.get(item_id) or 0) == qty:
            unchanged += 1
            continue

        if DRY_RUN:
            print("[DRY] %r -> %d%s" % (key, qty, " (+ enable tracking)" if need_track else ""))
            updated += 1
            if need_track:
                tracking_enabled += 1
            continue

        if need_track:
            if not enable_tracking(item_id):
                failed += 1
                failures.append((key, "tracking", "could not enable inventory tracking"))
                time.sleep(WRITE_DELAY)
                continue
            tracking_enabled += 1
            time.sleep(WRITE_DELAY)

        ok, status, body = set_inventory(location_id, inventory_item_id=item_id, qty=qty)
        if ok:
            updated += 1
        else:
            failed += 1
            failures.append((key, status, body))
        time.sleep(WRITE_DELAY)

    unmatched = len(server_inventory) - len(shopify_products.keys() & server_inventory.keys())

    print("\n================ SYNC SUMMARY ================")
    print("matched            : %d" % len(matched))
    print("updated            : %d" % updated)
    print("tracking enabled   : %d" % tracking_enabled)
    print("already correct    : %d" % unchanged)
    print("skipped unmatched  : %d" % unmatched)
    print("failed             : %d" % failed)
    if failures:
        print("--- failures (first 50) ---")
        for key, status, body in failures[:50]:
            print("  %r -> HTTP %s: %s" % (key, status, body))
    print("=============================================")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
