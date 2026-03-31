import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
from dotenv import load_dotenv

load_dotenv()
app = FastAPI()

VERIFY_TOKEN=os.getenv("META_VERIFY_TOKEN")

@app.get("/")
def root():
    return {"status": "ok"}

@app.get("/webhooks/meta")
async def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(content=challenge or "", status_code=200)
    
    raise HTTPException(status_code=403, detail="Verification Failed.")

@app.post("/webhooks/meta")
async def receive_webhook(request: Request):
    data = await request.json()
    print("Incoming webhook: ", data)
    return JSONResponse({"received": True})
