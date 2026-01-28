import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import List
import uvicorn
import json
import redis

from stream_simulator import StreamSimulator
from fraud_engine import FraudEngine
from models import Transaction
from sales_poller import SalesPoller

app = FastAPI(title="Retail Trust & Security Backend")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Allow all for demo
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# WebSocket Connection Manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception as e:
                print(f"Error broadcasting to client: {e}")

manager = ConnectionManager()

# Global instances
simulator = StreamSimulator()

async def broadcast_update(type: str, data: any):
    """Callback for FraudEngine to send updates to frontend"""
    message = {
        "type": type,
        "data": data.model_dump(mode='json')
    }
    await manager.broadcast(json.dumps(message))

fraud_engine = FraudEngine(update_callback=broadcast_update)

async def stream_consumer():
    """Background task to consume events from simulator and feed to engine"""
    async for stream_type, event in simulator.run():
        if stream_type == "VAS":
            await fraud_engine.process_vas(event)
        elif stream_type == "POS":
            await fraud_engine.process_pos(event)

@app.on_event("startup")
async def startup_event():
    # Start the simulator loop in background
    asyncio.create_task(stream_consumer())
    
    # Start the sales polling loop in background
    poller = SalesPoller()
    asyncio.create_task(poller.start_polling())

@app.on_event("shutdown")
def shutdown_event():
    simulator.stop()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive, maybe handle client messages later
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.post("/api/admin/validate")
async def validate_transaction(transaction_id: str, decision: str, notes: str = ""):
    print(f"Admin Decision: {transaction_id} -> {decision} ({notes})")
    
    # Broadcast the update back to UI to update the table row
    # In a real app we would update the DB here.
    # We construct a partial update message
    update_data = {
        "id": transaction_id,
        "status": decision,
        "notes": notes
    }
    
    message = {
        "type": "TRANSACTION_UPDATE",
        "data": update_data
    }
    await manager.broadcast(json.dumps(message))
    
    return {"status": "success"}
    
@app.get("/api/sales-stream")
async def get_sales_stream(count: int = 10):
    """
    Fetch the latest sales data published to Redis Stream.
    """
    try:
        r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
        # Read last 'count' entries from the stream "sales_stream"
        # xrevrange returns items in reverse order (newest first)
        stream_data = r.xrevrange("sales_stream", count=count)
        
        results = []
        for message_id, data in stream_data:
            results.append({
                "stream_id": message_id,
                "data": data
            })
            
        return {"status": "success", "count": len(results), "data": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
