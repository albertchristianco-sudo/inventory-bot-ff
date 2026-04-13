# Flame & Finish Inventory Agent

## Project Overview
Telegram-based inventory management bot for Flame & Finish Marketing Corp,
an import business in Cebu, Philippines dealing in SPC flooring and WPC wall panels.
The bot lets the owner and sales team check stock, record sales, and update prices
via Telegram messages — powered by Claude AI with Notion as the database.

## Tech Stack
- **Backend:** Python 3.10 (FastAPI + uvicorn)
- **Messaging:** Telegram Bot API (inbound via webhook, outbound via HTTPS)
- **Database:** Notion API (inventory DB + daily sales ledger + raw sales log)
- **AI Brain:** Claude API (`claude-sonnet-4-6`) via `anthropic` async SDK
- **Scheduler:** APScheduler (6PM Mon-Sat daily report, Asia/Manila tz)
- **Hosting:** Railway (auto-deploys from GitHub on push)
- **Repo:** github.com/albertchristianco-sudo/inventory-bot-ff

## Architecture
```
Telegram Message
  → Telegram Bot API (webhook POST)
    → Railway (FastAPI at /telegram-webhook)
      → agent.py (Claude async API with tool-use loop)
        → notion_client.py (Notion API for inventory CRUD)
      ← Telegram Bot API sendMessage (reply to same chat)
    ← {"ok": true}
  ← Telegram reply appears

+ APScheduler fires _run_daily_report() at 18:00 Mon-Sat (Asia/Manila),
  which pulls unprocessed sales from FF Sales Log, matches them against
  inventory, deducts stock, and posts a summary to TELEGRAM_CHAT_ID.
  Mismatches trigger a clarification alert to TELEGRAM_OWNER_CHAT_ID.
```

