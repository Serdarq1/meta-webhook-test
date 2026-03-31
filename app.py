import os
import secrets
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
from dotenv import load_dotenv

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
    print("Incoming webhook: ", data)
    return JSONResponse({"received": True})
