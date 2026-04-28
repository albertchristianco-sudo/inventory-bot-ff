import os
import time
import logging
import httpx
from collections import OrderedDict
from dotenv import load_dotenv

load_dotenv(override=True)

from fastapi import BackgroundTasks, FastAPI, Header, Request, Response

import agent
import notion_client as notion

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Deduplication: track recently processed Telegram update_ids to ignore retries.
_seen_update_ids: OrderedDict[int, float] = OrderedDict()
_DEDUP_TTL = 60  # seconds to remember an update_id
_DEDUP_MAX = 200  # max entries to keep

# Pending approvals: sales that the daily report couldn't auto-match with high
# confidence. Owner approves/skips via Telegram commands. Keyed by invoice
# number (uppercased). In-memory — lost on Railway redeploy, but the underlying
# sales rows stay unprocessed in Notion so they'll show up in the next report.
_pending_approvals: dict[str, dict] = {}
_PENDING_TTL = 24 * 60 * 60  # 24 hours

# Railway env vars can have trailing whitespace/newlines — strip everything
# we inject into HTTP headers or URLs.
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()  # daily report destination
TELEGRAM_WEBHOOK_SECRET = (os.getenv("TELEGRAM_WEBHOOK_SECRET") or "").strip()

# Owner-only bot: only the owner can chat with the bot. Staff update Notion
# directly (FF Sales Log) and the 6PM cron processes their entries.
# Defaults to TELEGRAM_CHAT_ID — for a private DM, chat_id == user_id.
OWNER_TELEGRAM_ID = (os.getenv("OWNER_TELEGRAM_ID") or TELEGRAM_CHAT_ID or "").strip()

# Hermes shared secret — required header on POST /run-daily-report when set.
# The 6PM Mon-Sat cron is owned by an external Hermes agent that hits this
# endpoint with X-Hermes-Secret. Leave unset to disable the check (e.g. for
# local development).
HERMES_SECRET = (os.getenv("HERMES_SECRET") or "").strip()


app = FastAPI(title="Flame & Finish Inventory Bot")


@app.get("/")
async def health():
    return {"status": "ok", "service": "Flame & Finish Inventory Bot (Telegram)"}


# --- Telegram helpers ---

async def _tg_call(method: str, payload: dict) -> dict:
    """Call the Telegram Bot API and return the parsed JSON response."""
    if not TELEGRAM_BOT_TOKEN:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not set"}
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload)
        try:
            data = resp.json()
        except Exception:
            data = {"ok": False, "http_status": resp.status_code, "error": f"non-JSON: {resp.text[:300]}"}
    except Exception as e:
        data = {"ok": False, "error": str(e)}
    if not data.get("ok"):
        logger.error(f"Telegram {method} failed: {data}")
    return data


async def _send_telegram_to(chat_id, text: str) -> bool:
    """Send a message to a specific Telegram chat."""
    data = await _tg_call("sendMessage", {"chat_id": chat_id, "text": text})
    return data.get("ok", False)


async def _send_telegram(text: str) -> bool:
    """Send to the default daily-report chat (TELEGRAM_CHAT_ID)."""
    if not TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_CHAT_ID not set — cannot send daily report")
        return False
    return await _send_telegram_to(TELEGRAM_CHAT_ID, text)


async def _send_telegram_alert(text: str) -> bool:
    """Send a clarification alert — goes to the same chat as the daily report
    (owner's DM), since this is an owner-only bot."""
    if not TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_CHAT_ID not set — skipping alert")
        return False
    return await _send_telegram_to(TELEGRAM_CHAT_ID, text)


