# Flame & Finish Inventory Agent

## Project Overview
WhatsApp-based inventory management bot for Flame & Finish Marketing Corp, 
an import business in Cebu Philippines dealing in SPC flooring and WPC wall panels.

## Tech Stack
- Backend: Python (FastAPI)
- WhatsApp: Twilio WhatsApp API
- Database: Notion API
- AI Brain: Claude API (claude-sonnet-4-6)
- Hosting: Railway

## Business Context
- Products include SPC flooring and WPC wall panels
- Each product has variants by color/finish
- Stock is tracked by boxes or panels
- Prices are in Philippine Peso (₱)
- Primary user is the business owner sending casual Filipino-English messages

## Agent Behavior
- Understand casual queries like "how many nalang yung oak SPC?" 
- Update stock when owner sends sale messages like "nabenta 20 boxes oak SPC 850 pesos"
- Reply in concise, friendly format
- Never guess stock numbers — always pull from Notion

## Key Files
- main.py — FastAPI server and Twilio webhook
- notion_client.py — All Notion read/write functions
- agent.py — Claude API logic and message handling
- .env — API keys (never commit this)