## Key Files
| File | Purpose |
|---|---|
| `main.py` | FastAPI server, Telegram webhook at `/telegram-webhook`, scheduler, daily report, diagnostics |
| `agent.py` | Claude async API client, agentic tool-use loop, 4 tools, keyword alias map, conversation memory |
| `notion_client.py` | Notion API functions: query_products, update_stock, update_price, log_sale, get_unprocessed_sales, find_inventory_product, mark_sale_processed |
| `requirements.txt` | Unpinned deps: fastapi, uvicorn, anthropic, httpx, python-dotenv, python-multipart, apscheduler |
| `railway.json` | Nixpacks builder config for Railway |
| `Procfile` | Railway start command: `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| `.env` | Local env vars (never committed) |
| `.env.example` | Template with placeholder values |

## Notion Databases

### FF Inventory (`NOTION_DATABASE_ID`)
- **Database ID:** `be5c97b4-7340-4310-ab87-7f8fad4f2856`
- **Properties:**
  - `Product Name` (title) — e.g. "WHITE OAK (WOK) DY 2008"
  - `FF Item Code` (rich_text) — unique code like FF-SPCFWG-CL04
  - `Category` (select) — "SPC Flooring", "WPC Wall Panels", "Outdoor Decking", etc.
  - `Color/Attribute` (rich_text)
  - `Stock` (number) — current stock count
  - `Stock (Boxes)` (number) — stock in boxes
  - `Unit Price` (number) — selling price in Philippine Pesos
  - `Min Sellable (Floor)` (number) — minimum price floor
  - `SRP @ 1.5x + VAT (₱)` (number) — suggested retail price

### Daily Sales Ledger (`NOTION_SALES_DB_ID`)
- **Database ID:** `5330a1dc-4524-43c4-867f-58a64a21da61`
- **Properties:**
  - `Sale Entry` (title) — auto-generated: "[Customer] - [Product] - [Date]"
  - `Date` (date) — sale date (ISO 8601)
  - `Customer Name` (rich_text)
  - `Customer Contact` (phone) — optional
  - `Product Sold` (multi_select) — SPC Flooring, WPC Wall Panels, Outdoor Decking, Fencing, Interior Finishes, Other
  - `Quantity` (number)
  - `Unit` (select) — boxes, sqm, pieces, sets
  - `Unit Price (₱)` (number)
  - `Total Amount (₱)` (number) — Quantity × Unit Price
  - `Payment Method` (select) — Cash, Bank Transfer, GCash, PayMaya, Check, Terms / Credit
  - `Payment Status` (select) — Paid, Partial, Unpaid
  - `Amount Received (₱)` (number)
  - `Balance Due (₱)` (number) — Total Amount - Amount Received
  - `Transaction Type` (select) — Walk-in, Delivery, Pick-up, Online Order
  - `Handled By` (select) — Ac, Staff 1, Staff 2, Staff 3
  - `Notes` (rich_text) — optional remarks

### FF Sales Log (`FF_SALES_LOG_DB_ID`)
- **Database ID:** `a3406a84-4be0-41d0-9593-8090cae4133c`
- Raw sales entries that staff log throughout the day. The 6PM scheduled
  job pulls everything where `Processed == false`, deducts inventory,
  posts the summary to Telegram, then flips `Processed` to true.
- **Properties used:** Buyer Name, Category, Color/Variant, Date, Quantity,
  Price per Unit, Installation Fee, Salesperson, Payment Method, Payment
  Status, Invoice #, Unit, Processed (checkbox).

## Agent Tools (defined in agent.py)
1. **lookup_products** — Search inventory by keyword or get all products
2. **update_stock** — Set new stock quantity by page ID
3. **update_price** — Set any pricing field (unit price, landed cost, min sellable, SRP tiers, USD cost)
4. **log_sale** — Record a sale with full details (customer, product, quantity, unit, price, payment, transaction type, handler)

## Agent Behavior
- Responds in **English by default**; replies in Bisaya when staff writes in Bisaya
- **Understands** Cebuano, Tagalog, and English input
- Never guesses stock — always calls `lookup_products` first
- Resolves shorthand via the **Keyword Alias Map** in the system prompt
  (e.g. "SPC" → SPC Flooring, "reducer" → Interior Finishes, "bamboo" → search "bamboo")
- Sale processing: (1) lookup product → (2) update stock → (3) log sale
- Confirms old stock, deduction, and new stock on every sale
- Short WhatsApp/Telegram-friendly replies (under 5 lines when possible)
- 30-minute conversation memory per Telegram user_id (in-memory, resets on redeploy)

## Authorized Users
Numeric Telegram user IDs in `ALLOWED_TELEGRAM_IDS` env var (comma-separated).
Each staff member messages **@userinfobot** on Telegram to get their numeric user ID.
The bot refuses unknown senders and replies with their ID so the admin can add them.

## Telegram Setup

### Bot creation
1. Message **@BotFather** on Telegram → `/newbot` → follow prompts
2. Copy the bot token → set as `TELEGRAM_BOT_TOKEN` on Railway
3. Start a chat with your new bot and send any message so Telegram opens the DM
4. Get your chat ID from @userinfobot or by calling `getUpdates` → set as `TELEGRAM_CHAT_ID`

### Webhook registration (one-time)
```
curl -X POST https://web-production-492dc.up.railway.app/telegram-setup-webhook \
  -H 'content-type: application/json' \
  -d '{"url": "https://web-production-492dc.up.railway.app/telegram-webhook"}'
```
Expect: `{"ok": true, "result": true, "description": "Webhook was set"}`

### Verify webhook
```
curl https://web-production-492dc.up.railway.app/telegram-webhook-info
```

### How replies work
The bot replies to whatever chat the message came from — so private DMs
stay private, group chats reply in-place. The daily 6PM report goes
specifically to `TELEGRAM_CHAT_ID`. Mismatch clarification alerts go to
`TELEGRAM_OWNER_CHAT_ID` (falls back to `TELEGRAM_CHAT_ID` if unset).

## Deployment
- **Railway URL:** `https://web-production-492dc.up.railway.app`
- **Telegram webhook:** `https://web-production-492dc.up.railway.app/telegram-webhook`
- **Auto-deploy:** Push to `main` branch on GitHub triggers Railway rebuild
- **Deploy time:** ~60-90 seconds after git push
- **Important:** Railway env vars may have trailing newlines — code uses `.strip()` on API keys

