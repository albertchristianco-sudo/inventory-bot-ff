# Flame & Finish Inventory Agent

## Project Overview
WhatsApp-based inventory management bot for Flame & Finish Marketing Corp,
an import business in Cebu, Philippines dealing in SPC flooring and WPC wall panels.
The bot lets the owner and sales team check stock, record sales, and update prices
via WhatsApp messages — powered by Claude AI with Notion as the database.

## Tech Stack
- **Backend:** Python 3.10 (FastAPI + uvicorn)
- **WhatsApp:** Twilio WhatsApp API (production number + sandbox fallback)
- **Database:** Notion API (inventory DB + sales log DB)
- **AI Brain:** Claude API (`claude-sonnet-4-6`) via `anthropic` async SDK
- **Hosting:** Railway (auto-deploys from GitHub on push)
- **Repo:** github.com/albertchristianco-sudo/inventory-bot-ff

## Architecture
```
WhatsApp Message
  → Twilio (webhook POST)
    → Railway (FastAPI at /webhook)
      → agent.py (Claude async API with tool-use loop)
        → notion_client.py (Notion API for inventory CRUD)
      ← Twilio REST API sends reply via messages.create()
    ← PlainTextResponse("OK")
  ← WhatsApp reply appears
```

## Key Files
| File | Purpose |
|---|---|
| `main.py` | FastAPI server, Twilio webhook at `/webhook`, REST API replies, team allow-list |
| `agent.py` | Claude async API client, agentic tool-use loop, 4 tools, conversation memory |
| `notion_client.py` | Notion API functions: query_products, update_stock, update_price, log_sale |
| `requirements.txt` | Unpinned deps: fastapi, uvicorn, twilio, anthropic, httpx, python-dotenv, python-multipart |
| `railway.json` | Nixpacks builder config for Railway |
| `Procfile` | Railway start command: `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| `.env` | Local env vars (never committed) |
| `.env.example` | Template with placeholder values |

## Notion Databases

### FF Inventory (`NOTION_DATABASE_ID`)
- **Database ID:** `be5c97b473404310ab877f8fad4f2856`
- **Properties:**
  - `Product Name` (title) — e.g. "WHITE OAK (WOK) DY 2008"
  - `Category` (select) — "SPC Flooring", "Wall Panels", "Outdoor Products"
  - `Color / Variant` (rich_text)
  - `Stock` (number) — current stock count
  - `Unit` (select) — "pcs", "boxes", etc.
  - `Unit Price (₱)` (number) — price in Philippine Pesos
- ~40+ products across 3 categories

### FF Sales Log (`NOTION_SALES_DB_ID`)
- **Database ID:** `91f464cc6f194a56be9e7ac591b8a483`
- **Properties:**
  - `Product` (title) — product name
  - `Quantity` (number)
  - `Unit Price (₱)` (number)
  - `Total (₱)` (number) — auto-calculated: quantity * unit price
  - `Sold By` (rich_text) — salesperson phone number or name
  - `Date` (date) — sale date

## Agent Tools (defined in agent.py)
1. **lookup_products** — Search inventory by keyword or get all products
2. **update_stock** — Set new stock quantity by page ID
3. **update_price** — Set new price by page ID
4. **log_sale** — Record a sale (product, quantity, unit price, sold_by)

## Agent Behavior
- Responds in **English by default**
- **Understands** Cebuano, Tagalog, and English input
- Never guesses stock — always calls `lookup_products` first
- Sale processing: (1) lookup product → (2) update stock → (3) log sale
- Confirms old stock, deduction, and new stock on every sale
- Short WhatsApp-friendly replies
- 30-minute conversation memory per phone number (in-memory, resets on redeploy)

## Authorized Users
4 WhatsApp numbers in `ALLOWED_NUMBERS` env var (comma-separated with `whatsapp:` prefix).
Each salesperson messages the bot directly from their own number.

## Twilio WhatsApp Setup

### Production Number (current)
- **Number:** `+15557803575` (business name: "Flame and Finish")
- **Messaging Service SID:** `MG26b64b51f8fa0cdc9c01b00ee01f9504`
- **WhatsApp Business Account ID:** `932464726410411`
- **Meta Business Manager ID:** `734181149085536`
- **Status:** Webhook works, but Meta disabled the WABA (error 63112). Need to resolve with Meta Business Manager before replies will deliver.
- **Webhook configured on:** Sender settings → "Webhook URL for incoming messages"
- **Reply method:** Twilio REST API `messages.create()` with `from_=whatsapp:+15557803575`

### Sandbox Fallback
- **Number:** `+14155238886` (Twilio sandbox)
- **Webhook configured on:** Twilio Console → Messaging → WhatsApp Sandbox → "When a message comes in"
- **Reply method:** Same REST API approach works. TwiML also works with sandbox.
- **To use sandbox:** Change `TWILIO_WHATSAPP_NUMBER` on Railway to `whatsapp:+14155238886` and configure sandbox webhook URL.

### Switching Between Production and Sandbox
Only need to change on Railway:
```
TWILIO_WHATSAPP_NUMBER=whatsapp:+15557803575   # production
TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886   # sandbox
```
And ensure the webhook URL `https://web-production-492dc.up.railway.app/webhook` is configured on the active sender.

