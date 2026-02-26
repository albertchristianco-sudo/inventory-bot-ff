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


# Debug: track last webhook hit
_last_webhook = {"hit": False, "from": "", "body": "", "error": "", "all_params": {}}


@app.get("/")
async def health():
    return {"status": "ok", "service": "Flame & Finish Inventory Bot"}


@app.get("/debug")
async def debug():
    return {
        "last_webhook": _last_webhook,
        "whatsapp_number": TWILIO_WHATSAPP_NUMBER,
        "allowed_numbers": list(ALLOWED_NUMBERS),
    }


@app.post("/webhook")
async def whatsapp_webhook(request: Request):
    """Receive incoming WhatsApp messages from Twilio."""
    form_data = await request.form()
    params = dict(form_data)

    From = params.get("From", "")
    Body = params.get("Body", "")

    # Debug tracking
    _last_webhook["hit"] = True
    _last_webhook["from"] = From
    _last_webhook["body"] = Body
    _last_webhook["all_params"] = {k: str(v)[:100] for k, v in params.items()}

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
        reply = "Sorry, something went wrong processing your message. Try again in a bit!"

    # Send reply via Twilio REST API using the WhatsApp number directly
    try:
        msg = _get_twilio_client().messages.create(
            body=reply,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=From,
        )
        _last_webhook["error"] = f"sent OK - sid: {msg.sid}, status: {msg.status}"
    except Exception as e:
        logger.error(f"Twilio send error: {e}")
        _last_webhook["error"] = f"Twilio send: {e}"

    return PlainTextResponse("OK")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
