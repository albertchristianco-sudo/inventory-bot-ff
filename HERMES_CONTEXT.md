# Hermes Context — Flame & Finish Inventory Bot

This document gives the Hermes cron agent full context on the Flame & Finish
Inventory Bot project, its architecture, and Hermes's role in it.

## Project Overview

Telegram-based inventory management bot for **Flame & Finish Marketing Corp**,
an import business in Cebu, Philippines dealing in SPC flooring and WPC wall
panels. The owner interacts with the bot via Telegram to check stock, update
prices, and review daily sales reports. Staff log sales directly in Notion.

**Owner:** Albert  
**Repo:** `github.com/albertchristianco-sudo/inventory-bot-ff`  
**Hosting:** Railway (auto-deploys from GitHub `main` branch)  
**Railway URL:** `https://web-production-492dc.up.railway.app`  
**Deploy time:** ~60-90 seconds after push to `main`

## Tech Stack

- **Backend:** Python 3.10, FastAPI + uvicorn
- **Messaging:** Telegram Bot API (webhook inbound, HTTPS outbound)
- **Database:** Notion API (inventory DB + sales ledger + raw sales log + archive)
- **AI Brain:** Claude API (`claude-sonnet-4-6`) via `anthropic` async SDK
- **Scheduler:** Hermes (you!) — external cron agent
- **Hosting:** Railway

## Hermes's Role

Hermes owns **all scheduled tasks** for this project. The bot process itself
has no scheduler — it relies entirely on Hermes to fire HTTP endpoints on
schedule.

### Cron Jobs

| Job | Cron (UTC) | Local Time | Endpoint | Purpose |
|-----|-----------|------------|----------|---------|
| Daily report | `0 10 * * 1-6` | 18:00 Mon-Sat Asia/Manila | `POST /run-daily-report` | Process unmatched sales, deduct inventory, send Telegram summary |
| Weekly archive | `0 2 * * 0` | 10:00 Sunday Asia/Manila | `POST /archive-weekly` | Move processed sales to archive DB, clear working log |

### Authentication

Both endpoints require the `X-Hermes-Secret` header. The secret is stored as
`HERMES_SECRET` on Railway. If the header is missing or wrong, the endpoint
returns HTTP 403.

```
curl -X POST https://web-production-492dc.up.railway.app/run-daily-report \
  -H "X-Hermes-Secret: <the-secret>"
```

### Expected Responses

**Daily report** (`POST /run-daily-report`):
```json
{
  "processed": 5,
  "pending": 2,
  "grand_total": 45000.00
}
```
- `processed`: sales auto-matched and deducted from inventory
- `pending`: sales with uncertain matches, queued for owner approval
- `grand_total`: total peso value of all processed sales

**Weekly archive** (`POST /archive-weekly`):
```json
{
  "candidates": 42,
  "archived": 42,
  "failed": 0,
  "failures": []
}
```
- `candidates`: processed sales found in the working log
- `archived`: successfully copied to archive DB and soft-deleted from source
- `failed`: count of failures (source row preserved if copy fails)
- `failures`: error details for each failed row

### Retry & Error Handling

- **Timeout:** 60 seconds (the daily report can take 30-50s for many sales)
- **Retries:** 3 retries with exponential backoff on network failure
- **On success:** Silent (no notification needed)
- **On failure:** Alert via Telegram or whatever Hermes's alert channel is
- **Logging:** `~/.hermes/logs/flame_finish_report.log` with 30-day rotation

### Failure Safety

If Hermes is down or an endpoint fails, **no data is lost**:
- Unprocessed sales stay `Processed=false` in Notion and reappear in the
  next daily report
- Unarchived processed sales stay in the working log until the next
  successful Sunday archive run

## Architecture Flow

```
Staff logs sale in Notion (FF Sales Log)
  ↓
Hermes fires POST /run-daily-report at 18:00 Mon-Sat
  ↓
Bot pulls unprocessed sales from FF Sales Log
  ↓
Multi-strategy matcher tries to find inventory product
  ↓
  ├─ Strong match → auto-deduct stock, mark Processed, include in summary
  ├─ Weak match → queue for owner approval, DM owner with best guess
  └─ No match → queue for owner approval, DM owner
  ↓
Bot sends daily summary to owner via Telegram
  ↓
Owner reviews pending items, replies "approve INV-XXXX" or "skip INV-XXXX"
  ↓
Every Sunday at 10:00 Manila, Hermes fires POST /archive-weekly
  ↓
Processed sales copied to FF Sales Archive, source rows soft-deleted
Pending sales stay in FF Sales Log for next week's reports
```

