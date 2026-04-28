import os
import httpx

NOTION_API_KEY = os.getenv("NOTION_API_KEY")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
NOTION_SALES_DB_ID = os.getenv("NOTION_SALES_DB_ID")
FF_SALES_LOG_DB_ID = os.getenv("FF_SALES_LOG_DB_ID", "a3406a84-4be0-41d0-9593-8090cae4133c")
FF_SALES_ARCHIVE_DB_ID = os.getenv("FF_SALES_ARCHIVE_DB_ID")  # destination for weekly archive
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


def _normalize_words(text: str) -> set[str]:
    """Lowercase, drop punctuation, split on whitespace. Used for fuzzy match."""
    if not text:
        return set()
    cleaned = text.lower()
    for ch in "()[]{},.":
        cleaned = cleaned.replace(ch, " ")
    cleaned = cleaned.replace("/", " ").replace("-", " ").replace("_", " ")
    return {w for w in cleaned.split() if w}


async def find_inventory_product(category: str, color: str) -> dict | None:
    """Find the best matching inventory product. Tries multiple strategies in order:

    1. Strong: Category + Color/Attribute word overlap
    2. Strong: FF Item Code substring match anywhere in the input
    3. Weak: Category + Product Name word overlap
    4. Weak: Cross-category Product Name word overlap (last resort)

    Returns a dict with `confidence` ("strong" or "weak") and `match_reason`
    (human-readable explanation of why it matched), plus the usual product
    fields. Returns None only if nothing in the inventory matches at all.
    """
    target_words = _normalize_words(color)
    if not target_words:
        return None

    async def _query(payload: dict) -> list:
        url = f"{NOTION_BASE_URL}/databases/{NOTION_DATABASE_ID}/query"
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=NOTION_HEADERS, json=payload)
            resp.raise_for_status()
            return resp.json().get("results", [])

    def _build(page, confidence: str, reason: str) -> dict:
        props = page["properties"]
        return {
            "id": page["id"],
            "name": _get_title(props, "Product Name"),
            "color": _get_rich_text(props, "Color/Attribute"),
            "item_code": _get_rich_text(props, "FF Item Code"),
            "stock": _get_number(props, "Stock"),
            "category": _get_select(props, "Category"),
            "confidence": confidence,
            "match_reason": reason,
        }

    # Strategies 1 + 2 + 3: scoped to the sale's category if provided
    if category:
        results = await _query({
            "filter": {"property": "Category", "select": {"equals": category}}
        })

        # Strategy 1: Color/Attribute word overlap (strong)
        best_color = None
        best_color_score = 0
        for page in results:
            props = page["properties"]
            color_words = _normalize_words(_get_rich_text(props, "Color/Attribute"))
            score = len(target_words & color_words)
            if score > best_color_score:
                best_color_score = score
                best_color = page
        if best_color and best_color_score > 0:
            return _build(best_color, "strong", f"matched {best_color_score} color word(s)")

        # Strategy 2: FF Item Code substring (strong)
        # If any target word looks like a product code, check item_code containment
        for page in results:
            props = page["properties"]
            item_code = _get_rich_text(props, "FF Item Code").lower()
            if not item_code:
                continue
            for word in target_words:
                if len(word) >= 3 and (word in item_code or item_code in word):
                    return _build(page, "strong", f"item code match: {item_code}")

        # Strategy 3: Product Name word overlap within same category (weak)
        best_name = None
        best_name_score = 0
        for page in results:
            props = page["properties"]
            name_words = _normalize_words(_get_title(props, "Product Name"))
            score = len(target_words & name_words)
            if score > best_name_score:
                best_name_score = score
                best_name = page
        if best_name and best_name_score > 0:
            return _build(best_name, "weak", f"product name match in {category}")

    # Strategy 4: Cross-category Product Name word overlap (last resort, weak)
    all_results = await _query({})
    best_any = None
    best_any_score = 0
    for page in all_results:
        props = page["properties"]
        name_words = _normalize_words(_get_title(props, "Product Name"))
        color_words = _normalize_words(_get_rich_text(props, "Color/Attribute"))
        score = len(target_words & (name_words | color_words))
        if score > best_any_score:
            best_any_score = score
            best_any = page
    if best_any and best_any_score > 0:
        return _build(best_any, "weak", "cross-category product name match")

    return None


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


# --- Weekly archive: copy Processed sales from FF Sales Log → FF Sales Archive ---

