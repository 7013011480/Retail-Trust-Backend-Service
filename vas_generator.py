import asyncio
import json
import os
import time
import uuid
import random

# Configuration matching FraudEngine
STORES = ["STR001", "STR002", "STR003", "NSCIN8227"]
LANES = [
    {"cam_id": "CAM-01", "window_id": "W1", "pos_id": "POS-01"},
    {"cam_id": "CAM-01", "window_id": "W2", "pos_id": "POS-02"},
    {"cam_id": "CAM-02", "window_id": "W1", "pos_id": "POS-03"},
    {"cam_id": "CAM-02", "window_id": "W2", "pos_id": "POS4"},
]

# Reverse map: POSId -> Lane Details
POS_MAP = {l["pos_id"]: l for l in LANES}

POS_FILE = "pos_data.json"
VAS_FILE = "vas_data.json"
METADATA_FILE = "pos_processed_metadata.json"

async def vas_generator():
    print("Starting VAS Generator Service...")
    
    # Load processed bills metadata
    processed_bills = set()
    if os.path.exists(METADATA_FILE):
        try:
            with open(METADATA_FILE, "r") as f:
                metadata = json.load(f)
                processed_bills = set(metadata.get("processed_bills", []))
                print(f"[VAS Generator] Loaded {len(processed_bills)} processed bills from metadata")
        except Exception as e:
            print(f"[VAS Generator] Error reading metadata: {e}")
    
    while True:
        try:
            # 1. Read POS Data
            pos_data = []
            if os.path.exists(POS_FILE):
                try:
                    with open(POS_FILE, "r") as f:
                        content = f.read()
                        if content:
                            pos_data = json.loads(content)
                            print(f"[VAS Generator] Read {len(pos_data)} events from {POS_FILE}")
                except Exception as e:
                    print(f"[VAS Generator] Error reading POS file: {e}")
            
            if not pos_data:
                print("[VAS Generator] No POS data found. Waiting...")
                await asyncio.sleep(1)
                continue

            # 2. Read existing VAS Data
            vas_data = []
            if os.path.exists(VAS_FILE):
                try:
                    with open(VAS_FILE, "r") as f:
                        content = f.read()
                        if content:
                            vas_data = json.loads(content)
                            print(f"[VAS Generator] Read {len(vas_data)} events from {VAS_FILE}")
                except Exception as e:
                    print(f"[VAS Generator] Error reading VAS file: {e}")

            # 3. Find NEW Unmatched POS Events
            new_vas_events = []
            newly_processed_bills = []
            
            for pos_event in pos_data:
                store_id = pos_event.get("StoreId")
                pos_id = pos_event.get("POSId")
                session_time = pos_event.get("SessionTime")
                bill_no = pos_event.get("billNo")
                
                # Skip if already processed
                if bill_no in processed_bills:
                    print(f"[VAS Generator] Skipping already processed bill: {bill_no}")
                    continue
                
                # Check if this POS event is supported in our mapping
                if pos_id not in POS_MAP:
                    # Can't generate valid VAS for unknown POS
                    print(f"[VAS Generator] Skipping unknown POS ID: {pos_id}")
                    continue
                
                lane = POS_MAP[pos_id]
                expected_seller_window = f"{store_id}_{lane['cam_id']}_{lane['window_id']}"
                
                # Check if corresponding VAS exists in current vas_data
                match_exists = False
                for v in vas_data:
                    # Check Store, Window
                    if v.get("StoreId") == store_id and v.get("SellerWindowId") == expected_seller_window:
                        # Check time
                        v_end = v.get("SessionEnd")
                        if abs(v_end - session_time) < 5: # 5 seconds tolerance means it's likely the pair we generated
                            match_exists = True
                            print(f"[VAS Generator] Match already exists for POS {pos_id} at {session_time}")
                            break
                            
                if not match_exists:
                    # GENERATE VAS EVENT
                    print(f"[VAS Generator] Generating VAS event for NEW POS: {pos_id} (Bill: {bill_no}) at {session_time}")
                    
                    vas_event = {
                        "StoreId": store_id,
                        "CamId": lane["cam_id"],
                        "SellerWindowId": expected_seller_window,
                        "SessionId": f"VAS-{uuid.uuid4().hex[:8]}",
                        "BillDate": pos_event.get("BillDate"),
                        # VAS session usually ends around POS time (payment)
                        # Start time was a bit before
                        "SessionStart": session_time - random.uniform(30, 120),
                        "SessionEnd": session_time, # Align exactly or slightly off
                        "ModeOfTransaction": pos_event.get("ModeOfTransaction"),
                        "ReceiptGenerationStatus": True,
                    }
                    new_vas_events.append(vas_event)
                    newly_processed_bills.append(bill_no)

            # 4. Append New Events
            if new_vas_events:
                # Re-read VAS file to reduce race condition risk (simple append)
                current_vas = []
                if os.path.exists(VAS_FILE):
                     with open(VAS_FILE, "r") as f:
                        try:
                            current_vas = json.load(f)
                        except: pass
                
                current_vas.extend(new_vas_events)
                
                with open(VAS_FILE, "w") as f:
                    json.dump(current_vas, f, indent=4)
                
                print(f"Generated {len(new_vas_events)} VAS events.")
                
                # Update metadata
                processed_bills.update(newly_processed_bills)
                with open(METADATA_FILE, "w") as f:
                    json.dump({"processed_bills": list(processed_bills)}, f, indent=4)
                print(f"[VAS Generator] Updated metadata with {len(newly_processed_bills)} new bills")

        except Exception as e:
            print(f"Error in VAS Generator: {e}")
            
        await asyncio.sleep(2)

if __name__ == "__main__":
    try:
        asyncio.run(vas_generator())
    except KeyboardInterrupt:
        print("Stopping VAS Generator...")
