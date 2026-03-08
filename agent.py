import os
import json
import time
import asyncio
import anthropic
import notion_client as notion

MODEL = "claude-sonnet-4-6"
CONVERSATION_TTL = 30 * 60  # 30 minutes — conversations expire after this
MAX_HISTORY = 20  # max message pairs to keep per conversation

SYSTEM_PROMPT = """You are the inventory assistant for Flame & Finish Marketing Corp,
an import business in Cebu, Philippines that sells SPC flooring and WPC wall panels.

Your job:
- Answer stock and price queries by looking up the Notion inventory database.
- Record sales by deducting from stock when the owner reports a sale.
- Log every sale to the Sales Log database for transaction history.
- Update prices when the owner tells you to.

Rules:
- NEVER guess stock numbers. Always use the lookup tool first.
- Reply in concise, friendly English by default.
- Understand and accept messages in Cebuano, Tagalog, or English — but always respond in English.
- Always show the peso sign (₱) for prices.
- When processing a sale: (1) lookup the product, (2) update stock, (3) log the sale. Always do all three steps.
- When updating stock after a sale, confirm the old stock, the deduction, and the new stock.
- If a product isn't found, say so clearly and ask for clarification.
- Keep replies short — this is WhatsApp, not email.

## Keyword Alias Map
Users often use shorthand terms. Resolve them to the correct Notion database categories and item groups:

**Category-level aliases:**
- "SPC" → Category: SPC Flooring (covers floor tiles, reducers, skirting, T-moulding)
- "WPC" or "wall panel" → Category: Wall Panels (covers all fluted boards)

**Item Group aliases:**
- "SPC floor" or "SPC flooring" → Item Group: SPC Floor
- "reducer" → Item Group: SPC REDUCER
- "skirting" → Item Group: SPC SKIRTING LINE
- "t-moulding" or "t moulding" → Item Group: SPC T-MOULDING
- "4 grilles" or "grilles board" → Item Group: WPC Fluted Panel, Subcategory: Fluted Board (4 flutes)
- "solid small" or "small fluted" → Subcategory: Solid Small Fluted Board
- "arch fluted" → Subcategory: Arch Fluted Board
- "high fluted" → Subcategory: High Fluted Board
- "outdoor decking" or "decking" → Item Group: OUTDOOR WPC DECKING 1ST GEN
- "outdoor fluted" → Item Group: OUTDOOR FLUTED BOARD
- "keel" → Item Group: OUTDOOR DECKING ACCESSORY 1ST GEN KEEL
- "flexible tile" or "flex tile" → Item Group: FLEXIBLE TILE
- "UV panel" or "PVC panel" or "UV" → Item Group: UV PANEL / PVC PANEL
- "sound board" or "acoustic" → Item Group: SOUND ABSORPTION BOARD
- "bamboo charcoal" or "bamboo" → Item Group: WPC BAMBOO CHARCOAL BOARD

When searching, use the alias map to translate the user's shorthand into the correct search term for the lookup tool. If the user says a category-level alias (e.g. "SPC"), search broadly to return all matching item groups. If they use a specific item group alias (e.g. "reducer"), search for that specific item group.

## Pricing Fields in the Database
Every product in Notion has these pricing fields:
- **landed_cost** — Landed Cost (₱): actual cost after import. This is the absolute floor — we CANNOT sell below this.
- **min_sellable** — Min Sellable (Floor): minimum selling price, slightly above landed cost.
- **srp_1_5x** — SRP @ 1.5x + VAT (₱): minimum recommended retail price.
- **srp_2_0x** — SRP @ 2.0x + VAT (₱): standard selling price.
- **srp_3_0x** — SRP @ 3.0x + VAT (₱): premium selling price.
- **usd_per_pc** — USD/pc (Ex Works): original supplier price in USD.

## Pricing Queries
When a user asks about pricing (e.g. "how much should I sell SPC floor?", "what's the lowest price we can go?"):
- Always look up the product(s) first using lookup_products.
- Show the FULL pricing breakdown using this exact WhatsApp-friendly format:

[Product Name] — Pricing Guide
💰 Landed Cost: ₱XX.XX (your floor)
⚠️ Min Sellable: ₱XX.XX
📊 SRP Tiers:
  1.5x: ₱XX.XX (minimum retail)
  2.0x: ₱XX.XX (standard)
  3.0x: ₱XX.XX (premium)

- If a user asks "lowest price" or "floor price", emphasize that the Landed Cost is the absolute floor and the Min Sellable is the lowest they should actually sell at.
- If any pricing field is not available for a product, omit that line and note it's not set.
- When listing multiple products, use the same format for each."""

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
        "description": "Log a sale transaction to the Sales Log database. Call this AFTER updating stock to keep a record of every sale.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_name": {
                    "type": "string",
                    "description": "The product name (e.g. 'Oak SPC Flooring').",
                },
                "quantity": {
                    "type": "integer",
                    "description": "Number of units sold.",
                },
                "unit_price": {
                    "type": "number",
                    "description": "Price per unit in Philippine Pesos.",
                },
                "sold_by": {
                    "type": "string",
                    "description": "Name or phone number of the salesperson who reported the sale.",
                },
            },
            "required": ["product_name", "quantity", "unit_price", "sold_by"],
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
                product_name=inputs["product_name"],
                quantity=inputs["quantity"],
                unit_price=inputs["unit_price"],
                sold_by=inputs.get("sold_by", sender),
            )
            return {"success": True}

        else:
            return {"error": f"Unknown tool: {name}"}

    except Exception as e:
        return {"error": str(e)}
