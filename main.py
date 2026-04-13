import os
import time
import logging
import httpx
from collections import OrderedDict
from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv(override=True)

from fastapi import BackgroundTasks, FastAPI, Request, Response
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import agent
import notion_client as notion

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Deduplication: track recently processed Telegram update_ids to ignore retries.
_seen_update_ids: OrderedDict[int, float] = OrderedDict()
_DEDUP_TTL = 60  # seconds to remember an update_id
_DEDUP_MAX = 200  # max entries to keep

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # daily report destination
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

# Owner-only bot: only the owner can chat with the bot. Staff update Notion
# directly (FF Sales Log) and the 6PM cron processes their entries.
# Defaults to TELEGRAM_CHAT_ID — for a private DM, chat_id == user_id.
OWNER_TELEGRAM_ID = (os.getenv("OWNER_TELEGRAM_ID") or TELEGRAM_CHAT_ID or "").strip()


# --- Scheduled daily report (6PM Mon-Sat, Philippine time UTC+8) ---
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
    """Background task: process message through Claude and reply via Telegram."""
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


# --- Daily report ---

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
            await _send_telegram_alert(
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


# --- Diagnostics ---

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
    data = await _tg_call("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": "🧪 Test from Flame & Finish bot — if you see this, Telegram delivery works.",
    })
    return {"configured": True, "response": data}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