## Endpoints
| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Health check |
| POST | `/telegram-webhook` | Telegram pushes incoming messages here |
| POST | `/telegram-setup-webhook` | One-shot: register the webhook URL with Telegram |
| GET | `/telegram-webhook-info` | Diagnostic: show Telegram's view of the registered webhook |
| POST | `/run-daily-report` | Manually trigger the daily sales report (same work as 6PM cron) |
| GET | `/scheduler-status` | Diagnostic: confirm scheduler is armed, show next fire time |
| POST | `/test-telegram` | Diagnostic: send a test message and return the raw Telegram API response |

## Environment Variables
```
NOTION_API_KEY            — Notion integration token (starts with ntn_...)
NOTION_DATABASE_ID        — Inventory database ID
NOTION_SALES_DB_ID        — Daily Sales Ledger database ID
FF_SALES_LOG_DB_ID        — Raw FF Sales Log database ID (has sensible default)
ANTHROPIC_API_KEY         — Anthropic API key (starts with sk-ant-...)
TELEGRAM_BOT_TOKEN        — Telegram bot token from @BotFather
TELEGRAM_CHAT_ID          — Chat ID for the 6PM daily report
TELEGRAM_OWNER_CHAT_ID    — Chat ID for mismatch alerts (optional; defaults to TELEGRAM_CHAT_ID)
TELEGRAM_WEBHOOK_SECRET   — Optional shared secret to reject forged webhook calls
ALLOWED_TELEGRAM_IDS      — Comma-separated numeric user IDs allowed to chat with the bot
```

## Key Technical Decisions & Gotchas
1. **AsyncAnthropic client** — Sync client causes `APIConnectionError` on Railway. Must use `anthropic.AsyncAnthropic` with `await`.
2. **`.strip()` on API keys** — Railway env vars can have trailing `\n` which causes `Illegal header value` errors.
3. **`load_dotenv(override=True)`** — Needed because the system may have an empty `ANTHROPIC_API_KEY` env var that blocks dotenv.
4. **Notion property names matter** — Must match exactly: "Color/Attribute" (not "Variant"), "Unit Price" (not "Price").
5. **Conversation memory is in-memory** — Keyed by `telegram:<user_id>`. Resets on every Railway redeploy. Fine for now, could add Redis later.
6. **FastAPI lifespan, not on_event** — `@app.on_event("startup")` is deprecated and can be flaky in production. Use the `asynccontextmanager` lifespan passed to `FastAPI(lifespan=lifespan)`.
7. **Scheduler callback must be `async def`** — AsyncIOScheduler runs coroutines natively. The old `asyncio.ensure_future()` wrapper silently swallowed errors; the current `async def` callback wraps the body in try/except so failures show up in Railway logs.
8. **Telegram dedup via update_id** — Telegram retries webhook delivery if it doesn't get a 200 back quickly. We remember the last 200 update_ids for 60s and ignore duplicates.
9. **Webhook authorization** — Two layers: (a) optional `X-Telegram-Bot-Api-Secret-Token` header check, (b) numeric user_id allowlist. Unknown senders get a "not authorized" reply that includes their ID for easy admin onboarding.
10. **Scheduler catch-up** — If Railway redeploys exactly at 6PM, the scheduler misses that fire window. APScheduler does not catch up missed runs — hit `POST /run-daily-report` manually if this happens.

## Pending / TODO
- [ ] Enable `TELEGRAM_WEBHOOK_SECRET` in production for forged-request protection
- [ ] Map Telegram user_ids to staff names (so `Handled By` can auto-fill in log_sale)
- [ ] Investigate persistent conversation memory (Redis) so memory survives redeploys

## Future Feature Ideas
- Persistent conversation memory (Redis or database)
- Weekly sales summary reports
- Low stock alerts (auto-notify when stock drops below threshold)
- Photo receipts (handle image messages via Telegram's photo updates)
- Export sales data to spreadsheet
- Multi-language replies (reply in the same language as the user, not just Bisaya vs English)