# --- Telegram webhook ---

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive incoming Telegram messages."""
    # Validate secret token if configured (recommended for production)
    if TELEGRAM_WEBHOOK_SECRET:
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if token != TELEGRAM_WEBHOOK_SECRET:
            logger.warning("Invalid Telegram webhook secret")
            return Response(status_code=403)

    try:
        update = await request.json()
    except Exception:
        logger.warning("Telegram webhook: non-JSON body")
        return {"ok": True}

    # Deduplicate by update_id — Telegram retries if it doesn't get a 200 back fast
    update_id = update.get("update_id")
    if isinstance(update_id, int):
        now = time.time()
        if update_id in _seen_update_ids:
            logger.info(f"Duplicate Telegram update ignored: update_id={update_id}")
            return {"ok": True}
        _seen_update_ids[update_id] = now
        while _seen_update_ids and (
            len(_seen_update_ids) > _DEDUP_MAX
            or next(iter(_seen_update_ids.values())) < now - _DEDUP_TTL
        ):
            _seen_update_ids.popitem(last=False)

    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"ok": True}  # ignore non-message updates (e.g. reactions, joins)

    chat = message.get("chat") or {}
    from_user = message.get("from") or {}
    chat_id = chat.get("id")
    user_id = from_user.get("id")
    text = (message.get("text") or "").strip()

    if not text or chat_id is None or user_id is None:
        return {"ok": True}

    sender_id = str(user_id)

    # Authorization — owner-only bot. Staff update Notion directly.
    if not OWNER_TELEGRAM_ID:
        logger.error("OWNER_TELEGRAM_ID not configured — refusing all messages")
        return {"ok": True}
    if sender_id != str(OWNER_TELEGRAM_ID):
        username = from_user.get("username") or from_user.get("first_name") or "?"
        logger.warning(f"Unauthorized Telegram user_id={sender_id} username={username}")
        await _send_telegram_to(
            chat_id,
            "⚠️ This bot is private. If you need inventory info, contact the owner.",
        )
        return {"ok": True}

    # Queue background processing so Telegram gets a fast 200 back
    background_tasks.add_task(_process_and_reply, text, f"telegram:{sender_id}", chat_id)
    return {"ok": True}


async def _process_and_reply(body: str, sender: str, chat_id):
    """Background task: handle approval commands directly, otherwise route to Claude."""
    text = body.strip()
    text_lower = text.lower()

    # Approval commands — handled here, not by the LLM, for predictability
    if text_lower.startswith("approve "):
        await _handle_approval(text[len("approve "):], chat_id)
        return
    if text_lower.startswith("skip "):
        await _handle_skip(text[len("skip "):], chat_id)
        return
    if text_lower in ("pending", "/pending"):
        await _send_telegram_to(chat_id, _format_pending_list())
        return

    # Everything else: route through Claude
    try:
        reply = await agent.handle_message(body, sender=sender)
    except Exception as e:
        logger.error(f"Agent error: {e}", exc_info=True)
        reply = "Sorry, something went wrong processing your message. Try again in a bit!"

    await _send_telegram_to(chat_id, reply)


# --- Telegram webhook management ---

@app.post("/telegram-setup-webhook")
async def setup_telegram_webhook(request: Request):
    """One-shot setup: register this service's webhook with Telegram.

    Example:
      curl -X POST https://<your-url>/telegram-setup-webhook \\
           -H 'content-type: application/json' \\
           -d '{"url": "https://<your-url>/telegram-webhook"}'
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    url = body.get("url")
    if not url:
        return {
            "ok": False,
            "error": "Provide JSON body: {\"url\": \"https://<your-railway-url>/telegram-webhook\"}",
        }
    payload = {"url": url, "drop_pending_updates": True}
    if TELEGRAM_WEBHOOK_SECRET:
        payload["secret_token"] = TELEGRAM_WEBHOOK_SECRET
    return await _tg_call("setWebhook", payload)


@app.get("/telegram-webhook-info")
async def telegram_webhook_info():
    """Diagnostic: show what Telegram thinks the registered webhook is."""
    return await _tg_call("getWebhookInfo", {})


# --- Pending approvals ---

def _cleanup_pending():
    """Drop entries older than _PENDING_TTL."""
    now = time.time()
    expired = [k for k, v in _pending_approvals.items() if v["expires_at"] < now]
    for k in expired:
        del _pending_approvals[k]


def _invoice_key(sale: dict) -> str:
    """Stable key for a pending sale. Uses invoice number if present, else page id."""
    inv = (sale.get("invoice") or "").strip().upper()
    return inv or f"SALE:{sale['id'][:8]}"


async def _send_approval_request(invoice: str) -> None:
    """Send a Telegram message asking the owner to approve or skip a pending sale."""
    pending = _pending_approvals.get(invoice)
    if not pending:
        return
    sale = pending["sale"]
    guess = pending["best_guess"]
    qty = int(sale.get("quantity") or 0)
    price = sale.get("price_per_unit") or 0.0
    unit = sale.get("unit") or "pcs"

    lines = [
        f"🤔 Need approval — {invoice}",
        "",
        "📋 SALE",
        f"• {qty} {unit} {sale.get('category') or '—'} ({sale.get('color') or '—'}) @ ₱{price:,.0f}",
        f"• {sale.get('salesperson') or '—'} → {sale.get('buyer') or '—'}",
        f"• {sale.get('payment_method') or '—'} — {sale.get('payment_status') or '—'}",
        "",
    ]

    if guess:
        stock = guess.get("stock")
        new_stock = max(0, int(stock) - qty) if stock is not None else None
        confidence = guess.get("confidence", "?")
        lines += [
            f"🎯 BEST GUESS ({confidence} match — {guess.get('match_reason', '')})",
            f"• {guess.get('name') or '—'}",
        ]
        if guess.get("item_code"):
            lines.append(f"• Code: {guess['item_code']}")
        if stock is not None:
            lines.append(f"• Stock: {int(stock)} → {new_stock} (-{qty})")
        else:
            lines.append("• Stock: not set in Notion")
        lines += [
            "",
            "Reply:",
            f"• approve {invoice} — deduct from best guess",
            f"• skip {invoice} — mark processed, no deduction",
            "• Or tell me the correct product",
        ]
    else:
        lines += [
            "⚠️ No inventory match found.",
            "",
            "Reply:",
            f"• skip {invoice} — mark processed, no deduction",
            "• Or tell me the correct product",
        ]

    await _send_telegram_alert("\n".join(lines))


