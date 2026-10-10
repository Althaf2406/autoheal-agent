from fastapi import FastAPI, Request
from backend.agent_researcher import run_researcher

app = FastAPI(title="AutoHeal Webhook Backend")

@app.post("/webhook")
async def receive_webhook(request: Request):
    """
    Endpoint untuk menerima payload JSON dari GitHub Actions.
    """
    # Menerima raw JSON payload
    payload = await request.json()
    
    print("=== Webhook Received ===")
    print(payload)
    print("========================")
    
    # Ambil pesan error dari payload (kita asumsikan ada properti 'error' atau 'error_log')
    error_message = payload.get("error_log") or payload.get("error") or str(payload)
    
    # Jalankan Agent Researcher
    analysis_result = run_researcher(error_message)
    
    # Mengembalikan response beserta hasil analisis sementara
    return {
        "status": "Success", 
        "message": "Webhook received successfully",
        "analysis": analysis_result
    }
