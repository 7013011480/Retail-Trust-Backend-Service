import asyncio
import random
import uuid
import time
from typing import List, Tuple, AsyncGenerator
from models import VASEvent, POSEvent, TransactionMode

class StreamSimulator:
    def __init__(self):
        # Configuration for stores and lanes
        self.stores = ["STR001", "STR002", "STR003"]
        self.lanes = [
            {"cam_id": "CAM-01", "window_id": "W1", "pos_id": "POS-01", "cashier": "Sarah Johnson"},
            {"cam_id": "CAM-01", "window_id": "W2", "pos_id": "POS-02", "cashier": "Michael Chen"},
            {"cam_id": "CAM-02", "window_id": "W1", "pos_id": "POS-03", "cashier": "Emily Rodriguez"},
            {"cam_id": "CAM-02", "window_id": "W2", "pos_id": "POS-04", "cashier": "James Williams"},
        ]
        self.running = False

    def _generate_ids(self) -> Tuple[str, dict]:
        store = random.choice(self.stores)
        lane = random.choice(self.lanes)
        seller_window_id = f"{store}_{lane['cam_id']}_{lane['window_id']}"
        return store, lane, seller_window_id

    def generate_scenario(self):
        """
        Generates a scenario which may produce 1 or 2 events (VAS and/or POS).
        Scenarios:
        1. Genuine Transaction (Matches)
        2. High Severity:
           - Payment Mismatch
           - Phantom Scan (VAS exists, POS missing)
        3. Medium Severity:
           - High Discount (> 20%)
           - Refund (> 0)
           - Bill Not Generated (ReceiptGenerationStatus=False)
        """
        scenario_type = random.choices(
            [
                "genuine", 
                "payment_mismatch", "phantom_scan", 
                "high_discount", "refund", "bill_not_generated"
            ],
            weights=[0.6, 0.05, 0.05, 0.1, 0.1, 0.1]
        )[0]

        store, lane, seller_window_id = self._generate_ids()
        session_id = f"{int(time.time()*1000)}_{seller_window_id}"
        bill_date = time.strftime("%Y-%m-%d")
        now = time.time()
        
        # Base attributes
        vas_mode = random.choice(list(TransactionMode))
        receipt_status = True
        
        if scenario_type == "bill_not_generated":
            receipt_status = False

        vas_event = VASEvent(
            StoreId=store,
            CamId=lane["cam_id"],
            SellerWindowId=seller_window_id,
            SessionId=session_id,
            BillDate=bill_date,
            SessionStart=now,
            SessionEnd=now + random.uniform(30, 120),
            ModeOfTransaction=vas_mode,
            ReceiptGenerationStatus=receipt_status
        )

        pos_event = None
        
        # Default POS values
        discount = 0.0
        refund = 0.0
        transaction_total = round(random.uniform(10.0, 500.0), 2)
        pos_mode = vas_mode

        if scenario_type == "high_discount":
            discount = random.uniform(21.0, 50.0)
        elif scenario_type == "refund":
            refund = random.uniform(10.0, 100.0)
            transaction_total = 0.0 # Or negative? Usually refunds are separate transactions but let's just flag the amount.
        elif scenario_type == "payment_mismatch":
            available_modes = list(TransactionMode)
            if vas_mode in available_modes:
                available_modes.remove(vas_mode)
            pos_mode = random.choice(available_modes)

        if scenario_type != "phantom_scan":
            pos_event = POSEvent(
                StoreId=store,
                CashierName=lane["cashier"],
                POSId=lane["pos_id"],
                BillDate=bill_date,
                SessionTime=now + random.uniform(1, 5),
                ModeOfTransaction=pos_mode,
                TransactionTotal=transaction_total,
                DiscountPercent=round(discount, 2),
                RefundAmount=round(refund, 2)
            )

        return vas_event, pos_event

    async def run(self) -> AsyncGenerator[Tuple[str, dict], None]:
        self.running = True
        while self.running:
            vas, pos = self.generate_scenario()
            
            # Yield events with slight random delays to simulate real network conditions
            # We yield a tuple (type, data)
            
            yield ("VAS", vas)
            
            if pos:
                # Simulate POS arriving slightly later or earlier
                await asyncio.sleep(random.uniform(0.1, 0.5))
                yield ("POS", pos)
            
            # Wait before next transaction
            await asyncio.sleep(random.uniform(2, 5))

    def stop(self):
        self.running = False