async def _handle_approval(invoice_raw: str, chat_id) -> None:
    """Owner replied 'approve <invoice>' — deduct from best guess and mark processed."""
    invoice = invoice_raw.strip().upper()
    _cleanup_pending()
    pending = _pending_approvals.get(invoice)
    if not pending:
        await _send_telegram_to(chat_id, f"⚠️ No pending sale for {invoice}. Send 'pending' to see what's waiting.")
        return

    sale = pending["sale"]
    guess = pending["best_guess"]
    if not guess:
        await _send_telegram_to(
            chat_id,
            f"⚠️ {invoice} has no best guess to approve. Reply 'skip {invoice}' or tell me the right product.",
        )
        return
    if guess.get("stock") is None:
        await _send_telegram_to(
            chat_id,
            f"⚠️ {guess.get('name')} has no Stock value in Notion. Set it first, then re-run the report.",
        )
        return

    qty = int(sale.get("quantity") or 0)
    new_stock = max(0, int(guess["stock"]) - qty)
    try:
        await notion.update_stock(guess["id"], new_stock)
        await notion.mark_sale_processed(sale["id"])
    except Exception as e:
        logger.error(f"Approval {invoice} failed: {e}", exc_info=True)
        await _send_telegram_to(chat_id, f"❌ Failed to apply {invoice}: {e}")
        return

    del _pending_approvals[invoice]
    reply = f"✅ {invoice} approved\n• {guess['name']}: {int(guess['stock'])} → {new_stock} (-{qty})"
    if new_stock < 20:
        reply += f"\n⚠️ {new_stock} pcs left — REORDER NEEDED"
    await _send_telegram_to(chat_id, reply)


async def _handle_skip(invoice_raw: str, chat_id) -> None:
    """Owner replied 'skip <invoice>' — mark processed without deducting any stock."""
    invoice = invoice_raw.strip().upper()
    _cleanup_pending()
    pending = _pending_approvals.get(invoice)
    if not pending:
        await _send_telegram_to(chat_id, f"⚠️ No pending sale for {invoice}.")
        return

    try:
        await notion.mark_sale_processed(pending["sale"]["id"])
    except Exception as e:
        logger.error(f"Skip {invoice} failed: {e}", exc_info=True)
        await _send_telegram_to(chat_id, f"❌ Failed to skip {invoice}: {e}")
        return

    del _pending_approvals[invoice]
    await _send_telegram_to(chat_id, f"✅ {invoice} skipped — marked processed, no stock deducted.")


def _format_pending_list() -> str:
    _cleanup_pending()
    if not _pending_approvals:
        return "No pending approvals."
    lines = [f"⏳ {len(_pending_approvals)} pending approval(s):"]
    for invoice, p in _pending_approvals.items():
        sale = p["sale"]
        guess = p["best_guess"]
        guess_str = f" → {guess['name']}" if guess else " → ❌ no match"
        lines.append(f"• {invoice}: {sale.get('category') or '—'} / {sale.get('color') or '—'}{guess_str}")
    return "\n".join(lines)


# --- Daily report ---

