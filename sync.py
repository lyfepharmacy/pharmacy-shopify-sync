import os
import re
import sys
import time

import requests

SHOPIFY_CLIENT_ID = os.environ.get("SHOPIFY_CLIENT_ID")
SHOPIFY_CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET")
SHOPIFY_STORE_DOMAIN = os.environ.get("SHOPIFY_STORE_DOMAIN")
API_URL = "http://137.59.222.96:8085/myapi/LocationWiseProductInventory"

API_VERSION = "2024-01"
WRITE_DELAY = 0.5
PRICE_MULTIPLIER = 1.10
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

CATEGORY_ALIASES = {
    "cosmaceuticals": "cosmeceuticals",
    "neutraceutical": "nutraceutical",
    "nutritional & health supplement": "nutritional health and supplements",
    "nutritional and health supplement": "nutritional health and supplements",
    "food & drinks": "food and drinks",
    "devices & appliances": "devices and appliances",
}
SKIP_CATEGORIES = {"defaultcategory", "candy", "ice cream", "general", "homoeopathic"}

if not SHOPIFY_STORE_DOMAIN:
    print("ERROR: SHOPIFY_STORE_DOMAIN environment variable is missing")
    sys.exit(1)

SHOPIFY_STORE_DOMAIN = SHOPIFY_STORE_DOMAIN.replace("https://", "").replace("http://", "").strip("/")
BASE = "https://%s/admin/api/%s" % (SHOPIFY_STORE_DOMAIN, API_VERSION)
HEADERS = {}


def get_access_token():
    """Mint a fresh Admin API token each run via the client credentials grant.

    Uses the app's Client ID + Client Secret, so no token needs to be created
    or rotated by hand.
    """
    if not (SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET):
        print("ERROR: set SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET")
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


def parse_price(val):
    try:
        return round(float(str(val).strip()), 2)
    except (ValueError, TypeError):
        return 0.0


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
                "price": v.get("price"),
            }
        url = next_page_url(res)
    return products, duplicates


def get_collections():
    index = {}
    url = "%s/custom_collections.json?limit=250" % BASE
    while url:
        res = requests.get(url, headers=HEADERS, timeout=60)
        if res.status_code != 200:
            print("WARNING: could not read collections (%s); category sync will be skipped." % res.status_code)
            return None
        for c in res.json().get("custom_collections", []):
            key = normalize(c.get("title"))
            if key and key not in index:
                index[key] = c.get("id")
        url = next_page_url(res)
    return index


def get_collects():
    membership = {}
    url = "%s/collects.json?limit=250" % BASE
    while url:
        res = requests.get(url, headers=HEADERS, timeout=60)
        if res.status_code != 200:
            print("WARNING: could not read collection memberships (%s); adds will be attempted blindly." % res.status_code)
            return None
        for c in res.json().get("collects", []):
            membership.setdefault(c.get("product_id"), set()).add(c.get("collection_id"))
        url = next_page_url(res)
    return membership


def resolve_collection_id(category, collections):
    key = normalize(category)
    if not key or key in SKIP_CATEGORIES:
        return None
    key = CATEGORY_ALIASES.get(key, key)
    return collections.get(key)


def get_server_inventory():
    try:
        res = requests.get(API_URL, timeout=60)
        res.raise_for_status()
        data = res.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        print("")
        print("==============================================================")
        print("SERVER IS OFFLINE: could not reach the inventory API.")
        print("  URL  : %s" % API_URL)
        print("  Reason: %s" % e)
        print("  NOTE : Please check that the server API is running and")
        print("         reachable. The next run will pick up the sync")
        print("         automatically once the server is back online.")
        print("==============================================================")
        print("")
        sys.exit(0)
    rows = data[0].get("locationWiseProductInventoryDetail", []) if isinstance(data, list) and data else []
    inventory = {}
    for r in rows:
        if str(r.get("LocationId", "")).strip() != "1":
            continue
        key = normalize(r.get("ProductName"))
        if not key:
            continue
        inventory[key] = {
            "qty": parse_qty(r.get("LocationStock", "0")),
            "price": parse_price(r.get("ProductSalePrice")),
            "category": r.get("Category"),
        }
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


def set_variant_price(variant_id, price_str):
    payload = {"variant": {"id": variant_id, "price": price_str}}
    for _ in range(4):
        res = requests.put("%s/variants/%s.json" % (BASE, variant_id), headers=HEADERS, json=payload, timeout=60)
        if res.status_code == 429:
            wait = float(res.headers.get("Retry-After", "2"))
            time.sleep(max(wait, 2.0))
            continue
        if res.status_code == 200:
            return True, res.status_code, ""
        return False, res.status_code, res.text[:200]
    return False, 429, "rate limited"


