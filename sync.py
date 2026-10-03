import os
import requests
import json

SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN")
SHOPIFY_STORE_DOMAIN = os.environ.get("SHOPIFY_STORE_DOMAIN")
API_URL = "http://137.59.222.96:8085/myapi/LocationWiseProductInventory"

if not SHOPIFY_ACCESS_TOKEN or not SHOPIFY_STORE_DOMAIN:
    print("ERROR: SHOPIFY_ACCESS_TOKEN or SHOPIFY_STORE_DOMAIN environment variables are missing!")
    exit(1)

SHOPIFY_STORE_DOMAIN = SHOPIFY_STORE_DOMAIN.replace("https://", "").replace("http://", "").strip("/")

HEADERS = {
    "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
    "Content-Type": "application/json"
}

# Auto-detected location cache
CACHED_LOCATION_ID = None

def get_all_shopify_products():
    products = {}
    url = f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/2024-01/products.json?limit=250"
    
    while url:
        res = requests.get(url, headers=HEADERS)
        if res.status_code != 200:
            print(f"Error fetching Shopify products: {res.text}")
            break

        data = res.json()
        for p in data.get('products', []):
            clean_title = p['title'].strip().lower()
            if p.get('variants'):
                variant = p['variants'][0]
                products[clean_title] = {
                    'product_id': p['id'],
                    'variant_id': variant['id'],
                    'inventory_item_id': variant['inventory_item_id']
                }
        
        link_header = res.headers.get('Link', '')
        url = None
        if 'rel="next"' in link_header:
            links = link_header.split(',')
            for link in links:
                if 'rel="next"' in link:
                    url = link.split(';')[0].strip('<> ')

    return products

def get_pharmacy_data():
    try:
        res = requests.get(API_URL, timeout=30)
        res.raise_for_status()
        data = res.json()
        if isinstance(data, list) and len(data) > 0:
            return data[0].get('locationWiseProductInventoryDetail', [])
        return []
    except Exception as e:
        print(f"Error fetching data from Pharmacy API: {e}")
        return []

def parse_qty(val):
    try:
        return int(float(str(val).strip()))
    except (ValueError, TypeError):
        return 0

def update_stock(inventory_item_id, qty):
    global CACHED_LOCATION_ID

    # Auto-detect location ID directly from existing item (uses read_inventory scope)
    if not CACHED_LOCATION_ID:
        inv_url = f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/2024-01/inventory_levels.json?inventory_item_ids={inventory_item_id}"
        inv_res = requests.get(inv_url, headers=HEADERS)
        if inv_res.status_code == 200:
            levels = inv_res.json().get('inventory_levels', [])
            if levels:
                CACHED_LOCATION_ID = levels[0]['location_id']

    if not CACHED_LOCATION_ID:
        print(f"Could not update inventory for item {inventory_item_id}: Location not found.")
        return

    url = f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/2024-01/inventory_levels/set.json"
    payload = {
        "location_id": CACHED_LOCATION_ID,
        "inventory_item_id": inventory_item_id,
        "available": parse_qty(qty)
    }
    requests.post(url, headers=HEADERS, json=payload)

def create_draft_product(item):
    url = f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/2024-01/products.json"
    stock_qty = parse_qty(item.get('LocationStock', 0))
    price = str(item.get('ProductSalePrice', '0.00')).strip()
    
    payload = {
        "product": {
            "title": item['ProductName'].strip(),
            "status": "draft",
            "tags": "NEW_FROM_API",
            "variants": [
                {
                    "price": price if price else "0.00",
                    "inventory_quantity": stock_qty,
                    "inventory_management": "shopify"
                }
            ]
        }
    }
    res = requests.post(url, headers=HEADERS, json=payload)
    return res.status_code == 201

def main():
    print("--- STARTING PHARMACY TO SHOPIFY SYNC ---")

    # 1. Fetch Existing Shopify Products
    shopify_products = get_all_shopify_products()
    print(f" Loaded {len(shopify_products)} existing products from Shopify.")

    # 2. Fetch Pharmacy API Items
    pharmacy_items = get_pharmacy_data()
    print(f" Fetched {len(pharmacy_items)} product records from Pharmacy API.")

    if not pharmacy_items:
        print("⚠ Warning: No data returned from Pharmacy API or connection failed.")
        return

    updated_count = 0
    created_count = 0
    new_products_list = []

    # 3. Process Sync (Filtered for HEAD OFFICE / LocationId 1)
    for item in pharmacy_items:
        if str(item.get('LocationId', '')).strip() != "1":
            continue

        name = str(item.get('ProductName', '')).strip()
        if not name:
            continue

        clean_name = name.lower()
        stock = item.get('LocationStock', '0')

        if clean_name in shopify_products:
            inv_item_id = shopify_products[clean_name]['inventory_item_id']
            update_stock(inv_item_id, stock)
            updated_count += 1
        else:
            success = create_draft_product(item)
            if success:
                created_count += 1
                new_products_list.append(name)
                shopify_products[clean_name] = {}

    print("\n================ SYNC SUMMARY ================")
    print(f" Successfully updated stock for: {updated_count} products.")
    print(f" Created as draft: {created_count} new products.")
    
    if new_products_list:
        print("\nNew Draft Products Added to Shopify:")
        for prod in new_products_list:
            print(f" - {prod}")
    print("==============================================")

if __name__ == "__main__":
    main()
