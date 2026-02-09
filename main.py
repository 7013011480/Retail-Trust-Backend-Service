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
from event_logger import EventLogger

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
event_logger = EventLogger()

async def broadcast_update(type: str, data: any):
    """Callback for FraudEngine to send updates to frontend"""
    message = {
        "type": type,
        "data": data.model_dump(mode='json')
    }
    await manager.broadcast(json.dumps(message))

async def delete_event_from_stream(stream_name: str, message_id: str):
    """Callback to delete processed events from Redis Stream"""
    try:
        if redis_client:
            redis_client.xdel(stream_name, message_id)
            # print(f"Deleted {message_id} from {stream_name}")
    except Exception as e:
        print(f"Error deleting from stream {stream_name}: {e}")

fraud_engine = FraudEngine(update_callback=broadcast_update, delete_callback=delete_event_from_stream)


# Global Redis Client
try:
    redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
except Exception as e:
    print(f"Failed to connect to Redis: {e}")
    redis_client = None

async def stream_consumer():
    """Background task to consume events from simulator and feed to engine"""
    async for stream_type, event in simulator.run():
        msg_id = None
        # Publish to Redis Streams
        try:
            if redis_client:
                # Add timestamp for ordering if needed, or rely on Redis ID
                if stream_type == "VAS":
                    data = event.model_dump(mode='json')
                    for k, v in data.items():
                        if isinstance(v, bool):
                            data[k] = str(v).lower()
                    msg_id = redis_client.xadd("vas_stream", data)
                    
                    # Log to File
                    event_with_id = {"stream_id": msg_id, "data": data}
                    event_logger.log_event("VAS", event_with_id)
                    
                    # Broadcast RAW Event
                    await broadcast_update("EVENT_VAS", event)
                    
                elif stream_type == "POS":
                    data = event.model_dump(mode='json')
                    for k, v in data.items():
                        if isinstance(v, bool):
                            data[k] = str(v).lower()
                    msg_id = redis_client.xadd("sales_stream", data) # changed pos_stream to sales_stream to match other files? NO, earlier code used pos_stream. Wait.
                    # verify_deletion.py uses "sales_stream" for POS.
                    # main.py get_stream_data checks ["vas_stream", "pos_stream", "sales_stream"]
                    # simulator emits "POS".
                    # Let's align with verify_deletion.py and use "sales_stream" for POS data if that's the convention.
                    # But wait, original code in main.py used "pos_stream" at line 85: redis_client.xadd("pos_stream", data)
                    # sales_poller.py uses "sales_stream".
                    # verify_deletion.py uses "sales_stream" for pos_stream deletion.
                    # I should probably stick to "pos_stream" if main.py was using it, OR switch to "sales_stream" if that's the unified stream.
                    # Given sales_poller uses sales_stream, maybe POS events should go there?
                    # Let's keep it as "pos_stream" for now to minimize side effects, UNLESS fraud_engine expects otherwise.
                    # FraudEngine delete_callback calls `await self.delete_callback("sales_stream", event_id)` in process_pos!
                    # So FraudEngine expects "sales_stream" for POS events!
                    # So I MUST use "sales_stream" here to ensure the delete callback works on the right stream.
                    
                    # Log to File
                    event_with_id = {"stream_id": msg_id, "data": data}
                    event_logger.log_event("POS", event_with_id)
                    
                    # Broadcast RAW Event
                    await broadcast_update("EVENT_POS", event)

        except Exception as e:
            print(f"Error publishing to Redis: {e}")

        if msg_id:
            if stream_type == "VAS":
                await fraud_engine.process_vas(event, msg_id)
            elif stream_type == "POS":
                await fraud_engine.process_pos(event, msg_id)

@app.on_event("startup")
async def startup_event():
    # Start the simulator loop in background
    asyncio.create_task(stream_consumer())
    
    # Start the sales polling loop in background
    # poller = SalesPoller()
    # asyncio.create_task(poller.start_polling())

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

@app.get("/api/streams/{stream_name}")
async def get_stream_data(stream_name: str, count: int = 20):
    """
    Fetch the latest data from a specific Redis Stream (vas_stream or pos_stream).
    """
    valid_streams = ["vas_stream", "pos_stream", "sales_stream"]
    if stream_name not in valid_streams:
        raise HTTPException(status_code=400, detail="Invalid stream name")

    try:
        # Read from EventLogger (File Persistence)
        results = event_logger.get_events(stream_name, count)
        return {"status": "success", "count": len(results), "data": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.get("/api/sales-stream")
async def get_sales_stream(count: int = 10):
    """
    Fetch the latest sales data published to Redis Stream.
    PROBABLY DEPRECATED in favor of universal endpoint above, but keeping for compatibility.
    """
    return await get_stream_data("sales_stream", count)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
