import os
import logging
from dotenv import load_dotenv

load_dotenv(override=True)

from fastapi import FastAPI, Form, Request
from fastapi.responses import PlainTextResponse, Response
from twilio.rest import Client as TwilioClient
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

import agent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Flame & Finish Inventory Bot")

TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")

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


@app.get("/debug")
async def debug():
    """Temporary debug endpoint — remove before production."""
    import anthropic
    import httpx

    # Test Anthropic SDK (async)
    sdk_test = "not tested"
    try:
        client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", "").strip())
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=10,
            messages=[{"role": "user", "content": "Hi"}],
        )
        sdk_test = f"OK - {resp.content[0].text[:50]}"
    except Exception as e:
        cause = e.__cause__
        sdk_test = f"FAIL - {type(e).__name__}: {e} | cause: {type(cause).__name__ if cause else 'none'}: {cause}"

    # Test raw httpx POST to Anthropic API
    raw_post = "not tested"
    try:
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        async with httpx.AsyncClient() as hc:
            r = await hc.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": "Hi"}],
                },
                timeout=30,
            )
        raw_post = f"status {r.status_code} - {r.text[:100]}"
    except Exception as e:
        raw_post = f"FAIL - {type(e).__name__}: {e}"

    return {
        "sdk_test": sdk_test,
        "raw_post_test": raw_post,
        "anthropic_sdk_version": anthropic.__version__,
        "anthropic_key_prefix": (os.getenv("ANTHROPIC_API_KEY") or "")[:20] + "...",
    }


@app.post("/webhook")
async def whatsapp_webhook(request: Request):
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
            return PlainTextResponse("Invalid signature", status_code=403)

    # Restrict to allowed team numbers
    if ALLOWED_NUMBERS and From not in ALLOWED_NUMBERS:
        logger.warning(f"Unauthorized number: {From}")
        return PlainTextResponse("Unauthorized", status_code=403)

    # Process through Claude agent and reply
    try:
        reply = await agent.handle_message(Body, sender=From)
    except Exception as e:
        logger.error(f"Agent error: {e}", exc_info=True)
        reply = f"Error: {type(e).__name__}: {e}"

    # Return TwiML response — Twilio reads this and sends the reply as a WhatsApp message
    twiml = MessagingResponse()
    twiml.message(reply)
    return Response(content=str(twiml), media_type="text/xml")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
