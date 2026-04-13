import os
import time
import logging
import httpx
from collections import OrderedDict
from dotenv import load_dotenv

load_dotenv(override=True)

from fastapi import BackgroundTasks, FastAPI, Request, Response
from twilio.rest import Client as TwilioClient
from twilio.request_validator import RequestValidator

import agent
import notion_client as notion

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Deduplication: track recently processed Twilio MessageSids to ignore retries.
# Twilio can send the same webhook multiple times if it doesn't get a fast response.
_seen_message_sids: OrderedDict[str, float] = OrderedDict()
_DEDUP_TTL = 60  # seconds to remember a MessageSid
_DEDUP_MAX = 200  # max entries to keep

TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OWNER_WHATSAPP_NUMBER = os.getenv("OWNER_WHATSAPP_NUMBER")

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


# --- Scheduled daily report (6PM Mon-Sat, Philippine time UTC+8) ---
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

scheduler = AsyncIOScheduler()


async def _scheduled_daily_report():
    """Scheduler callback — runs the daily report and logs the outcome.
    AsyncIOScheduler runs coroutines natively on the event loop."""
    try:
        logger.info("Scheduler fired — running daily report")
        result = await _run_daily_report()
        logger.info(f"Scheduled daily report done: {result}")
    except Exception as e:
        logger.error(f"Scheduled daily report failed: {e}", exc_info=True)


scheduler.add_job(
    _scheduled_daily_report,
    CronTrigger(hour=18, minute=0, day_of_week="mon-sat", timezone="Asia/Manila"),
    id="daily_report",
    name="6PM Daily Sales Report (Mon-Sat)",
    replace_existing=True,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    scheduler.start()
    job = scheduler.get_job("daily_report")
    next_run = job.next_run_time if job else None
    logger.info(f"APScheduler started — next daily report at {next_run}")
    yield
    scheduler.shutdown()
    logger.info("APScheduler shut down")


app = FastAPI(title="Flame & Finish Inventory Bot", lifespan=lifespan)


@app.get("/")
async def health():
    return {"status": "ok", "service": "Flame & Finish Inventory Bot"}


EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


@app.post("/webhook")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive incoming WhatsApp messages from Twilio."""
    form_data = await request.form()
    params = dict(form_data)

    From = params.get("From", "")
    Body = params.get("Body", "")
    message_sid = params.get("MessageSid", "")

    # Deduplicate: Twilio may retry the webhook if our response is slow.
    # Ignore messages we've already seen.
    if message_sid:
        now = time.time()
        if message_sid in _seen_message_sids:
            logger.info(f"Duplicate webhook ignored: MessageSid={message_sid}")
            return Response(content=EMPTY_TWIML, media_type="text/xml")
        _seen_message_sids[message_sid] = now
        # Evict old entries
        while _seen_message_sids and (
            len(_seen_message_sids) > _DEDUP_MAX
            or next(iter(_seen_message_sids.values())) < now - _DEDUP_TTL
        ):
            _seen_message_sids.popitem(last=False)

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
    # Return empty TwiML immediately, then send reply via REST API when ready
    background_tasks.add_task(_process_and_reply, Body, From)

    return Response(content=EMPTY_TWIML, media_type="text/xml")


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


async def _send_whatsapp_alert(text: str) -> bool:
    """Send a WhatsApp message to the owner for clarification on mismatched sales."""
    if not OWNER_WHATSAPP_NUMBER or not TWILIO_WHATSAPP_NUMBER:
        logger.warning("Owner WhatsApp not configured — skipping alert")
        return False
    try:
        msg = _get_twilio_client().messages.create(
            body=text,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=OWNER_WHATSAPP_NUMBER,
        )
        logger.info(f"WhatsApp alert sent to owner - sid: {msg.sid}")
        return True
    except Exception as e:
        logger.error(f"WhatsApp alert failed: {e}")
        return False


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
            alert_msg = f"⚠️ No inventory match for: {sale['category']} / {sale['color']} (check manually)"
            alerts.append(alert_msg)
            await _send_whatsapp_alert(
                f"🔍 Daily Report — Need Clarification\n\n"
                f"I couldn't match this sale to inventory:\n"
                f"• Category: {sale['category']}\n"
                f"• Color: {sale['color'] or '—'}\n"
                f"• Qty: {int(qty)} {sale['unit'] or 'pcs'}\n"
                f"• Buyer: {buyer}\n\n"
                f"Please check manually and update inventory if needed."
            )

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


@app.get("/scheduler-status")
async def scheduler_status():
    """Diagnostic — confirms scheduler is alive and shows the next fire time."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run_time": str(job.next_run_time) if job.next_run_time else None,
            "trigger": str(job.trigger),
        })
    return {
        "scheduler_running": scheduler.running,
        "server_time_utc": datetime.utcnow().isoformat(),
        "server_time_manila": datetime.now(ZoneInfo("Asia/Manila")).isoformat(),
        "jobs": jobs,
    }


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
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": "🧪 Test from Flame & Finish bot — if you see this, Telegram delivery works."}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
        return {
            "configured": True,
            "http_status": resp.status_code,
            "response": resp.json(),
        }
    except Exception as e:
        return {"configured": True, "error": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
