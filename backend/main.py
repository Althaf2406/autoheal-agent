from fastapi import FastAPI, Request

app = FastAPI(title="AutoHeal Webhook Backend")

@app.post("/webhook")
async def receive_webhook(request: Request):
    """
    Endpoint untuk menerima payload JSON dari GitHub Actions.
    """
    # Menerima raw JSON payload
    payload = await request.json()
    
    # Mencetak payload ke terminal (untuk keperluan debug/log awal)
    print("=== Webhook Received ===")
    print(payload)
    print("========================")
    
    # Mengembalikan response Success
    return {"status": "Success", "message": "Webhook received successfully"}