## Deployment
- **Railway URL:** `https://web-production-492dc.up.railway.app`
- **Webhook:** `https://web-production-492dc.up.railway.app/webhook`
- **Auto-deploy:** Push to `main` branch on GitHub triggers Railway rebuild
- **Env vars on Railway:** All keys from `.env` plus `VALIDATE_TWILIO_SIGNATURE=false`
- **Important:** Railway env vars may have trailing newlines — code uses `.strip()` on API keys
- **Deploy time:** ~60-90 seconds after git push

## Environment Variables
```
TWILIO_ACCOUNT_SID        — Twilio account SID (starts with AC...)
TWILIO_AUTH_TOKEN          — Twilio auth token
TWILIO_WHATSAPP_NUMBER    — whatsapp:+15557803575 (production) or whatsapp:+14155238886 (sandbox)
NOTION_API_KEY            — Notion integration token (starts with ntn_...)
NOTION_DATABASE_ID        — Inventory database ID
NOTION_SALES_DB_ID        — Sales log database ID
ANTHROPIC_API_KEY         — Anthropic API key (starts with sk-ant-...)
ALLOWED_NUMBERS           — Comma-separated WhatsApp numbers (whatsapp:+63...)
VALIDATE_TWILIO_SIGNATURE — "true" or "false" (false for sandbox/dev)
```

## Key Technical Decisions & Gotchas
1. **REST API replies (not TwiML)** — Production WhatsApp numbers with Messaging Services require `messages.create()` REST API calls. TwiML only works reliably with the sandbox.
2. **AsyncAnthropic client** — Sync client causes `APIConnectionError` on Railway. Must use `anthropic.AsyncAnthropic` with `await`.
3. **`.strip()` on API keys** — Railway env vars can have trailing `\n` which causes `Illegal header value` errors.
4. **`load_dotenv(override=True)`** — Needed because system may have empty `ANTHROPIC_API_KEY` env var that blocks dotenv.
5. **Twilio signature validation disabled** — Set `VALIDATE_TWILIO_SIGNATURE=false` for dev/sandbox. Enable for production.
6. **Notion property names matter** — Must match exactly: "Color / Variant" (not "Variant"), "Unit Price (₱)" (not "Price").
7. **Conversation memory is in-memory** — Resets on every Railway redeploy. Fine for now, could add Redis later.
8. **Twilio error 63112** — Meta disabled the WhatsApp Business Account. Need to resolve via Meta Business Manager before production number can send replies.

## Pending / TODO
- [ ] Resolve Meta WABA suspension (error 63112) to enable production number replies
- [ ] Remove unused imports (MessagingResponse, Response) after confirming production works
- [ ] Enable Twilio signature validation for production

## Future Feature Ideas
- Persistent conversation memory (Redis or database)
- Daily/weekly sales summary reports
- Low stock alerts (auto-notify when stock drops below threshold)
- Photo receipts (handle image messages)
- Export sales data to spreadsheet
- Multi-language responses (reply in the same language as the user)
