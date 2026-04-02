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
from fraud_engine import FraudEngine, load_rule_config, RULE_CONFIG_FILE, SALES_DATA_FILE
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

# Persistence files
TRANSACTIONS_FILE = "transactions.jsonl"
ALERTS_FILE = "alerts.jsonl"

def append_jsonl(filepath: str, record: dict):
    """Append a single JSON record as a line to a JSONL file."""
    with open(filepath, "a") as f:
        f.write(json.dumps(record) + "\n")

def read_jsonl(filepath: str) -> list:
    """Read all records from a JSONL file."""
    records = []
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return records

async def broadcast_update(type: str, data: any):
    """Callback for FraudEngine to send updates to frontend. Also persists to JSONL."""
    record = data.model_dump(mode='json')
    message = {"type": type, "data": record}
    await manager.broadcast(json.dumps(message))

    # Persist
    if type == "NEW_TRANSACTION":
        append_jsonl(TRANSACTIONS_FILE, record)
    elif type == "NEW_ALERT":
        append_jsonl(ALERTS_FILE, record)

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

async def idle_pos_monitor():
    """
    Background task to detect idle POS terminals.
    Fires an alert if no transaction has been processed for longer than the configured threshold.
    """
    print("Starting Idle POS Monitor...")
    last_txn_time: dict[str, datetime] = {}
    alerted_idle: set[str] = set()

    # Load store names
    store_name_map = {}
    try:
        if os.path.exists("stores.json"):
            with open("stores.json", "r") as f:
                for s in json.load(f):
                    store_name_map[s["cin"]] = s.get("name", s["cin"])
    except:
        pass

    while True:
        try:
            config = load_rule_config()
            idle_minutes = config.get("idle_pos_minutes", 30)

            # Read processed POS events to find latest transaction per POS
            if os.path.exists(SALES_DATA_FILE):
                with open(SALES_DATA_FILE, "r") as f:
                    content = f.read()
                    if content:
                        events = json.loads(content)
                        for evt in events:
                            pos_key = f"{evt.get('StoreId', 'Unknown')}_{evt.get('POSId', 'Unknown')}"
                            try:
                                ts = datetime.strptime(evt.get("SessionTime", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
                                if pos_key not in last_txn_time or ts > last_txn_time[pos_key]:
                                    last_txn_time[pos_key] = ts
                            except:
                                pass

            # Also check pending POS data
            if os.path.exists(pos_file_path):
                with open(pos_file_path, "r") as f:
                    content = f.read()
                    if content:
                        events = json.loads(content)
                        for evt in events:
                            pos_key = f"{evt.get('StoreId', 'Unknown')}_{evt.get('POSId', 'Unknown')}"
                            try:
                                ts = datetime.strptime(evt.get("SessionTime", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
                                if pos_key not in last_txn_time or ts > last_txn_time[pos_key]:
                                    last_txn_time[pos_key] = ts
                            except:
                                pass

            now = datetime.now(IST)
            for pos_key, last_ts in last_txn_time.items():
                gap_minutes = (now - last_ts).total_seconds() / 60
                if gap_minutes >= idle_minutes and pos_key not in alerted_idle:
                    store_id, pos_id = pos_key.split("_", 1) if "_" in pos_key else (pos_key, "Unknown")
                    alert_record = {
                        "id": f"ALT-IDLE-{pos_key}-{int(now.timestamp())}",
                        "transaction_id": "N/A",
                        "shop_id": store_id,
                        "shop_name": store_name_map.get(store_id, store_id),
                        "cashier_name": "N/A",
                        "risk_level": "Medium",
                        "triggered_rules": [f"POS Idle for {int(gap_minutes)} minutes"],
                        "timestamp": now.isoformat(),
                        "status": "new",
                    }
                    await manager.broadcast(json.dumps({"type": "NEW_ALERT", "data": alert_record}))
                    append_jsonl(ALERTS_FILE, alert_record)
                    alerted_idle.add(pos_key)
                    print(f"[Idle Monitor] Alert: {pos_key} idle for {int(gap_minutes)} minutes")
                elif gap_minutes < idle_minutes and pos_key in alerted_idle:
                    # POS is active again, allow future alerts
                    alerted_idle.discard(pos_key)

        except Exception as e:
            print(f"Error in idle POS monitor: {e}")

        await asyncio.sleep(60)  # Check every minute


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

    # Start idle POS monitor
    asyncio.create_task(idle_pos_monitor())

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


@app.get("/api/stores")
async def get_stores():
    """Get list of configured stores."""
    try:
        if os.path.exists("stores.json"):
            with open("stores.json", "r") as f:
                return json.load(f)
    except:
        pass
    return []


@app.get("/api/config")
async def get_config():
    """Get current rule configuration thresholds."""
    return load_rule_config()


@app.post("/api/config")
async def update_config(config: dict):
    """Update rule configuration thresholds. Clears persisted data so next /api/history re-classifies."""
    current = load_rule_config()
    current.update(config)
    with open(RULE_CONFIG_FILE, "w") as f:
        json.dump(current, f, indent=4)
    # Clear persisted transactions so they get re-classified with new thresholds
    for f_path in [TRANSACTIONS_FILE, ALERTS_FILE, "bills_raw.jsonl"]:
        if os.path.exists(f_path):
            os.remove(f_path)
    print("[Config] Cleared persisted data for re-classification")
    return current


@app.get("/api/history")
async def get_historical_data(days: int = 5):
    """Fetch new POS data from the API since last saved transaction and classify."""
    # Find the latest timestamp in our persisted data
    existing = read_jsonl(TRANSACTIONS_FILE)
    last_ts = None
    if existing:
        timestamps = [r.get("timestamp", "") for r in existing if r.get("timestamp")]
        if timestamps:
            last_ts = max(timestamps)

    poller = SalesPoller(storage_path=pos_file_path)

    if last_ts:
        # Only fetch from last timestamp onwards
        try:
            last_dt = datetime.fromisoformat(last_ts)
            days_since = max(1, (datetime.now(IST) - last_dt).days + 1)
            print(f"[History] Fetching {days_since} days since last transaction: {last_ts}")
            result = await poller.fetch_historical(days=days_since)
        except:
            result = await poller.fetch_historical(days=days)
    else:
        # No existing data, fetch full range
        result = await poller.fetch_historical(days=days)
    events = result["events"]
    raw_bills = result["raw_bills"]

    transactions = []
    bills_map = {}

    # Build store name lookup
    store_names = {}
    try:
        if os.path.exists("stores.json"):
            with open("stores.json", "r") as f:
                for s in json.load(f):
                    store_names[s["cin"]] = s.get("name", s["cin"])
    except:
        pass

    config = load_rule_config()

    for i, (event, bill) in enumerate(zip(events, raw_bills)):
        bill_no = event.get("billNo", str(i))
        txn_id = f"TXN-{bill_no}"

        triggered_rules = []

        # Rule: High Discount
        if event.get("DiscountPercent", 0) > config["discount_threshold_percent"]:
            triggered_rules.append(f"High Discount ({event['DiscountPercent']}%)")

        # Rule: Refund — for cash, only flag excess change; for non-cash, flag all
        pay_mode = str(event.get("ModeOfTransaction", "unknown")).lower()
        refund_amt = event.get("RefundAmount", 0)
        if refund_amt > config["refund_amount_threshold"]:
            if pay_mode == "cash":
                expected_change = max(0, event.get("PaymentReceived", 0) - event.get("BillAmount", 0))
                excess = refund_amt - expected_change
                if excess > 1:
                    triggered_rules.append(f"Excess Cash Return (Rs.{excess:.0f} over expected change)")
            else:
                triggered_rules.append(f"Refund Processed (Rs.{refund_amt})")

        # Rule: Complementary Order
        if event.get("IsComplementary") == "Yes":
            triggered_rules.append("Complementary Order")

        # Rule: Void/Cancelled
        if event.get("VoidReason"):
            triggered_rules.append(f"Void Transaction ({event['VoidReason']})")
        if event.get("CancelDate"):
            triggered_rules.append("Cancelled Transaction")

        # Rule: Negative Amount
        total = event.get("TransactionTotal", 0)
        if total < 0:
            triggered_rules.append(f"Negative Amount (Rs.{total})")

        # Rule: High Value
        if total > config["high_value_threshold"]:
            triggered_rules.append(f"High Value Transaction (Rs.{total})")

        # Rule: Bulk Purchase
        item_count = event.get("ItemCount", 0)
        if item_count > config["bulk_quantity_threshold"]:
            triggered_rules.append(f"Bulk Purchase ({item_count} items)")

        # Determine risk
        has_high = any(r for r in triggered_rules if "Void" in r or "Cancelled" in r or "Negative" in r)
        risk_level = "High" if has_high else ("Medium" if triggered_rules else "Low")
        status = "fraudulent" if risk_level == "High" else ("suspicious" if triggered_rules else "genuine")

        session_time = event.get("SessionTime", "")
        try:
            ts = datetime.strptime(session_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST).isoformat()
        except:
            ts = datetime.now(IST).isoformat()

        cam_id = event.get("SellerWindowId", "Unknown")
        shop_id = event.get("StoreId", "Unknown")

        transactions.append({
            "id": txn_id,
            "shop_id": shop_id,
            "shop_name": store_names.get(shop_id, shop_id),
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

    # Persist to JSONL (deduplicate by txn_id)
    existing_ids = {r.get("id") for r in read_jsonl(TRANSACTIONS_FILE)}
    new_count = 0
    for txn in transactions:
        if txn["id"] not in existing_ids:
            append_jsonl(TRANSACTIONS_FILE, txn)
            # Also persist raw bill
            if txn["id"] in bills_map:
                append_jsonl("bills_raw.jsonl", {"txn_id": txn["id"], "bill": bills_map[txn["id"]]})
            new_count += 1

    # Also generate alerts for flagged transactions
    existing_alert_ids = {r.get("id") for r in read_jsonl(ALERTS_FILE)}
    alert_count = 0
    for txn in transactions:
        if txn["risk_level"] != "Low" and txn.get("triggered_rules"):
            alert_id = f"ALT-{txn['id']}"
            if alert_id not in existing_alert_ids:
                alert_record = {
                    "id": alert_id,
                    "transaction_id": txn["id"],
                    "shop_id": txn["shop_id"],
                    "shop_name": txn.get("shop_name", txn["shop_id"]),
                    "cashier_name": txn.get("cashier_name", "Unknown"),
                    "risk_level": txn["risk_level"],
                    "triggered_rules": txn["triggered_rules"],
                    "timestamp": txn["timestamp"],
                    "status": "new",
                }
                append_jsonl(ALERTS_FILE, alert_record)
                alert_count += 1

    print(f"[History] Persisted {new_count} transactions, {alert_count} alerts")

    return {
        "transactions": transactions,
        "bills_map": bills_map
    }


@app.get("/api/transactions")
async def get_transactions():
    """Get all persisted transactions from local JSONL (no external API call)."""
    transactions = read_jsonl(TRANSACTIONS_FILE)
    transactions.sort(key=lambda t: t.get("timestamp", ""), reverse=True)

    bills_map = {}
    for record in read_jsonl("bills_raw.jsonl"):
        bills_map[record.get("txn_id", "")] = record.get("bill", {})

    return {
        "transactions": transactions,
        "bills_map": bills_map
    }


@app.get("/api/alerts")
async def get_alerts():
    """Get all persisted alerts from local JSONL."""
    alerts = read_jsonl(ALERTS_FILE)
    alerts.sort(key=lambda a: a.get("timestamp", ""), reverse=True)
    return alerts


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
