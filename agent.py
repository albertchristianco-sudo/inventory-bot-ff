import os
import json
import time
import asyncio
import anthropic
import notion_client as notion

MODEL = "claude-sonnet-4-6"
CONVERSATION_TTL = 30 * 60  # 30 minutes — conversations expire after this
MAX_HISTORY = 20  # max message pairs to keep per conversation

SYSTEM_PROMPT = """You are the Flame & Finish sales assistant bot. You help staff log daily sales transactions and check inventory through WhatsApp.

## YOUR ROLE
- Log sales into the Daily Sales Ledger (Notion)
- Check product stock levels from FF Inventory (Notion)
- Update prices when the owner tells you to
- Confirm entries back to the staff member
- Keep responses SHORT and conversational — this is WhatsApp, not email

## RULES
1. NEVER guess stock numbers. Always use lookup_products first.
2. NEVER save a sale without confirmation from the user.
3. If a field is unclear, ask — don't guess.
4. If a product isn't found, say: "I can't find that product. Check the item code or spelling."
5. Keep all messages under 5 lines when possible.
6. Use ₱ symbol for all peso amounts.
7. Default to English. If staff writes in Cebuano/Bisaya, respond in Bisaya.
8. Understand Cebuano, Tagalog, and English input.

## LANGUAGE — KEY BISAYA PHRASES
- "pila pa" = how much stock left
- "benta" / "nabaligya" = sale / sold
- "bayad" = payment
- "taod-taod" = later / on terms

## LOGGING A SALE
When a user reports a sale, gather ALL required info (ask in one message if possible):
1. Customer name
2. Product sold (SPC Flooring, WPC Wall Panels, Outdoor Decking, Fencing, Interior Finishes, Other)
3. Quantity + unit (boxes, sqm, pieces, sets)
4. Unit price (₱)
5. Payment method (Cash / GCash / PayMaya / Bank Transfer / Check / Terms)
6. Paid in full, partial, or unpaid?
7. Walk-in, Delivery, Pick-up, or Online?
8. Handled by (Ac, Staff 1, Staff 2, Staff 3)

Before saving, confirm with a summary:
📋 Customer: [name]
📦 Product: [product] — [qty] [unit]
💰 Total: ₱[total] ([unit price] × [qty])
💳 Payment: [method] — [status]
🚚 Type: [transaction type]
👤 Handled by: [staff]

Save this? Reply YES to confirm or send corrections.

Then: (1) lookup product, (2) update stock, (3) log the sale. Always do all three steps.
Confirm old stock, deduction, and new stock on every sale.

## CHECKING STOCK
When a user asks about stock, respond with:
📦 [Product Name]
Code: [FF Item Code]
Stock: [Stock] units / [Stock (Boxes)] boxes
Price: ₱[Unit Price] | Floor: ₱[Min Sellable]

If stock is 0 or ≤5: ⚠️ [Product Name] — LOW STOCK ([n] remaining)

## PRICING QUERIES
When a user asks about pricing, always look up the product first, then show:
[Product Name] — Pricing Guide
⚠️ Min Sellable: ₱XX.XX
📊 SRP: ₱XX.XX (1.5x + VAT)
💰 Selling Price: ₱XX.XX

If a user asks "lowest price" or "floor price", emphasize Min Sellable is the lowest they should sell at.

## PRICING FIELDS (for update_price tool)
- **unit_price** — Unit Price (₱): current selling price
- **landed_cost** — Landed Cost (₱): actual cost after import (absolute floor)
- **min_sellable** — Min Sellable (Floor): minimum selling price
- **srp_1_5x** — SRP @ 1.5x + VAT (₱): suggested retail price
- **srp_2_0x** — SRP @ 2.0x + VAT (₱): standard selling price
- **srp_3_0x** — SRP @ 3.0x + VAT (₱): premium selling price
- **usd_per_pc** — USD/pc (Ex Works): original supplier price in USD"""

