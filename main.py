import os
import secrets
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
from dotenv import load_dotenv
from db import supabase

load_dotenv()
app = FastAPI()

VERIFY_TOKEN = (os.getenv("META_VERIFY_TOKEN") or "").strip()

@app.get("/")
def root():
    return {"status": "ok"}

@app.head("/")
def root_head():
    return PlainTextResponse(status_code=200)

@app.get("/webhooks/meta")
async def verify_webhook(request: Request):
    mode = (request.query_params.get("hub.mode") or "").strip()
    token = (request.query_params.get("hub.verify_token") or "").strip()
    challenge = request.query_params.get("hub.challenge")

    token_matches = bool(VERIFY_TOKEN) and secrets.compare_digest(token, VERIFY_TOKEN)
    print(
        "Webhook verification attempt:",
        {
            "mode": mode,
            "challenge_present": bool(challenge),
            "token_present": bool(token),
            "env_token_present": bool(VERIFY_TOKEN),
            "token_matches": token_matches,
            "token_length": len(token),
            "env_token_length": len(VERIFY_TOKEN),
        },
    )

    if mode == "subscribe" and token_matches:
        return PlainTextResponse(content=challenge or "", status_code=200)
    
    raise HTTPException(status_code=403, detail="Verification Failed.")

@app.post("/webhooks/meta")
async def receive_webhook(request: Request):
    data = await request.json()

    raw_event = (
        supabase.table("webhook_events")
        .insert({
            "provider": "meta_whatsapp",
            "event_type": "messages",
            "payload": data,
            "processed": False,
        })
        .execute()
    )

    raw_event_rows = raw_event.data or []
    event_id = raw_event_rows[0].get("id") if raw_event_rows else None

    try:
        process_whatsapp_payload(data)

        if event_id:
            supabase.table("webhook_events").update({
                "processed": True,
                "processing_error": None,
            }).eq("id", event_id).execute()

    except Exception as e:
        print("Webhook processing error:", str(e))

        if event_id:
            supabase.table("webhook_events").update({
                "processed": False,
                "processing_error": str(e),
            }).eq("id", event_id).execute()

    return JSONResponse({"received": True})

def get_salon_id_by_phone_number_id(phone_number_id: str) -> str | None:
    result = (
        supabase.table("integrations")
        .select("salon_id")
        .eq("channel", "whatsapp")
        .eq("external_phone_number_id", phone_number_id)
        .limit(1)
        .execute()
    )

    if result.data:
        return result.data[0]["salon_id"]
    return None

def get_or_create_conversation(salon_id: str, external_user_id: str, channel: str) -> str:
    if not external_user_id:
        raise ValueError("Incoming message is missing external user id.")

    existing = (
        supabase.table("conversations")
        .select("id")
        .eq("salon_id", salon_id)
        .eq("external_user_id", external_user_id)
        .eq("channel", channel)
        .limit(1)
        .execute()
    )

    if existing.data:
        return existing.data[0]["id"]

    created = (
        supabase.table("conversations")
        .insert({
            "salon_id": salon_id,
            "external_user_id": external_user_id,
            "channel": channel,
            "last_message_at": datetime.now(timezone.utc).isoformat(),
        })
        .execute()
    )

    created_rows = created.data or []
    if created_rows:
        return created_rows[0]["id"]

    existing = (
        supabase.table("conversations")
        .select("id")
        .eq("salon_id", salon_id)
        .eq("external_user_id", external_user_id)
        .eq("channel", channel)
        .limit(1)
        .execute()
    )
    if existing.data:
        return existing.data[0]["id"]

    raise RuntimeError("Failed to create or fetch conversation.")

def parse_message_timestamp(timestamp: str | None) -> str | None:
    if not timestamp:
        return None

    try:
        return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None

def process_inbound_message(phone_number_id: str, message: dict) -> None:
    salon_id = get_salon_id_by_phone_number_id(phone_number_id)
    if not salon_id:
        raise ValueError(f"No salon found for phone_number_id={phone_number_id}")

    external_user_id = message.get("from")
    external_message_id = message.get("id")
    message_type = message.get("type")
    timestamp = message.get("timestamp")

    if not external_message_id:
        raise ValueError("Incoming message is missing external message id.")
    if not message_type:
        raise ValueError("Incoming message is missing message type.")

    text_content = None
    if message_type == "text":
        text_content = message.get("text", {}).get("body")

    conversation_id = get_or_create_conversation(
        salon_id=salon_id,
        external_user_id=external_user_id,
        channel="whatsapp",
    )

    supabase.table("messages").insert({
        "conversation_id": conversation_id,
        "direction": "inbound",
        "external_message_id": external_message_id,
        "message_type": message_type,
        "text_content": text_content,
        "payload": message,
        "status": "received",
        "sent_at": parse_message_timestamp(timestamp),
    }).execute()

    supabase.table("conversations").update({
        "last_message_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", conversation_id).execute()

def process_whatsapp_payload(payload: dict) -> None:
    entries = payload.get("entry", [])

    for entry in entries:
        changes = entry.get("changes", [])

        for change in changes:
            value = change.get("value", {})
            metadata = value.get("metadata", {})
            messages = value.get("messages", [])

            phone_number_id = metadata.get("phone_number_id")
            if not phone_number_id:
                continue

            for message in messages:
                process_inbound_message(phone_number_id, message)

