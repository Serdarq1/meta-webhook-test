import os
import secrets
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import PlainTextResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from db import supabase

load_dotenv()
app = FastAPI()

VERIFY_TOKEN = (os.getenv("META_VERIFY_TOKEN") or "").strip()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {"status": "ok"}


@app.head("/")
def root_head():
    return PlainTextResponse(status_code=200)


# ---------------------------------------------------------------------------
# Webhook verification (GET)
# ---------------------------------------------------------------------------

@app.get("/webhooks/meta")
async def verify_webhook(request: Request):
    mode      = (request.query_params.get("hub.mode")         or "").strip()
    token     = (request.query_params.get("hub.verify_token") or "").strip()
    challenge =  request.query_params.get("hub.challenge")

    token_matches = bool(VERIFY_TOKEN) and secrets.compare_digest(token, VERIFY_TOKEN)
    print("Webhook verification attempt:", {
        "mode": mode,
        "challenge_present": bool(challenge),
        "token_present": bool(token),
        "env_token_present": bool(VERIFY_TOKEN),
        "token_matches": token_matches,
    })

    if mode == "subscribe" and token_matches:
        return PlainTextResponse(content=challenge or "", status_code=200)

    raise HTTPException(status_code=403, detail="Verification failed.")


# ---------------------------------------------------------------------------
# Webhook receiver (POST)
# ---------------------------------------------------------------------------

@app.post("/webhooks/meta")
async def receive_webhook(request: Request):
    data = await request.json()

    raw = (
        supabase.table("webhook_events")
        .insert({
            "provider":   "meta_whatsapp",
            "event_type": "messages",
            "payload":    data,
            "processed":  False,
        })
        .execute()
    )
    rows     = raw.data or []
    event_id = rows[0].get("id") if rows else None

    try:
        process_whatsapp_payload(data)
        if event_id:
            supabase.table("webhook_events").update({
                "processed":        True,
                "processing_error": None,
            }).eq("id", event_id).execute()

    except Exception as exc:
        print("Webhook processing error:", str(exc))
        if event_id:
            supabase.table("webhook_events").update({
                "processed":        False,
                "processing_error": str(exc),
            }).eq("id", event_id).execute()

    # Always 200 — Meta will retry on non-200
    return JSONResponse({"received": True})


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