TOOLS = [
    {
        "name": "lookup_products",
        "description": "Search the inventory database for products. Use a search term to filter by product name, or leave empty to get all products.",
        "input_schema": {
            "type": "object",
            "properties": {
                "search_term": {
                    "type": "string",
                    "description": "Product name or keyword to search for (e.g. 'oak', 'SPC', 'walnut'). Leave empty for all products.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "update_stock",
        "description": "Update the stock quantity of a product after confirming the product ID and new stock value.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "The Notion page ID of the product to update.",
                },
                "new_stock": {
                    "type": "integer",
                    "description": "The new stock quantity after the adjustment.",
                },
            },
            "required": ["page_id", "new_stock"],
        },
    },
    {
        "name": "update_price",
        "description": "Update any pricing field for a product. Can set unit price, landed cost, min sellable, SRP tiers, or USD cost.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "The Notion page ID of the product to update.",
                },
                "new_price": {
                    "type": "number",
                    "description": "The new price value.",
                },
                "field": {
                    "type": "string",
                    "description": "Which pricing field to update.",
                    "enum": [
                        "unit_price",
                        "landed_cost",
                        "min_sellable",
                        "srp_1_5x",
                        "srp_2_0x",
                        "srp_3_0x",
                        "usd_per_pc",
                    ],
                },
            },
            "required": ["page_id", "new_price", "field"],
        },
    },
    {
        "name": "log_sale",
        "description": "Log a sale transaction to the Daily Sales Ledger. Call this AFTER updating stock. Gather all required fields from the user before calling.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {
                    "type": "string",
                    "description": "Customer name.",
                },
                "product_sold": {
                    "type": "string",
                    "description": "Product category sold.",
                    "enum": ["SPC Flooring", "WPC Wall Panels", "Outdoor Decking", "Fencing", "Interior Finishes", "Other"],
                },
                "quantity": {
                    "type": "integer",
                    "description": "Number of units sold.",
                },
                "unit": {
                    "type": "string",
                    "description": "Unit of measurement.",
                    "enum": ["boxes", "sqm", "pieces", "sets"],
                },
                "unit_price": {
                    "type": "number",
                    "description": "Price per unit in Philippine Pesos.",
                },
                "payment_method": {
                    "type": "string",
                    "description": "How the customer paid.",
                    "enum": ["Cash", "Bank Transfer", "GCash", "PayMaya", "Check", "Terms / Credit"],
                },
                "payment_status": {
                    "type": "string",
                    "description": "Payment status.",
                    "enum": ["Paid", "Partial", "Unpaid"],
                },
                "transaction_type": {
                    "type": "string",
                    "description": "Type of transaction.",
                    "enum": ["Walk-in", "Delivery", "Pick-up", "Online Order"],
                },
                "handled_by": {
                    "type": "string",
                    "description": "Staff member who handled the sale.",
                    "enum": ["Ac", "Staff 1", "Staff 2", "Staff 3"],
                },
                "customer_contact": {
                    "type": "string",
                    "description": "Customer phone number (optional).",
                },
                "amount_received": {
                    "type": "number",
                    "description": "Amount actually received. Required if payment is Partial or Unpaid.",
                },
                "notes": {
                    "type": "string",
                    "description": "Optional notes — delivery address, remarks, etc.",
                },
            },
            "required": ["customer_name", "product_sold", "quantity", "unit", "unit_price", "payment_method", "payment_status", "transaction_type", "handled_by"],
        },
    },
]

# In-memory conversation store: {phone_number: {"messages": [...], "last_active": timestamp}}
_conversations: dict[str, dict] = {}

# Per-sender locks to prevent concurrent processing of messages from the same user
_sender_locks: dict[str, asyncio.Lock] = {}


def _get_conversation(sender: str) -> list[dict]:
    """Get or create conversation history for a sender. Expires after TTL."""
    now = time.time()
    convo = _conversations.get(sender)

    if convo and (now - convo["last_active"]) < CONVERSATION_TTL:
        return convo["messages"]

    # Expired or new — start fresh
    _conversations[sender] = {"messages": [], "last_active": now}
    return _conversations[sender]["messages"]


