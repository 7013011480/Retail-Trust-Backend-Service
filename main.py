import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Set
import uvicorn
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from datetime import datetime, timedelta, timezone
from fraud_engine import FraudEngine
from models import Transaction, VASEvent, POSEvent, TransactionStatus
from sales_poller import SalesPoller

IST = timezone(timedelta(hours=5, minutes=30))


app = FastAPI(title="Retail Trust & Security Backend")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

async def broadcast_update(type: str, data: any):
    """Callback for FraudEngine to send updates to frontend"""
    message = {
        "type": type,
        "data": data.model_dump(mode='json')
    }
    await manager.broadcast(json.dumps(message))

# Synchronization for POS file access
pos_lock = asyncio.Lock()
pos_file_path = "pos_data.json"
vas_file_path = "vas_data.json"

# Initialize FraudEngine with lock
fraud_engine = FraudEngine(update_callback=broadcast_update, pos_lock=pos_lock)

async def scheduled_data_processor():
    """
    Background task to process VAS and POS data every 2 minutes.
    """
    print("Starting Scheduled Data Processor...")
    while True:
        try:
            # 1. Process VAS
            await fraud_engine.run_vas_batch_process()
            
            # 2. Process POS
            await fraud_engine.run_pos_batch_process()
            
        except Exception as e:
            print(f"Error in scheduled data processor: {e}")
        
        # Wait 2 minutes
        await asyncio.sleep(120)

async def scheduled_stream_broadcaster():
    """
    Background task to broadcast the full content of VAS and POS data files every 30 seconds.
    """
    print("Starting Scheduled Stream Broadcaster...")
    while True:
        try:
            # Broadcast VAS Data
            vas_data = []
            if os.path.exists(vas_file_path):
                with open(vas_file_path, "r") as f:
                    content = f.read()
                    if content:
                        vas_data = json.loads(content)
            
            await manager.broadcast(json.dumps({
                "type": "RAW_VAS_DATA",
                "data": vas_data
            }))

            # Broadcast POS Data
            pos_data = []
            if os.path.exists(pos_file_path):
                with open(pos_file_path, "r") as f:
                    content = f.read()
                    if content:
                        pos_data = json.loads(content)
            
            await manager.broadcast(json.dumps({
                "type": "RAW_POS_DATA",
                "data": pos_data
            }))
            
        except Exception as e:
            print(f"Error in stream broadcaster: {e}")
        
        # Wait 30 seconds
        await asyncio.sleep(30)

@app.on_event("startup")
async def startup_event():
    # Start the sales polling loop in background
    poller = SalesPoller(file_lock=pos_lock, storage_path=pos_file_path)
    asyncio.create_task(poller.start_polling())
    
    # Start scheduled processor
    asyncio.create_task(scheduled_data_processor())

    # Start stream broadcaster
    asyncio.create_task(scheduled_stream_broadcaster())

@app.on_event("shutdown")
def shutdown_event():
    pass

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


@app.get("/api/history")
async def get_historical_data(days: int = 10):
    """Fetch historical POS data from the API and classify with POS-only rules."""
    poller = SalesPoller(storage_path=pos_file_path)
    result = await poller.fetch_historical(days=days)
    events = result["events"]
    raw_bills = result["raw_bills"]

    transactions = []
    bills_map = {}

    for i, (event, bill) in enumerate(zip(events, raw_bills)):
        bill_no = event.get("billNo", str(i))
        txn_id = f"TXN-{bill_no}"

        triggered_rules = []
        if event.get("DiscountPercent", 0) > 20:
            triggered_rules.append(f"High Discount ({event['DiscountPercent']}%)")
        if event.get("RefundAmount", 0) > 0:
            triggered_rules.append(f"Refund Processed (Rs.{event['RefundAmount']})")
        if event.get("IsComplementary") == "Yes":
            triggered_rules.append("Complementary Order")

        risk_level = "Medium" if triggered_rules else "Low"
        status = "suspicious" if triggered_rules else "genuine"

        session_time = event.get("SessionTime", "")
        try:
            ts = datetime.strptime(session_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST).isoformat()
        except:
            ts = datetime.now(IST).isoformat()

        cam_id = event.get("SellerWindowId", "Unknown")

        transactions.append({
            "id": txn_id,
            "shop_id": event.get("StoreId", "Unknown"),
            "cam_id": cam_id,
            "pos_id": event.get("POSId", "Unknown"),
            "cashier_name": event.get("CashierName", "Unknown"),
            "timestamp": ts,
            "transaction_total": event.get("TransactionTotal", 0),
            "risk_level": risk_level,
            "triggered_rules": triggered_rules if triggered_rules else None,
            "status": status,
            "fraud_category": triggered_rules[0] if triggered_rules else None,
        })
        bills_map[txn_id] = bill

    # Sort latest first
    transactions.sort(key=lambda t: t["timestamp"], reverse=True)

    return {
        "transactions": transactions,
        "bills_map": bills_map
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
