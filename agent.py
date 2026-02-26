import os
import json
import time
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
- Keep replies short — this is WhatsApp, not email."""

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
        "description": "Update the price of a product.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "The Notion page ID of the product to update.",
                },
                "new_price": {
                    "type": "number",
                    "description": "The new price in Philippine Pesos.",
                },
            },
            "required": ["page_id", "new_price"],
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
    """Keep conversation history within limits."""
    convo = _conversations.get(sender)
    if not convo:
        return
    # Each exchange is roughly 2 messages (user + assistant), but tool calls add more.
    # Trim from the front to keep recent context.
    while len(convo["messages"]) > MAX_HISTORY:
        convo["messages"].pop(0)
    convo["last_active"] = time.time()


async def handle_message(user_message: str, sender: str = "default") -> str:
    """Process a WhatsApp message through Claude and return the response."""
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    messages = _get_conversation(sender)
    messages.append({"role": "user", "content": user_message})

    # Agentic loop: keep going until Claude produces a final text response
    while True:
        response = client.messages.create(
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
            await notion.update_price(inputs["page_id"], inputs["new_price"])
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
