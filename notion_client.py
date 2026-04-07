import os
import httpx

NOTION_API_KEY = os.getenv("NOTION_API_KEY")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
NOTION_SALES_DB_ID = os.getenv("NOTION_SALES_DB_ID")
FF_SALES_LOG_DB_ID = os.getenv("FF_SALES_LOG_DB_ID", "a3406a84-4be0-41d0-9593-8090cae4133c")
NOTION_BASE_URL = "https://api.notion.com/v1"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


async def query_products(search_term: str = "") -> list[dict]:
    """Query the Notion inventory database. Optionally filter by product name."""
    url = f"{NOTION_BASE_URL}/databases/{NOTION_DATABASE_ID}/query"

    payload = {}
    if search_term:
        payload["filter"] = {
            "property": "Product Name",
            "title": {"contains": search_term},
        }

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=NOTION_HEADERS, json=payload)
        resp.raise_for_status()
        data = resp.json()

    products = []
    for page in data.get("results", []):
        props = page["properties"]
        products.append({
            "id": page["id"],
            "name": _get_title(props, "Product Name"),
            "item_code": _get_rich_text(props, "FF Item Code"),
            "category": _get_select(props, "Category"),
            "color_attribute": _get_rich_text(props, "Color/Attribute"),
            "stock": _get_number(props, "Stock"),
            "stock_boxes": _get_number(props, "Stock (Boxes)"),
            "price": _get_number(props, "Unit Price"),
            "min_sellable": _get_number(props, "Min Sellable (Floor)"),
            "srp_1_5x": _get_number(props, "SRP @ 1.5x + VAT (₱)"),
        })
    return products


async def update_stock(page_id: str, new_stock: int) -> bool:
    """Update the stock quantity for a product."""
    url = f"{NOTION_BASE_URL}/pages/{page_id}"
    payload = {
        "properties": {
            "Stock": {"number": new_stock},
        }
    }
    async with httpx.AsyncClient() as client:
        resp = await client.patch(url, headers=NOTION_HEADERS, json=payload)
        resp.raise_for_status()
    return True


# Map of allowed pricing field keys to their exact Notion property names
PRICING_FIELDS = {
    "unit_price": "Unit Price",
    "landed_cost": "Landed Cost (₱)",
    "min_sellable": "Min Sellable (Floor)",
    "srp_1_5x": "SRP @ 1.5x + VAT (₱)",
    "srp_2_0x": "SRP @ 2.0x + VAT (₱)",
    "srp_3_0x": "SRP @ 3.0x + VAT (₱)",
    "usd_per_pc": "USD/pc (Ex Works)",
}


async def update_price(page_id: str, new_price: float, field: str = "unit_price") -> bool:
    """Update a pricing field for a product. Field must be a key from PRICING_FIELDS."""
    notion_property = PRICING_FIELDS.get(field)
    if not notion_property:
        raise ValueError(f"Unknown pricing field: {field}. Valid fields: {list(PRICING_FIELDS.keys())}")
    url = f"{NOTION_BASE_URL}/pages/{page_id}"
    payload = {
        "properties": {
            notion_property: {"number": new_price},
        }
    }
    async with httpx.AsyncClient() as client:
        resp = await client.patch(url, headers=NOTION_HEADERS, json=payload)
        resp.raise_for_status()
    return True


async def log_sale(
    customer_name: str,
    product_sold: str,
    quantity: int,
    unit: str,
    unit_price: float,
    payment_method: str,
    payment_status: str,
    transaction_type: str,
    handled_by: str,
    customer_contact: str = "",
    amount_received: float | None = None,
    notes: str = "",
) -> bool:
    """Log a sale transaction to the Daily Sales Ledger database."""
    from datetime import date

    today = date.today().isoformat()
    total = quantity * unit_price
    balance_due = total - (amount_received if amount_received is not None else total)
    if amount_received is None:
        amount_received = total  # default: fully paid

    sale_entry = f"{customer_name} - {product_sold} - {today}"

    url = f"{NOTION_BASE_URL}/pages"
    properties: dict = {
        "Sale Entry": {
            "title": [{"text": {"content": sale_entry}}],
        },
        "Date": {"date": {"start": today}},
        "Customer Name": {
            "rich_text": [{"text": {"content": customer_name}}],
        },
        "Product Sold": {
            "multi_select": [{"name": product_sold}],
        },
        "Quantity": {"number": quantity},
        "Unit": {"select": {"name": unit}},
        "Unit Price (₱)": {"number": unit_price},
        "Total Amount (₱)": {"number": total},
        "Payment Method": {"select": {"name": payment_method}},
        "Payment Status": {"select": {"name": payment_status}},
        "Amount Received (₱)": {"number": amount_received},
        "Balance Due (₱)": {"number": balance_due},
        "Transaction Type": {"select": {"name": transaction_type}},
        "Handled By": {"select": {"name": handled_by}},
    }

    if customer_contact:
        properties["Customer Contact"] = {"phone_number": customer_contact}
    if notes:
        properties["Notes"] = {
            "rich_text": [{"text": {"content": notes}}],
        }

    payload = {
        "parent": {"database_id": NOTION_SALES_DB_ID},
        "properties": properties,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=NOTION_HEADERS, json=payload)
        resp.raise_for_status()
    return True


