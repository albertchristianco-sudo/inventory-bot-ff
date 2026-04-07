import os
import logging
import httpx
from dotenv import load_dotenv

load_dotenv(override=True)

from fastapi import BackgroundTasks, FastAPI, Request, Response
from twilio.rest import Client as TwilioClient
from twilio.request_validator import RequestValidator

import agent
import notion_client as notion

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Flame & Finish Inventory Bot")

TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Authorized team numbers — comma-separated in .env
_allowed_raw = os.getenv("ALLOWED_NUMBERS", "")
ALLOWED_NUMBERS = {n.strip() for n in _allowed_raw.split(",") if n.strip()}


def _get_twilio_client() -> TwilioClient:
    """Lazy-init Twilio client so the server can start without credentials."""
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set")
    return TwilioClient(sid, token)


def _validate_twilio_signature(url: str, params: dict, signature: str) -> bool:
    """Validate that the request actually came from Twilio."""
    token = os.getenv("TWILIO_AUTH_TOKEN")
    if not token:
        return False
    validator = RequestValidator(token)
    return validator.validate(url, params, signature)


@app.get("/")
async def health():
    return {"status": "ok", "service": "Flame & Finish Inventory Bot"}


@app.post("/webhook")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive incoming WhatsApp messages from Twilio."""
    form_data = await request.form()
    params = dict(form_data)

    From = params.get("From", "")
    Body = params.get("Body", "")

    # Validate Twilio signature (skip in dev — ngrok changes the URL which breaks validation)
    if os.getenv("VALIDATE_TWILIO_SIGNATURE", "false").lower() == "true":
        signature = request.headers.get("X-Twilio-Signature", "")
        url = str(request.url)
        if not _validate_twilio_signature(url, params, signature):
            logger.warning(f"Invalid Twilio signature from {From}")
            return Response(status_code=403)

    # Restrict to allowed team numbers
    if ALLOWED_NUMBERS and From not in ALLOWED_NUMBERS:
        logger.warning(f"Unauthorized number: {From}")
        return Response(status_code=403)

    # Process in background so Twilio doesn't time out (15s limit)
    # Return empty TwiML so Twilio knows not to send any message
    background_tasks.add_task(_process_and_reply, Body, From)

    return Response(
        content="<Response></Response>",
        media_type="text/xml",
    )


async def _process_and_reply(body: str, sender: str):
    """Background task: process message through Claude and send reply via Twilio."""
    try:
        reply = await agent.handle_message(body, sender=sender)
    except Exception as e:
        logger.error(f"Agent error: {e}", exc_info=True)
        reply = "Sorry, something went wrong processing your message. Try again in a bit!"

    try:
        msg = _get_twilio_client().messages.create(
            body=reply,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=sender,
        )
        logger.info(f"Reply sent - sid: {msg.sid}, status: {msg.status}")
    except Exception as e:
        logger.error(f"Twilio send error: {e}")


async def _send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    ok = resp.json().get("ok", False)
    if not ok:
        logger.error(f"Telegram send failed: {resp.text}")
    return ok


async def _run_daily_report() -> dict:
    from datetime import date
    today_str = date.today().strftime("%B %d, %Y")
    sales = await notion.get_unprocessed_sales()

    if not sales:
        await _send_telegram(f"🔥 Flame & Finish — Daily Sales Report\n📅 {today_str}\n\nNo sales logged today.")
        return {"processed": 0, "grand_total": 0}

    lines = ["🔥 Flame & Finish — Daily Sales Report", f"📅 {today_str}", "", "💰 SALES SUMMARY"]
    total_revenue = 0.0
    total_installation = 0.0
    alerts = []
    processed_count = 0
    skipped_count = 0

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

        if inv and inv["stock"] is not None:
            new_stock = max(0, int(inv["stock"]) - int(qty))
            await notion.update_stock(inv["id"], new_stock)
            if new_stock < 20:
                alerts.append(f"⚠️ {inv['name']} ({inv['color']}): {new_stock} pcs left — REORDER NEEDED")
        else:
            alerts.append(f"⚠️ No inventory match for: {sale['category']} / {sale['color']} (check manually)")

        unit = sale["unit"] or "pcs"
        lines.append(f"• {sale['category']} ({sale['color']}) — {int(qty)} {unit} @ P{price:,.0f} = P{subtotal:,.0f}")
        lines.append(f"  👤 {sale['salesperson'] or '—'}  |  🏢 {buyer}")
        lines.append(f"  🧾 {sale['invoice'] or '—'}  |  💳 {sale['payment_method'] or '—'} — {sale['payment_status'] or '—'}")
        if fee:
            lines.append(f"  🔧 Installation: P{fee:,.0f}")
        lines.append("")

        await notion.mark_sale_processed(sale["id"])
        processed_count += 1

    grand_total = total_revenue + total_installation
    lines += ["📊 TOTALS", f"• Total Revenue: P{total_revenue:,.0f}", f"• Installation Fees: P{total_installation:,.0f}", f"• Grand Total: P{grand_total:,.0f}", ""]

    if alerts:
        lines.append("📦 INVENTORY ALERTS")
        lines += alerts
        lines.append("")

    summary = f"✅ {processed_count} sale(s) processed."
    if skipped_count:
        summary += f" {skipped_count} placeholder(s) cleared."
    lines.append(summary)

    await _send_telegram("\n".join(lines))
    logger.info(f"Daily report sent — {processed_count} sales, grand total P{grand_total:,.0f}")
    return {"processed": processed_count, "grand_total": grand_total}


@app.post("/run-daily-report")
async def daily_report_endpoint():
    result = await _run_daily_report()
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