@app.get("/conversations")
def list_conversations(
    salon_id: str = Query(...),
    limit:    int = Query(50, ge=1, le=200),
    offset:   int = Query(0,  ge=0),
):
    result = (
        supabase.table("conversations")
        .select("id, salon_id, external_user_id, channel, last_message_at, created_at")
        .eq("salon_id", salon_id)
        .order("last_message_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return {"conversations": result.data, "limit": limit, "offset": offset}


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

@app.get("/messages")
def list_messages(
    conversation_id: str = Query(...),
    limit:           int = Query(50, ge=1, le=200),
    offset:          int = Query(0,  ge=0),
):
    convo = (
        supabase.table("conversations")
        .select("id")
        .eq("id", conversation_id)
        .limit(1)
        .execute()
    )
    if not convo.data:
        raise HTTPException(status_code=404, detail="Conversation not found.")

    result = (
        supabase.table("messages")
        .select(
            "id, conversation_id, direction, message_type, "
            "text_content, status, sent_at, created_at"
        )
        .eq("conversation_id", conversation_id)
        .order("sent_at", desc=False)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return {"messages": result.data, "limit": limit, "offset": offset}


# ---------------------------------------------------------------------------
# Send message
# ---------------------------------------------------------------------------

class SendMessageRequest(BaseModel):
    conversation_id: str
    text: str


@app.post("/send-message")
async def send_message(body: SendMessageRequest):
    # 1. Load conversation
    convo_res = (
        supabase.table("conversations")
        .select("id, salon_id, external_user_id, channel")
        .eq("id", body.conversation_id)
        .limit(1)
        .execute()
    )
    if not convo_res.data:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    convo = convo_res.data[0]

    if convo["channel"] != "whatsapp":
        raise HTTPException(status_code=400, detail="Only whatsapp channel is supported.")

    # 2. Load integration credentials
    integration_res = (
        supabase.table("integrations")
        .select("wa_phone_id, access_token")
        .eq("salon_id", convo["salon_id"])
        .eq("channel", "whatsapp")
        .limit(1)
        .execute()
    )
    if not integration_res.data:
        raise HTTPException(status_code=404, detail="No WhatsApp integration found for salon.")
    integration = integration_res.data[0]

    # 3. Call WhatsApp Cloud API
    wa_response = await _send_whatsapp_text(
        wa_phone_id  = integration["wa_phone_id"],
        access_token = integration["access_token"],
        to           = convo["external_user_id"],
        text         = body.text,
    )
    external_message_id = (wa_response.get("messages") or [{}])[0].get("id")

    # 4. Persist outbound message
    now = datetime.now(timezone.utc).isoformat()
    supabase.table("messages").insert({
        "conversation_id":     body.conversation_id,
        "direction":           "outbound",
        "external_message_id": external_message_id,
        "message_type":        "text",
        "text_content":        body.text,
        "payload":             wa_response,
        "status":              "sent",
        "sent_at":             now,
    }).execute()

    supabase.table("conversations").update({
        "last_message_at": now,
    }).eq("id", body.conversation_id).execute()

    return {"sent": True, "external_message_id": external_message_id}


# ---------------------------------------------------------------------------
# WhatsApp Cloud API helper
# ---------------------------------------------------------------------------

WA_API_BASE = "https://graph.facebook.com/v19.0"


async def _send_whatsapp_text(
    wa_phone_id: str,
    access_token: str,
    to: str,
    text: str,
) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{WA_API_BASE}/{wa_phone_id}/messages",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "to":   to,
                "type": "text",
                "text": {"body": text},
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Internal: payload processing
# ---------------------------------------------------------------------------

def get_salon_id_by_phone_number_id(phone_number_id: str) -> str | None:
    result = (
        supabase.table("integrations")
        .select("salon_id")
        .eq("channel", "whatsapp")
        .eq("external_phone_number_id", phone_number_id)
        .limit(1)
        .execute()
    )
    return result.data[0]["salon_id"] if result.data else None


def get_or_create_conversation(salon_id: str, external_user_id: str, channel: str) -> str:
    if not external_user_id:
        raise ValueError("Incoming message is missing external_user_id.")

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
            "salon_id":        salon_id,
            "external_user_id": external_user_id,
            "channel":         channel,
            "last_message_at": datetime.now(timezone.utc).isoformat(),
        })
        .execute()
    )
    rows = created.data or []
    if rows:
        return rows[0]["id"]

    # Race condition: another request created it first
    fallback = (
        supabase.table("conversations")
        .select("id")
        .eq("salon_id", salon_id)
        .eq("external_user_id", external_user_id)
        .eq("channel", channel)
        .limit(1)
        .execute()
    )
    if fallback.data:
        return fallback.data[0]["id"]

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

    external_user_id    = message.get("from")
    external_message_id = message.get("id")
    message_type        = message.get("type")
    timestamp           = message.get("timestamp")

    if not external_message_id:
        raise ValueError("Incoming message is missing external message id.")
    if not message_type:
        raise ValueError("Incoming message is missing message type.")

    # Idempotency — Meta retries deliver the same message_id
    duplicate = (
        supabase.table("messages")
        .select("id")
        .eq("external_message_id", external_message_id)
        .limit(1)
        .execute()
    )
    if duplicate.data:
        print(f"Duplicate inbound message skipped: {external_message_id}")
        return

    text_content = None
    if message_type == "text":
        text_content = message.get("text", {}).get("body")

    conversation_id = get_or_create_conversation(
        salon_id=salon_id,
        external_user_id=external_user_id,
        channel="whatsapp",
    )

    supabase.table("messages").insert({
        "conversation_id":     conversation_id,
        "direction":           "inbound",
        "external_message_id": external_message_id,
        "message_type":        message_type,
        "text_content":        text_content,
        "payload":             message,
        "status":              "received",
        "sent_at":             parse_message_timestamp(timestamp),
    }).execute()

    supabase.table("conversations").update({
        "last_message_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", conversation_id).execute()


def process_whatsapp_payload(payload: dict) -> None:
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value    = change.get("value", {})
            metadata = value.get("metadata", {})
            phone_number_id = metadata.get("phone_number_id")
            if not phone_number_id:
                continue
            for message in value.get("messages", []):
                process_inbound_message(phone_number_id, message)