async def get_unprocessed_sales() -> list[dict]:
    """Fetch all sales from the FF Sales Log that haven't been processed yet."""
    url = f"{NOTION_BASE_URL}/databases/{FF_SALES_LOG_DB_ID}/query"
    payload = {
        "filter": {
            "property": "Processed",
            "checkbox": {"equals": False},
        }
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=NOTION_HEADERS, json=payload)
        resp.raise_for_status()
        data = resp.json()

    sales = []
    for page in data.get("results", []):
        props = page["properties"]
        sales.append({
            "id": page["id"],
            "buyer": _get_rich_text(props, "Buyer Name"),
            "category": _get_select(props, "Category"),
            "color": _get_rich_text(props, "Color/Variant"),
            "date": _get_date(props, "Date"),
            "quantity": _get_number(props, "Quantity"),
            "price_per_unit": _get_number(props, "Price per Unit"),
            "installation_fee": _get_number(props, "Installation Fee"),
            "salesperson": _get_select(props, "Salesperson"),
            "payment_method": _get_select(props, "Payment Method"),
            "payment_status": _get_select(props, "Payment Status"),
            "invoice": _get_rich_text(props, "Invoice #"),
            "unit": _get_select(props, "Unit"),
        })
    return sales


async def find_inventory_product(category: str, color: str) -> dict | None:
    """Find the best matching inventory product by category and color word overlap."""
    url = f"{NOTION_BASE_URL}/databases/{NOTION_DATABASE_ID}/query"
    payload = {
        "filter": {
            "property": "Category",
            "select": {"equals": category},
        }
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=NOTION_HEADERS, json=payload)
        resp.raise_for_status()
        data = resp.json()

    target_words = set(color.lower().replace("(", "").replace(")", "").split())
    best_match = None
    best_score = 0

    for page in data.get("results", []):
        props = page["properties"]
        product_color = _get_rich_text(props, "Color/Attribute")
        product_words = set(product_color.lower().replace("(", "").replace(")", "").split())
        score = len(target_words & product_words)
        if score > best_score:
            best_score = score
            best_match = {
                "id": page["id"],
                "name": _get_title(props, "Product Name"),
                "color": product_color,
                "stock": _get_number(props, "Stock"),
                "category": _get_select(props, "Category"),
            }

    return best_match if best_score > 0 else None


async def mark_sale_processed(page_id: str) -> bool:
    """Mark a sale in the FF Sales Log as processed."""
    url = f"{NOTION_BASE_URL}/pages/{page_id}"
    payload = {
        "properties": {
            "Processed": {"checkbox": True},
        }
    }
    async with httpx.AsyncClient() as client:
        resp = await client.patch(url, headers=NOTION_HEADERS, json=payload)
        resp.raise_for_status()
    return True


# --- Notion property helpers ---

def _get_title(props: dict, key: str) -> str:
    try:
        return props[key]["title"][0]["plain_text"]
    except (KeyError, IndexError):
        return ""


def _get_rich_text(props: dict, key: str) -> str:
    try:
        return props[key]["rich_text"][0]["plain_text"]
    except (KeyError, IndexError):
        return ""


def _get_number(props: dict, key: str) -> float | None:
    try:
        return props[key]["number"]
    except KeyError:
        return None


def _get_select(props: dict, key: str) -> str:
    try:
        return props[key]["select"]["name"]
    except (KeyError, TypeError):
        return ""


def _get_date(props: dict, key: str) -> str:
    try:
        return props[key]["date"]["start"]
    except (KeyError, TypeError):
        return ""
