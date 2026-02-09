import redis
import time
import random
import json
import uuid
from typing import List
from models import VASEvent, TransactionMode

class VASSimulator:
    def __init__(self):
        self.client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
        self.stores = ["STR001", "STR002", "STR003"]
        self.lanes = [
            {"cam_id": "CAM-01", "window_id": "W1", "pos_id": "POS-01", "cashier": "Sarah Johnson"},
            {"cam_id": "CAM-01", "window_id": "W2", "pos_id": "POS-02", "cashier": "Michael Chen"},
            {"cam_id": "CAM-02", "window_id": "W1", "pos_id": "POS-03", "cashier": "Emily Rodriguez"},
            {"cam_id": "CAM-02", "window_id": "W2", "pos_id": "POS-04", "cashier": "James Williams"},
        ]
    
    def _generate_ids(self):
        store = random.choice(self.stores)
        lane = random.choice(self.lanes)
        seller_window_id = f"{store}_{lane['cam_id']}_{lane['window_id']}"
        return store, lane, seller_window_id

    def generate_event(self) -> VASEvent:
        store, lane, seller_window_id = self._generate_ids()
        now = time.time()
        session_id = f"{int(now*1000)}_{seller_window_id}"
        bill_date = time.strftime("%Y-%m-%d")
        
        event = VASEvent(
            StoreId=store,
            CamId=lane["cam_id"],
            SellerWindowId=seller_window_id,
            SessionId=session_id,
            BillDate=bill_date,
            SessionStart=now,
            SessionEnd=now + random.uniform(30, 120),
            ModeOfTransaction=random.choice(list(TransactionMode)),
            ReceiptGenerationStatus=random.choice([True, True, True, False]) # Mostly true
        )
        return event

    def run(self, count: int = -1, delay: float = 1.0):
        print(f"Starting VAS Simulator (infinite loop if count=-1)...")
        generated = 0
        try:
            while count == -1 or generated < count:
                event = self.generate_event()
                
                # Publish to Redis
                try:
                    data = event.model_dump(mode='json')
                    # Redis xadd doesn't support bool, convert to str or int
                    for k, v in data.items():
                        if isinstance(v, bool):
                            data[k] = str(v).lower()
                            
                    self.client.xadd("vas_stream", data)
                    print(f"[{time.strftime('%H:%M:%S')}] Published VAS Event: {event.SessionId} ({event.ModeOfTransaction})")
                except Exception as e:
                    print(f"Error publishing to Redis: {e}")
                
                generated += 1
                time.sleep(random.uniform(delay * 0.5, delay * 1.5))
                
        except KeyboardInterrupt:
            print("\nSimulator stopped by user.")

if __name__ == "__main__":
    sim = VASSimulator()
    sim.run(delay=2.0) # Run indefinitely with ~2s delay