## Notion Databases

| Database | Env Var | ID | Purpose |
|----------|---------|----|---------| 
| FF Inventory | `NOTION_DATABASE_ID` | `be5c97b4-7340-4310-ab87-7f8fad4f2856` | Product catalog with stock levels |
| Daily Sales Ledger | `NOTION_SALES_DB_ID` | `5330a1dc-4524-43c4-867f-58a64a21da61` | Structured sales records (bot-created) |
| FF Sales Log | `FF_SALES_LOG_DB_ID` | `a3406a84-4be0-41d0-9593-8090cae4133c` | Raw staff entries, processed by daily report |
| FF Sales Archive | `FF_SALES_ARCHIVE_DB_ID` | *(set on Railway)* | Weekly archive of processed sales |

## Key Endpoints (Full List)

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/` | — | Health check |
| POST | `/telegram-webhook` | Telegram secret + owner-id | Incoming Telegram messages |
| POST | `/run-daily-report` | `X-Hermes-Secret` | **Hermes daily cron target** |
| POST | `/archive-weekly` | `X-Hermes-Secret` | **Hermes weekly cron target** |
| POST | `/telegram-setup-webhook` | — | One-time webhook registration |
| GET | `/telegram-webhook-info` | — | Diagnostic: webhook status |
| POST | `/test-telegram` | — | Diagnostic: send test message |

## Environment Variables (Railway)

```
NOTION_API_KEY            — Notion integration token
NOTION_DATABASE_ID        — FF Inventory database
NOTION_SALES_DB_ID        — Daily Sales Ledger database
FF_SALES_LOG_DB_ID        — Raw FF Sales Log database
FF_SALES_ARCHIVE_DB_ID    — Archive database (same schema as FF Sales Log)
ANTHROPIC_API_KEY         — Claude API key
TELEGRAM_BOT_TOKEN        — Telegram bot token
TELEGRAM_CHAT_ID          — Owner's chat ID (for reports)
OWNER_TELEGRAM_ID         — Owner's user ID (access control)
TELEGRAM_WEBHOOK_SECRET   — Optional webhook auth
HERMES_SECRET             — Shared secret for Hermes endpoints
```

## Current State & Recent Changes

### What's been built (all on branch `claude/add-keyword-aliases-IqX4o`):
1. Keyword alias map in Claude's system prompt for shorthand resolution
2. Multi-strategy inventory matcher with confidence scoring (strong/weak)
3. Owner-approval flow (approve/skip/pending commands via Telegram)
4. Hermes cron integration (removed APScheduler from the bot)
5. X-Hermes-Secret header authentication on both endpoints
6. Weekly archive: copy Processed sales to archive DB, soft-delete source

### Planned but not yet built:
- **Notion Relation-based sales entry** — Replace free-text product fields
  in FF Sales Log with a Relation property pointing to FF Inventory. This
  would eliminate category/color mismatches from staff typos. Includes
  support for pre-sale orders (products not yet in inventory).
- **Xero API reconciliation** — Read-only cross-reference of daily sales
  against paid Xero invoices. Would add a reconciliation section to the
  daily report showing which sales have matching paid invoices.
- **TELEGRAM_WEBHOOK_SECRET** — Enable forged-request protection in prod.

## Timezone Reference

All times are **Asia/Manila (UTC+8)**:
- 18:00 Manila = 10:00 UTC (daily report)
- 10:00 Manila = 02:00 UTC (weekly archive)
- Staff work hours: roughly 08:00-18:00 Manila
- The daily report fires at end of business day so it captures all sales

## Gotchas

1. Railway env vars may have trailing newlines — the bot strips them, but
   be aware if you're debugging.
2. Pending approvals are in-memory — a Railway redeploy between the daily
   report and the owner's approval loses the queue. Sales stay
   Processed=false and reappear in the next report. No data loss.
3. The daily report can take 30-50 seconds for many sales — set your
   timeout to at least 60s.
4. The weekly archive soft-deletes source rows (Notion trash, recoverable
   for 30 days). If anything goes wrong, rows can be restored.