def _property_for_create(prop: dict) -> dict | None:
    """Convert a Notion property from GET (read) format into the shape required
    for POST /pages. Returns None for read-only types (formula, rollup, etc.)
    so callers can drop them from the create payload."""
    ptype = prop.get("type")
    if ptype == "title":
        text = "".join(t.get("plain_text", "") for t in prop.get("title") or [])
        return {"title": [{"text": {"content": text}}]} if text else {"title": []}
    if ptype == "rich_text":
        text = "".join(t.get("plain_text", "") for t in prop.get("rich_text") or [])
        return {"rich_text": [{"text": {"content": text}}]} if text else {"rich_text": []}
    if ptype == "number":
        n = prop.get("number")
        return {"number": n} if n is not None else None
    if ptype == "select":
        sel = prop.get("select")
        return {"select": {"name": sel["name"]}} if sel else None
    if ptype == "multi_select":
        opts = prop.get("multi_select") or []
        return {"multi_select": [{"name": o["name"]} for o in opts]}
    if ptype == "date":
        d = prop.get("date")
        if not d or not d.get("start"):
            return None
        out = {"start": d["start"]}
        if d.get("end"):
            out["end"] = d["end"]
        return {"date": out}
    if ptype == "checkbox":
        return {"checkbox": prop.get("checkbox", False)}
    if ptype == "phone_number":
        return {"phone_number": prop.get("phone_number")}
    if ptype == "email":
        return {"email": prop.get("email")}
    if ptype == "url":
        return {"url": prop.get("url")}
    if ptype == "people":
        return {"people": [{"id": p["id"]} for p in prop.get("people") or [] if p.get("id")]}
    # Unsupported / read-only (formula, rollup, files, relation, status, etc.)
    return None


async def _get_processed_sales_raw() -> list[dict]:
    """Fetch full raw page objects (not flattened) for all Processed sales.
    We need the raw form so we can copy every property to the archive verbatim."""
    url = f"{NOTION_BASE_URL}/databases/{FF_SALES_LOG_DB_ID}/query"
    payload = {
        "filter": {"property": "Processed", "checkbox": {"equals": True}}
    }
    pages = []
    async with httpx.AsyncClient() as client:
        while True:
            resp = await client.post(url, headers=NOTION_HEADERS, json=payload)
            resp.raise_for_status()
            data = resp.json()
            pages.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            payload["start_cursor"] = data["next_cursor"]
    return pages


async def _create_archive_page(properties: dict) -> str:
    """Create a single row in FF_SALES_ARCHIVE_DB_ID. Returns the new page ID."""
    url = f"{NOTION_BASE_URL}/pages"
    payload = {
        "parent": {"database_id": FF_SALES_ARCHIVE_DB_ID},
        "properties": properties,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=NOTION_HEADERS, json=payload)
        resp.raise_for_status()
        return resp.json()["id"]


async def _soft_delete_page(page_id: str) -> bool:
    """Move a page to Notion's trash (recoverable for 30 days)."""
    url = f"{NOTION_BASE_URL}/pages/{page_id}"
    async with httpx.AsyncClient() as client:
        resp = await client.patch(url, headers=NOTION_HEADERS, json={"archived": True})
        resp.raise_for_status()
    return True


async def archive_processed_sales() -> dict:
    """Copy every Processed=true row from FF Sales Log to FF Sales Archive,
    then soft-delete the source row. Pending (Processed=false) sales are
    left in place. Returns a per-step count for the report."""
    if not FF_SALES_ARCHIVE_DB_ID:
        raise RuntimeError(
            "FF_SALES_ARCHIVE_DB_ID is not set — create the archive database in "
            "Notion, share it with the integration, and set this env var on Railway."
        )

    pages = await _get_processed_sales_raw()
    archived = 0
    failed = 0
    failures: list[str] = []

    for page in pages:
        try:
            new_props = {}
            for name, prop in (page.get("properties") or {}).items():
                converted = _property_for_create(prop)
                if converted is not None:
                    new_props[name] = converted
            await _create_archive_page(new_props)
            await _soft_delete_page(page["id"])
            archived += 1
        except Exception as e:
            failed += 1
            failures.append(f"{page['id'][:8]}: {e}")

    return {
        "candidates": len(pages),
        "archived": archived,
        "failed": failed,
        "failures": failures,
    }