async def _run_daily_report() -> dict:
    from datetime import date
    today_str = date.today().strftime("%B %d, %Y")
    sales = await notion.get_unprocessed_sales()

    if not sales:
        await _send_telegram(f"🔥 Flame & Finish — Daily Sales Report\n📅 {today_str}\n\nNo sales logged today.")
        return {"processed": 0, "grand_total": 0}

    _cleanup_pending()

    lines = ["🔥 Flame & Finish — Daily Sales Report", f"📅 {today_str}", "", "💰 SALES SUMMARY"]
    total_revenue = 0.0
    total_installation = 0.0
    alerts = []
    pending_invoices = []
    processed_count = 0
    skipped_count = 0
    pending_count = 0

    for sale in sales:
        buyer = (sale["buyer"] or "").strip()
        qty = sale["quantity"] or 0
        price = sale["price_per_unit"] or 0.0
        fee = sale["installation_fee"] or 0.0

        if not buyer or buyer.upper() == "NO SALES" or qty == 0:
            await notion.mark_sale_processed(sale["id"])
            skipped_count += 1
            continue

        subtotal = qty * price
        total_revenue += subtotal
        total_installation += fee

        inv = None
        if sale["category"]:
            inv = await notion.find_inventory_product(sale["category"], sale["color"] or "")

        # Strong match: auto-deduct and mark processed.
        # Weak or no match: queue for owner approval and leave Processed=false.
        sale_status = ""
        if inv and inv.get("confidence") == "strong" and inv.get("stock") is not None:
            new_stock = max(0, int(inv["stock"]) - int(qty))
            await notion.update_stock(inv["id"], new_stock)
            await notion.mark_sale_processed(sale["id"])
            processed_count += 1
            if new_stock < 20:
                alerts.append(f"⚠️ {inv['name']} ({inv['color']}): {new_stock} pcs left — REORDER NEEDED")
            sale_status = f"✅ deducted from {inv['name']}"
        else:
            invoice_key = _invoice_key(sale)
            _pending_approvals[invoice_key] = {
                "sale": sale,
                "best_guess": inv,
                "expires_at": time.time() + _PENDING_TTL,
            }
            pending_invoices.append(invoice_key)
            pending_count += 1
            if inv:
                sale_status = f"🤔 needs approval — best guess: {inv['name']} ({inv.get('confidence')})"
            else:
                sale_status = "⚠️ no inventory match — needs your input"

        unit = sale["unit"] or "pcs"
        lines.append(f"• {sale['category']} ({sale['color']}) — {int(qty)} {unit} @ P{price:,.0f} = P{subtotal:,.0f}")
        lines.append(f"  👤 {sale['salesperson'] or '—'}  |  🏢 {buyer}")
        lines.append(f"  🧾 {sale['invoice'] or '—'}  |  💳 {sale['payment_method'] or '—'} — {sale['payment_status'] or '—'}")
        if fee:
            lines.append(f"  🔧 Installation: P{fee:,.0f}")
        lines.append(f"  {sale_status}")
        lines.append("")

    grand_total = total_revenue + total_installation
    lines += ["📊 TOTALS", f"• Total Revenue: P{total_revenue:,.0f}", f"• Installation Fees: P{total_installation:,.0f}", f"• Grand Total: P{grand_total:,.0f}", ""]

    if alerts:
        lines.append("📦 INVENTORY ALERTS")
        lines += alerts
        lines.append("")

    summary = f"✅ {processed_count} sale(s) auto-processed."
    if pending_count:
        summary += f" 🤔 {pending_count} need your approval (see follow-ups below)."
    if skipped_count:
        summary += f" {skipped_count} placeholder(s) cleared."
    lines.append(summary)

    await _send_telegram("\n".join(lines))

    # Send one approval-request message per pending sale so each is independently actionable
    for invoice in pending_invoices:
        await _send_approval_request(invoice)

    logger.info(
        f"Daily report sent — auto={processed_count}, pending={pending_count}, "
        f"grand total P{grand_total:,.0f}"
    )
    return {"processed": processed_count, "pending": pending_count, "grand_total": grand_total}


@app.post("/run-daily-report")
async def daily_report_endpoint(
    x_hermes_secret: str | None = Header(default=None, alias="X-Hermes-Secret"),
):
    """Trigger the daily sales report. Owned by Hermes (external cron) when
    HERMES_SECRET is configured — Hermes hits this endpoint at 18:00 Asia/Manila
    Mon-Sat with X-Hermes-Secret. Manual triggers from the owner's terminal
    must include the same header (e.g. -H 'X-Hermes-Secret: <value>').
    """
    if HERMES_SECRET and x_hermes_secret != HERMES_SECRET:
        logger.warning("run-daily-report: missing/invalid X-Hermes-Secret")
        return Response(status_code=403)
    result = await _run_daily_report()
    return result


# --- Diagnostics ---

@app.post("/test-telegram")
async def test_telegram():
    """Diagnostic — sends a test message via Telegram and returns the raw API response."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {
            "configured": False,
            "error": "TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID not set",
            "bot_token_set": bool(TELEGRAM_BOT_TOKEN),
            "chat_id_set": bool(TELEGRAM_CHAT_ID),
        }
    data = await _tg_call("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": "🧪 Test from Flame & Finish bot — if you see this, Telegram delivery works.",
    })
    return {"configured": True, "response": data}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
