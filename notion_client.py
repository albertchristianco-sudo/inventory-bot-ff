import os
import httpx

NOTION_API_KEY = os.getenv("NOTION_API_KEY")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
NOTION_SALES_DB_ID = os.getenv("NOTION_SALES_DB_ID")
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
            "category": _get_select(props, "Category"),
            "variant": _get_rich_text(props, "Color / Variant"),
            "stock": _get_number(props, "Stock"),
            "unit": _get_select(props, "Unit"),
            "price": _get_number(props, "Unit Price (₱)"),
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


async def update_price(page_id: str, new_price: float) -> bool:
    """Update the price for a product."""
    url = f"{NOTION_BASE_URL}/pages/{page_id}"
    payload = {
        "properties": {
            "Unit Price (₱)": {"number": new_price},
        }
    }
    async with httpx.AsyncClient() as client:
        resp = await client.patch(url, headers=NOTION_HEADERS, json=payload)
        resp.raise_for_status()
    return True


async def log_sale(
    product_name: str,
    quantity: int,
    unit_price: float,
    sold_by: str,
) -> bool:
    """Log a sale transaction to the Sales Log database."""
    from datetime import date

    url = f"{NOTION_BASE_URL}/pages"
    payload = {
        "parent": {"database_id": NOTION_SALES_DB_ID},
        "properties": {
            "Product": {
                "title": [{"text": {"content": product_name}}],
            },
            "Quantity": {"number": quantity},
            "Unit Price (₱)": {"number": unit_price},
            "Total (₱)": {"number": quantity * unit_price},
            "Sold By": {
                "rich_text": [{"text": {"content": sold_by}}],
            },
            "Date": {
                "date": {"start": date.today().isoformat()},
            },
        },
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=NOTION_HEADERS, json=payload)
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