def _trim_conversation(sender: str):
    """Keep conversation history within limits without breaking tool call pairs."""
    convo = _conversations.get(sender)
    if not convo:
        return
    msgs = convo["messages"]
    # Trim from the front, but never leave an orphaned tool_result without
    # its matching tool_use in the previous assistant message.
    while len(msgs) > MAX_HISTORY:
        msgs.pop(0)
    # After trimming, ensure the first message isn't a tool_result (role=user
    # with tool_result content).  If it is, keep popping until we reach a clean
    # user text message.
    while msgs and _is_tool_result_message(msgs[0]):
        msgs.pop(0)
    # Also ensure we don't start with an assistant message (Claude API requires
    # conversations to start with a user message).
    while msgs and msgs[0].get("role") == "assistant":
        msgs.pop(0)
    convo["last_active"] = time.time()


def _is_tool_result_message(msg: dict) -> bool:
    """Check if a message is a user message containing tool_result blocks."""
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if isinstance(content, list):
        return any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        )
    return False


async def handle_message(user_message: str, sender: str = "default") -> str:
    """Process a WhatsApp message through Claude and return the response."""
    # Serialize messages per sender so concurrent webhooks don't corrupt history
    if sender not in _sender_locks:
        _sender_locks[sender] = asyncio.Lock()
    async with _sender_locks[sender]:
        return await _handle_message_inner(user_message, sender)


async def _handle_message_inner(user_message: str, sender: str) -> str:
    """Inner handler — runs under per-sender lock."""
    client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", "").strip())

    messages = _get_conversation(sender)

    # Snapshot message count so we can rollback on failure
    snapshot_len = len(messages)

    messages.append({"role": "user", "content": user_message})

    try:
        # Agentic loop: keep going until Claude produces a final text response
        while True:
            response = await client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            # If Claude wants to use tools, execute them and continue the loop
            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})

                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = await _execute_tool(block.name, block.input, sender)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, ensure_ascii=False),
                        })

                messages.append({"role": "user", "content": tool_results})
                continue

            # Extract final text response and save to history
            text_parts = [block.text for block in response.content if block.type == "text"]
            reply = "\n".join(text_parts) if text_parts else "Sorry, I couldn't process that."

            messages.append({"role": "assistant", "content": reply})
            _trim_conversation(sender)

            return reply

    except Exception:
        # Rollback conversation to pre-request state so a failed tool loop
        # doesn't leave orphaned tool_use/tool_result messages in history.
        del messages[snapshot_len:]
        raise


async def _execute_tool(name: str, inputs: dict, sender: str = "default") -> dict:
    """Route tool calls to the appropriate Notion client function."""
    try:
        if name == "lookup_products":
            products = await notion.query_products(inputs.get("search_term", ""))
            return {"products": products}

        elif name == "update_stock":
            await notion.update_stock(inputs["page_id"], inputs["new_stock"])
            return {"success": True}

        elif name == "update_price":
            await notion.update_price(inputs["page_id"], inputs["new_price"], inputs.get("field", "unit_price"))
            return {"success": True}

        elif name == "log_sale":
            await notion.log_sale(
                customer_name=inputs["customer_name"],
                product_sold=inputs["product_sold"],
                quantity=inputs["quantity"],
                unit=inputs["unit"],
                unit_price=inputs["unit_price"],
                payment_method=inputs["payment_method"],
                payment_status=inputs["payment_status"],
                transaction_type=inputs["transaction_type"],
                handled_by=inputs["handled_by"],
                customer_contact=inputs.get("customer_contact", ""),
                amount_received=inputs.get("amount_received"),
                notes=inputs.get("notes", ""),
            )
            return {"success": True}

        else:
            return {"error": f"Unknown tool: {name}"}

    except Exception as e:
        return {"error": str(e)}
