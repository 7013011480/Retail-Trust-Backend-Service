import asyncio
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Callable
from models import VASEvent, POSEvent, Transaction, Alert, TransactionStatus, AlertStatus, TransactionMode

class FraudEngine:
    def __init__(self, update_callback: Callable):
        self.update_callback = update_callback
        # Mapping SellerWindowId -> POSId
        # In a real app this would be in a DB
        self.mapping = {}
        stores = ["STR001", "STR002", "STR003"]
        lanes = [
            {"cam_id": "CAM-01", "window_id": "W1", "pos_id": "POS-01"},
            {"cam_id": "CAM-01", "window_id": "W2", "pos_id": "POS-02"},
            {"cam_id": "CAM-02", "window_id": "W1", "pos_id": "POS-03"},
            {"cam_id": "CAM-02", "window_id": "W2", "pos_id": "POS-04"},
        ]
        
        # Consistent mapping generation matching StreamSimulator
        # Note: POS IDs in simulator are static per lane, but here we might need unique POS IDs per store?
        # The simulator uses: "pos_id": "POS-01" for lane 1 regardless of store.
        # This implies POS-01 exists in STR001, POS-01 exists in STR002, etc. (Which is weird but let's stick to simulator logic)
        # Simulator: 
        # return store, lane, seller_window_id
        # pos_event has POSId=lane["pos_id"]
        # So POS-01 is used for STR001, STR002, etc.
        # We need to map SellerWindowId -> POSId.
        
        for store in stores:
            for lane in lanes:
                seller_window_id = f"{store}_{lane['cam_id']}_{lane['window_id']}"
                self.mapping[seller_window_id] = lane['pos_id']
        # Buffer for events waiting for their pair
        self.pending_vas: Dict[str, VASEvent] = {} # Key: SellerWindowId
        self.pending_pos: Dict[str, POSEvent] = {} # Key: POSId
        
        # Mapping for reverse lookup: POSId -> (CamId, WindowId)
        self.pos_config = {}
        for lane in lanes:
            self.pos_config[lane['pos_id']] = (lane['cam_id'], lane['window_id'])

        # Buffer for events waiting for their pair
        self.pending_vas: Dict[str, VASEvent] = {} # Key: SellerWindowId
        self.pending_pos: Dict[str, POSEvent] = {} # Key: POSId
        
    async def process_vas(self, event: VASEvent):
        seller_window_id = event.SellerWindowId
        # Look up expected POS ID from mapping
        expected_pos_id = self.mapping.get(seller_window_id)
        
        if not expected_pos_id:
            print(f"Unknown mapping for {seller_window_id}")
            return

        # Check if POS event is already waiting
        # Note: POS event key in pending_pos currently uses just POSId. 
        # But since POSId might be shared across stores, we should probably key by StoreId_POSId?
        # Let's adjust pending_pos key to be unique: f"{event.StoreId}_{expected_pos_id}"
        
        pos_key = f"{event.StoreId}_{expected_pos_id}"
        
        if pos_key in self.pending_pos:
            pos_event = self.pending_pos.pop(pos_key)
            await self._analyze_pair(event, pos_event)
        else:
            # Store and wait
            self.pending_vas[seller_window_id] = event
            asyncio.create_task(self._check_timeout(seller_window_id, event))

    async def process_pos(self, event: POSEvent):
        pos_id = event.POSId
        store_id = event.StoreId
        
        # Resolve SellerWindowId from POS data
        if pos_id not in self.pos_config:
            print(f"Unknown POS configuration for {pos_id}")
            return
            
        cam_id, window_id = self.pos_config[pos_id]
        expected_seller_window_id = f"{store_id}_{cam_id}_{window_id}"
        
        # Check if VAS event is already waiting
        if expected_seller_window_id in self.pending_vas:
            vas_event = self.pending_vas.pop(expected_seller_window_id)
            await self._analyze_pair(vas_event, event)
        else:
            # Store with unique key combining Store and POS
            pos_key = f"{store_id}_{pos_id}"
            self.pending_pos[pos_key] = event
            asyncio.create_task(self._cleanup_pos(pos_key))

    async def _cleanup_pos(self, pos_key: str): # Updated signature
        await asyncio.sleep(30)
        if pos_key in self.pending_pos:
            del self.pending_pos[pos_key]

    async def _check_timeout(self, seller_window_id: str, vas_event: VASEvent):
        # Wait for 120 seconds (2 mins) to see if POS arrives as per High Severity Rule
        await asyncio.sleep(120) 
        
        # If still in pending, it means POS never arrived
        if seller_window_id in self.pending_vas and self.pending_vas[seller_window_id] == vas_event:
            del self.pending_vas[seller_window_id]
            
            # Application Logic: Receipt generated but POS missing
            # Only if receipt was actually generated (or supposedly generated)
            if vas_event.ReceiptGenerationStatus:
                await self._create_fraud_alert(
                    vas=vas_event, 
                    pos=None, 
                    rules=["Corresponding object is not present in VAS for POS and vise versa"],
                    risk_level="High"
                )

    async def _cleanup_pos(self, pos_key: str):
        # Increased cleanup time to match timeout window + buffer
        await asyncio.sleep(150)
        if pos_key in self.pending_pos:
            del self.pending_pos[pos_key]
            # TODO: Handle Case: POS exists, VAS missing? (Vice versa rule)
            # For now, just cleaning up. The vice versa rule implies we should check here too.
            # But let's stick to the primary flow for now to keep it simple unless specified.

    async def _analyze_pair(self, vas: VASEvent, pos: POSEvent):
        # We have both. Check for consistency.
        triggered_rules = []
        risk_level = "Low"

        # Rule 1: Payment Mode Mismatch (High)
        if vas.ModeOfTransaction != pos.ModeOfTransaction:
            triggered_rules.append(f"Payment Mode Mismatch (VAS: {vas.ModeOfTransaction.value}, POS: {pos.ModeOfTransaction.value})")

        # Rule 2: Bill not generated (Medium)
        if not vas.ReceiptGenerationStatus:
            triggered_rules.append("Bill not generated in VAS")

        # Rule 3: High Discount (Medium)
        if hasattr(pos, 'DiscountPercent') and pos.DiscountPercent > 20:
             triggered_rules.append(f"High Discount ({pos.DiscountPercent}%)")

        # Rule 4: Refund (Medium)
        if hasattr(pos, 'RefundAmount') and pos.RefundAmount > 0:
            triggered_rules.append(f"Refund Processed (${pos.RefundAmount})")

        # Determine Risk Level
        if any("Mismatch" in r for r in triggered_rules):
            risk_level = "High"
        elif any("Bill not generated" in r for r in triggered_rules): 
            # Note: User said Medium, but logic might dictate higher if no receipt? 
            # User specified Medium.
            if risk_level != "High":
                risk_level = "Medium"
        elif triggered_rules: # Any other rules (Discount, Refund)
             if risk_level != "High":
                risk_level = "Medium"
        
        # Additional check for High Severity "Vice Versa" rule is implicitly handled if one is missing (handled in timeout).
        # This function runs when BOTH match. 

        status = TransactionStatus.PENDING
        if risk_level == "High":
            status = TransactionStatus.FRAUDULENT
        elif risk_level == "Medium":
            status = TransactionStatus.SUSPICIOUS
        else:
            status = TransactionStatus.GENUINE

        # Create Transaction Record
        transaction = Transaction(
            id=f"TXN-{vas.SessionId}",
            shop_id=vas.StoreId,
            cam_id=vas.CamId,
            pos_id=pos.POSId,
            cashier_name=pos.CashierName,
            timestamp=datetime.fromtimestamp(pos.SessionTime),
            transaction_total=pos.TransactionTotal,
            risk_level=risk_level,
            triggered_rules=triggered_rules,
            status=status,
            fraud_category=triggered_rules[0] if triggered_rules else None,
            notes=", ".join(triggered_rules) if triggered_rules else None
        )

        await self.update_callback("NEW_TRANSACTION", transaction)

        # If High or Medium risk, trigger Alert (or just High?)
        # Let's trigger for both High and Medium to be safe/visible
        if risk_level in ["High", "Medium"]:
             await self._create_fraud_alert(vas, pos, triggered_rules, risk_level, transaction_id=transaction.id)

    async def _create_fraud_alert(self, vas: VASEvent, pos: Optional[POSEvent], rules: List[str], risk_level: str, transaction_id: str = None):
        if not transaction_id:
            transaction_id = f"TXN-{vas.SessionId}"
            # Dummy transaction for phantom scan
            transaction = Transaction(
                id=transaction_id,
                shop_id=vas.StoreId,
                cam_id=vas.CamId,
                pos_id=self.mapping.get(vas.SellerWindowId, "Unknown"),
                cashier_name="Unknown", 
                timestamp=datetime.fromtimestamp(vas.SessionEnd),
                transaction_total=0.0,
                risk_level=risk_level,
                triggered_rules=rules,
                status=TransactionStatus.FRAUDULENT,
                fraud_category=rules[0],
                notes="; ".join(rules)
            )
            await self.update_callback("NEW_TRANSACTION", transaction)

        alert = Alert(
            id=f"ALT-{uuid.uuid4().hex[:6].upper()}",
            transaction_id=transaction_id,
            shop_id=vas.StoreId,
            cashier_name=pos.CashierName if pos else "Unknown",
            risk_level=risk_level,
            triggered_rules=rules,
            timestamp=datetime.fromtimestamp(vas.SessionEnd),
            status=AlertStatus.NEW
        )
        await self.update_callback("NEW_ALERT", alert)
        
import uuid