def add_to_collection(collection_id, product_id):
    payload = {"collect": {"collection_id": collection_id, "product_id": product_id}}
    for _ in range(4):
        res = requests.post("%s/collects.json" % BASE, headers=HEADERS, json=payload, timeout=60)
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

    server_inventory = get_server_inventory()
    print("Server inventory (LocationId 1): %d unique products" % len(server_inventory))
    if not server_inventory:
        print("ERROR: server API returned no data; aborting")
        sys.exit(1)

    shopify_products, duplicates = get_shopify_products()
    print("Shopify products: %d unique titles (%d duplicate titles skipped)" % (len(shopify_products), duplicates))

    collections = get_collections()
    if collections is None:
        print("Category sync disabled (collections unavailable)")
    else:
        print("Shopify collections: %d" % len(collections))
    membership = get_collects() if collections is not None else None

    matched = {k: v for k, v in server_inventory.items() if k in shopify_products}
    print("Matched to website: %d  |  Unmatched (skipped): %d" % (len(matched), len(server_inventory) - len(matched)))

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
    price_updated = price_unchanged = price_skipped = 0
    collect_added = collect_unchanged = collect_skipped = 0
    failures = []

    for key, data in matched.items():
        info = shopify_products[key]
        item_id = info["inventory_item_id"]
        qty = data["qty"]
        need_track = not info["tracked"]
        stock_same = (
            not need_track
            and current is not None
            and item_id in current
            and int(current.get(item_id) or 0) == qty
        )

        target_price = None
        if data["price"] > 0:
            target_price = round(data["price"] * PRICE_MULTIPLIER, 2)
        if target_price is None:
            price_state = "skip"
        else:
            try:
                cur_price = round(float(info["price"]), 2)
            except (ValueError, TypeError):
                cur_price = None
            price_state = "same" if cur_price == target_price else "change"

        target_collection = resolve_collection_id(data["category"], collections) if collections is not None else None
        if collections is None or target_collection is None:
            collect_state = "skip"
        elif membership is not None and target_collection in membership.get(info["product_id"], set()):
            collect_state = "same"
        else:
            collect_state = "add"

        if stock_same:
            unchanged += 1
        elif DRY_RUN:
            updated += 1
            if need_track:
                tracking_enabled += 1
            print("[DRY] stock %r -> %d%s" % (key, qty, " (+ enable tracking)" if need_track else ""))
        else:
            ok = True
            if need_track:
                if enable_tracking(item_id):
                    tracking_enabled += 1
                else:
                    ok = False
                    failed += 1
                    failures.append((key, "tracking", "could not enable inventory tracking"))
                time.sleep(WRITE_DELAY)
            if ok:
                set_ok, status, body = set_inventory(location_id, item_id, qty)
                if set_ok:
                    updated += 1
                else:
                    failed += 1
                    failures.append((key, status, body))
                time.sleep(WRITE_DELAY)

        if price_state == "skip":
            price_skipped += 1
        elif price_state == "same":
            price_unchanged += 1
        elif DRY_RUN:
            price_updated += 1
            print("[DRY] price %r -> %.2f" % (key, target_price))
        else:
            price_ok, status, body = set_variant_price(info["variant_id"], "%.2f" % target_price)
            if price_ok:
                price_updated += 1
            else:
                failed += 1
                failures.append((key, status, body))
            time.sleep(WRITE_DELAY)

        if collect_state == "skip":
            collect_skipped += 1
        elif collect_state == "same":
            collect_unchanged += 1
        elif DRY_RUN:
            collect_added += 1
            print("[DRY] collect %r -> %s" % (key, target_collection))
        else:
            add_ok, status, body = add_to_collection(target_collection, info["product_id"])
            if add_ok:
                collect_added += 1
            else:
                failed += 1
                failures.append((key, status, body))
            time.sleep(WRITE_DELAY)

    unmatched = len(server_inventory) - len(shopify_products.keys() & server_inventory.keys())

    print("\n================ SYNC SUMMARY ================")
    print("matched            : %d" % len(matched))
    print("-- stock --")
    print("  updated          : %d" % updated)
    print("  tracking enabled : %d" % tracking_enabled)
    print("  already correct  : %d" % unchanged)
    print("-- price (x1.10) --")
    print("  updated          : %d" % price_updated)
    print("  already correct  : %d" % price_unchanged)
    print("  skipped (no price): %d" % price_skipped)
    print("-- collections --")
    print("  added            : %d" % collect_added)
    print("  already member   : %d" % collect_unchanged)
    print("  skipped (no coll): %d" % collect_skipped)
    print("-- other --")
    print("  skipped unmatched: %d" % unmatched)
    print("  failed           : %d" % failed)
    if failures:
        print("--- failures (first 50) ---")
        for key, status, body in failures[:50]:
            print("  %r -> HTTP %s: %s" % (key, status, body))
    print("=============================================")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
