import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import List
import uvicorn
import json
import redis
import os
import time
import glob
from concurrent.futures import ThreadPoolExecutor

# from stream_simulator import StreamSimulator # Removed
from fraud_engine import FraudEngine
from models import Transaction, VASEvent, POSEvent
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
# simulator = StreamSimulator() # Removed

async def broadcast_update(type: str, data: any):
    """Callback for FraudEngine to send updates to frontend"""
    message = {
        "type": type,
        "data": data.model_dump(mode='json')
    }
    await manager.broadcast(json.dumps(message))

fraud_engine = FraudEngine(update_callback=broadcast_update)

# Synchronization for POS file access
pos_lock = asyncio.Lock()
pos_file_path = "pos_data.json"

async def file_orchestrator():
    """Background task to consumer events from files"""
    print("Starting File Orchestrator...")
    vas_file_path = "vas_data.json"
    vas_metadata_path = "vas_metadata.json"
    processed_pos_file = "sales_data.json"
    processed_vas_file = "vas_event.json"
    
    # Track previous VAS events to detect new arrivals
    previous_vas_session_ids = set()

    while True:
        try:
            # We need to lock both or at least POS to ensure consistency with Poller
            # Ideally we lock POS when reading/writing it.
            # We will read both, match, then write all 4 files safely.
            
            matched_indices_pos = set()
            matched_indices_vas = set()
            
            matched_pos_events = []
            matched_vas_events = []
            
            # 1. READ DATA
            pos_data_list = []
            vas_data_list = []
            
            # Read POS (Shared with Poller)
            async with pos_lock:
                if os.path.exists(pos_file_path):
                    try:
                        with open(pos_file_path, "r") as f:
                            content = f.read()
                            if content:
                                pos_data_list = json.loads(content)
                                # print(f"[Orchestrator] Read {len(pos_data_list)} POS events.")
                    except Exception as e:
                        print(f"[Orchestrator] Error reading POS file: {e}")
            
            # Read VAS (User manual input)
            # We don't have a lock for VAS, assuming single writer or manual append
            if os.path.exists(vas_file_path):
                try:
                    with open(vas_file_path, "r") as f:
                        content = f.read()
                        if content:
                            vas_data_list = json.loads(content)
                            # print(f"[Orchestrator] Read {len(vas_data_list)} VAS events.")
                except Exception as e:
                    print(f"[Orchestrator] Error reading VAS file: {e}")
            
            # 2. TRACK NEW VAS EVENTS
            # Load metadata
            vas_metadata = {}
            if os.path.exists(vas_metadata_path):
                try:
                    with open(vas_metadata_path, "r") as f:
                        content = f.read()
                        if content:
                            vas_metadata = json.loads(content)
                except Exception as e:
                    print(f"[Orchestrator] Error reading VAS metadata: {e}")
            
            # Detect new VAS events
            current_vas_session_ids = {v.get("SessionId") for v in vas_data_list if v.get("SessionId")}
            new_vas_ids = current_vas_session_ids - previous_vas_session_ids
            
            if new_vas_ids:
                current_time = time.time()
                for session_id in new_vas_ids:
                    vas_metadata[session_id] = current_time
                    print(f"[Orchestrator] New VAS event detected: {session_id}, will be eligible for matching after 60 seconds")
                
                # Save metadata
                with open(vas_metadata_path, "w") as f:
                    json.dump(vas_metadata, f, indent=4)
            
            previous_vas_session_ids = current_vas_session_ids
            
            # 3. FILTER VAS EVENTS BY WAIT TIME
            current_time = time.time()
            eligible_vas_data = []
            waiting_vas_count = 0
            
            for vas_event in vas_data_list:
                session_id = vas_event.get("SessionId")
                arrival_time = vas_metadata.get(session_id)
                
                if arrival_time is None:
                    # No metadata, assume it's old enough (backward compatibility)
                    eligible_vas_data.append(vas_event)
                elif current_time - arrival_time >= 60:
                    # Waited long enough
                    eligible_vas_data.append(vas_event)
                else:
                    # Still waiting
                    waiting_vas_count += 1
                    wait_remaining = 60 - (current_time - arrival_time)
                    print(f"[Orchestrator] VAS event {session_id} waiting... {wait_remaining:.0f}s remaining")
            
            if waiting_vas_count > 0:
                print(f"[Orchestrator] {waiting_vas_count} VAS event(s) still in wait period")
            
            # Use eligible VAS events for matching
            vas_data_list = eligible_vas_data
            
            if not pos_data_list or not vas_data_list:
                # print("[Orchestrator] Waiting for data...")
                await asyncio.sleep(1)
                continue

            print(f"[Orchestrator] Matching {len(pos_data_list)} POS events against {len(vas_data_list)} VAS events...")

            # 4. MATCHING LOGIC
            # Simple O(N*M) match for now, or use dictionary for O(N)
            # Match criteria: StoreId + POSId + Time Window?
            # User said: "compare... if a match is found do the relavent calculations"
            
            # Let's index POS data by (StoreId, POSId) for faster lookup
            # But duplicate keys might exist (multiple txns).
            
            # Iterating VAS to find match in POS
            for v_idx, vas_item in enumerate(vas_data_list):
                try:
                    v_store = vas_item.get("StoreId")
                    # VAS Event might have SellerWindowId, need mapping?
                    # "process_vas_with_lookup" in fraud_engine used mapping.
                    # Let's see if VAS Item has POSId directly or if we need mapping.
                    # Assuming VAS input follows VASEvent model which has SellerWindowId
                    
                    seller_window_id = vas_item.get("SellerWindowId")
                    # We need to map this to POSId
                    # Constructing mapping again locally or using fraud_engine's mapping
                    # Ideally reuse fraud_engine
                    
                    # For this implementation, I will assume fraud_engine is valid instance
                    expected_pos_id = fraud_engine.mapping.get(seller_window_id)
                    
                    if not expected_pos_id:
                        # Maybe VAS data already has POSId?
                        expected_pos_id = vas_item.get("POSId")
                    
                    if not expected_pos_id:
                        print(f"[Orchestrator] Could not resolve POS ID for VAS Item: {seller_window_id}")
                        continue

                    # Search matching POS
                    match_found_idx = -1
                    for p_idx, pos_item in enumerate(pos_data_list):
                        if p_idx in matched_indices_pos:
                            continue
                        
                        p_store = pos_item.get("StoreId")
                        p_id = pos_item.get("POSId")
                        
                        if p_store == v_store and p_id == expected_pos_id:
                            # Match found! 
                            # (Ignoring time for now as user just said "if a match is found")
                            # But we should probably check time roughly?
                            # Let's trust strict Store+POS match corresponds to the event queue head for now
                            # or strictly speaking, we should match timestamps.
                            # "relavent calculations" -> FraudEngine logic
                            
                            print(f"[Orchestrator] Match Found! POS {p_id} (Idx {p_idx}) <-> VAS {seller_window_id} (Idx {v_idx})")
                            match_found_idx = p_idx
                            break
                    
                    if match_found_idx != -1:
                        matched_indices_vas.add(v_idx)
                        matched_indices_pos.add(match_found_idx)
                        
                        # Prepare for "Relevant Calculations"
                        vas_obj = VASEvent(**vas_item)
                        pos_obj = POSEvent(**pos_data_list[match_found_idx])
                        
                        # We can call fraud engine to process/alert
                        # But also need to prepare data for storage
                        # User: "put it in sales_data.json and vas_event.json"
                        
                        # Perform calculations
                        print("[Orchestrator] Running Fraud Analysis on pair...")
                        await fraud_engine._analyze_pair(vas_obj, pos_obj)
                        
                        matched_vas_events.append(vas_item)
                        matched_pos_events.append(pos_data_list[match_found_idx])

                except Exception as e:
                    print(f"[Orchestrator] Error matching item {v_idx}: {e}")

            # 5. WRITE & UPDATE
            if matched_indices_pos or matched_indices_vas:
                
                # A. Append to sales_data.json
                if matched_pos_events:
                    print(f"[Orchestrator] Appending {len(matched_pos_events)} events to {processed_pos_file}")
                    existing_sales = []
                    if os.path.exists(processed_pos_file):
                        try:
                            with open(processed_pos_file, "r") as f:
                                existing_sales = json.load(f)
                        except: pass
                    
                    existing_sales.extend(matched_pos_events)
                    with open(processed_pos_file, "w") as f:
                        json.dump(existing_sales, f, indent=4)
                
                # B. Append to vas_event.json
                if matched_vas_events:
                    print(f"[Orchestrator] Appending {len(matched_vas_events)} events to {processed_vas_file}")
                    existing_vas = []
                    if os.path.exists(processed_vas_file):
                        try:
                            with open(processed_vas_file, "r") as f:
                                existing_vas = json.load(f)
                        except: pass
                    
                    existing_vas.extend(matched_vas_events)
                    with open(processed_vas_file, "w") as f:
                        json.dump(existing_vas, f, indent=4)

                # C. Remove from pos_data.json (With Lock)
                async with pos_lock:
                    # Re-read to be safe? 
                    # If poller added something, indices might be shifted?
                    # YES. This is dangerous.
                    # We should identify by ID or object content, not index.
                    # Or hold lock for the entire duration (reading, matching, writing).
                    # Holding lock for matching is safer.
                    # Refactoring: The entire logic above should be under lock provided it's fast.
                    
                    # Since I cannot easily wrap the whole block now without massive indent change,
                    # I will accept the risk OR re-read and remove by value.
                    # Given sample size, I will remove by value/ID if possible.
                    # But I don't have unique IDs on raw inputs easily.
                    # I will rewrite the file filtering out the *exact objects* I matched.
                    
                    # Re-read fresh
                    fresh_pos = []
                    if os.path.exists(pos_file_path):
                         with open(pos_file_path, "r") as f:
                            fresh_pos = json.loads(f.read())
                    
                    # Filter
                    # We matched `matched_pos_events`.
                    # We need to remove one instance of each from fresh_pos.
                    remaining_pos = []
                    temp_matched = list(matched_pos_events)
                    
                    for p in fresh_pos:
                        if p in temp_matched:
                            temp_matched.remove(p) # Remove one instance match
                        else:
                            remaining_pos.append(p)
                            
                    with open(pos_file_path, "w") as f:
                        json.dump(remaining_pos, f, indent=4)
                    print(f"[Orchestrator] Removed {len(matched_pos_events)} matched events from {pos_file_path}")

                # D. Remove from vas_data.json
                # Re-read fresh VAS
                fresh_vas = []
                if os.path.exists(vas_file_path):
                    with open(vas_file_path, "r") as f:
                        fresh_vas = json.loads(f.read())

                remaining_vas = []
                temp_matched_vas = list(matched_vas_events)
                for v in fresh_vas:
                    if v in temp_matched_vas:
                        temp_matched_vas.remove(v)
                    else:
                        remaining_vas.append(v)

                with open(vas_file_path, "w") as f:
                    json.dump(remaining_vas, f, indent=4)
                print(f"[Orchestrator] Removed {len(matched_vas_events)} matched events from {vas_file_path}")
                
                # E. Clean up metadata for matched VAS events
                for vas_event in matched_vas_events:
                    session_id = vas_event.get("SessionId")
                    if session_id in vas_metadata:
                        del vas_metadata[session_id]
                        print(f"[Orchestrator] Cleaned up metadata for {session_id}")
                
                with open(vas_metadata_path, "w") as f:
                    json.dump(vas_metadata, f, indent=4)

                print(f"[Orchestrator] Cycle Complete. Processed {len(matched_pos_events)} pairs.")

        except Exception as e:
            print(f"Error in file orchestrator: {e}")
        
        await asyncio.sleep(2) # Interval

@app.on_event("startup")
async def startup_event():
    # Start the sales polling loop in background
    poller = SalesPoller(file_lock=pos_lock, storage_path=pos_file_path)
    asyncio.create_task(poller.start_polling(interval_seconds=60))
    
    # Start file orchestrator
    asyncio.create_task(file_orchestrator())

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
