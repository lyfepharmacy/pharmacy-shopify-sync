import os
import requests
import json

SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN")
SHOPIFY_STORE_DOMAIN = os.environ.get("SHOPIFY_STORE_DOMAIN")
API_URL = "http://137.59.222.96:8085/myapi/LocationWiseProductInventory"

HEADERS = {
    "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
    "Content-Type": "application/json"
}

def get_shopify_location_id():
    url = f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/2024-01/locations.json"
    response = requests.get(url, headers=HEADERS)
    locations = response.json().get('locations', [])
    if locations:
        return locations[0]['id']
    raise Exception("No Shopify locations found.")

def get_all_shopify_products():
    products = {}
    url = f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/2024-01/products.json?limit=250"
    
    while url:
        res = requests.get(url, headers=HEADERS)
        data = res.json()
        for p in data.get('products', []):
            clean_title = p['title'].strip().lower()
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
    res = requests.get(API_URL, timeout=30)
    data = res.json()
    items = data[0].get('locationWiseProductInventoryDetail', []) if isinstance(data, list) and data else []
    return items

def update_stock(inventory_item_id, location_id, qty):
    url = f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/2024-01/inventory_levels/set.json"
    payload = {
        "location_id": location_id,
        "inventory_item_id": inventory_item_id,
        "available": int(float(qty))
    }
    requests.post(url, headers=HEADERS, json=payload)

def create_draft_product(item, location_id):
    url = f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/2024-01/products.json"
    stock_qty = int(float(item.get('LocationStock', 0)))
    price = item.get('ProductSalePrice', '0.00')
    
    payload = {
        "product": {
            "title": item['ProductName'].strip(),
            "status": "draft",
            "tags": "NEW_FROM_API",
            "variants": [
                {
                    "price": price,
                    "inventory_quantity": stock_qty,
                    "inventory_management": "shopify"
                }
            ]
        }
    }
    res = requests.post(url, headers=HEADERS, json=payload)
    return res.status_code == 201

def main():
    print("Starting Sync Process...")
    shopify_loc_id = get_shopify_location_id()
    print(f"Shopify Location ID: {shopify_loc_id}")

    shopify_products = get_all_shopify_products()
    print(f"Found {len(shopify_products)} products on Shopify.")

    pharmacy_items = get_pharmacy_data()
    print(f"Fetched {len(pharmacy_items)} records from Pharmacy API.")

    updated_count = 0
    created_count = 0
    new_products_list = []

    for item in pharmacy_items:
        if str(item.get('LocationId')).strip() != "1":
            continue

        name = item.get('ProductName', '').strip()
        if not name:
            continue

        clean_name = name.lower()
        stock = item.get('LocationStock', '0')

        if clean_name in shopify_products:
            inv_item_id = shopify_products[clean_name]['inventory_item_id']
            update_stock(inv_item_id, shopify_loc_id, stock)
            updated_count += 1
        else:
            success = create_draft_product(item, shopify_loc_id)
            if success:
                created_count += 1
                new_products_list.append(name)
                shopify_products[clean_name] = {}

    print("\n--- SYNC SUMMARY ---")
    print(f"Updated Stock for: {updated_count} existing products.")
    print(f"Created Drafts for: {created_count} new products.")
    
    if new_products_list:
        print("\nNew Draft Products Added:")
        for prod in new_products_list:
            print(f"- {prod}")

if __name__ == "__main__":
    main()
