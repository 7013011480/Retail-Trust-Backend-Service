import asyncio
import json
import os
import uuid
import time
from datetime import datetime
from typing import Dict, Optional, List, Callable
from models import VASEvent, POSEvent, Transaction, Alert, TransactionStatus, AlertStatus, TransactionMode

# File Constants
POS_DATA_FILE = "pos_data.json"
VAS_DATA_FILE = "vas_data.json"
SALES_DATA_FILE = "pos_event.json"
VAS_EVENTS_FILE = "vas_event.json"

class FraudEngine:
    def __init__(self, update_callback: Callable, pos_lock: asyncio.Lock):
        self.update_callback = update_callback
        self.pos_lock = pos_lock
        self.active_tasks: Dict[str, asyncio.Task] = {} # Track active VAS tasks by SessionId

    async def handle_vas_event(self, vas_event: VASEvent):
        """
        Entry point for a new VAS event. 
        Spawns a background task to manage the lifecycle of this event.
        """
        session_id = vas_event.SessionId
        print(f"[FraudEngine] Received VAS Event: {session_id} (Window: {vas_event.SellerWindowId})")
        
        # Avoid duplicate tasks for the same session
        if session_id in self.active_tasks:
            # print(f"[FraudEngine] Task already active for {session_id}, skipping.")
            return

        # Create background task
        task = asyncio.create_task(self._process_vas_lifecycle(vas_event))
        self.active_tasks[session_id] = task
        
        # Cleanup task when done
        task.add_done_callback(lambda t: self.active_tasks.pop(session_id, None))

    async def _process_vas_lifecycle(self, vas_event: VASEvent):
        """
        Manages the lifecycle of a VAS event:
        1. Check for match immediately.
        2. If not found, wait 4 minutes.
        3. Check again.
        4. If found -> Validate & Move.
        5. If not found -> Raise Alert.
        """
        try:
            # 1. First Check
            print(f"[FraudEngine] {vas_event.SessionId}: Initial check for POS match...")
            match = await self._find_pos_match(vas_event)
            
            if match:
                print(f"[FraudEngine] {vas_event.SessionId}: Match found immediately!")
                await self._execute_judgment(vas_event, match)
                return

            # 2. Wait (4 minutes)
            print(f"[FraudEngine] {vas_event.SessionId}: No match found. Waiting 4 minutes...")
            await asyncio.sleep(240) 

            # 3. Second Check
            print(f"[FraudEngine] {vas_event.SessionId}: Retry check after wait...")
            match = await self._find_pos_match(vas_event)

            if match:
                print(f"[FraudEngine] {vas_event.SessionId}: Match found after wait!")
                await self._execute_judgment(vas_event, match)
                return

            # 4. Final: No match found
            print(f"[FraudEngine] {vas_event.SessionId}: Still no match. Raising alert.")
            await self._raise_missing_alert(vas_event)

        except Exception as e:
            print(f"[FraudEngine] Error in lifecycle for {vas_event.SessionId}: {e}")

    async def _find_pos_match(self, vas_event: VASEvent) -> Optional[POSEvent]:
        """
        Reads POS data and looks for a match based on SellerWindowId and Time.
        Condition: vas.SessionStart <= pos.SessionTime <= vas.SessionEnd
        """
        pos_data_list = []
        async with self.pos_lock:
            if os.path.exists(POS_DATA_FILE):
                try:
                    with open(POS_DATA_FILE, "r") as f:
                        content = f.read()
                        if content:
                            pos_data_list = json.loads(content)
                except Exception as e:
                    print(f"[FraudEngine] Error reading POS file: {e}")
                    return None

        # Filter for logic
        candidates = []
        for p in pos_data_list:
            # Check SellerWindowId
            p_window = p.get("SellerWindowId")
            if p_window != vas_event.SellerWindowId:
                continue

            # Check Time
            p_time = p.get("SessionTime", 0)
            if vas_event.SessionStart <= p_time <= vas_event.SessionEnd:
                candidates.append(p)

        if not candidates:
            return None
        
        # If multiple matches, we might need logic? User said "if found more than one event, then compare the timeframes"
        # We already compared timeframes. 
        # For now, pick the first valid one.
        if len(candidates) > 1:
            print(f"[FraudEngine] Warning: Multiple POS matches for {vas_event.SessionId}. Picking first.")

        return POSEvent(**candidates[0])

    async def _execute_judgment(self, vas: VASEvent, pos: POSEvent):
        """
        Validates the pair, sends to dashboard, and moves data to processed files.
        """
        triggered_rules = []
        risk_level = "Low"

        # Rule 1: Payment Mode Mismatch
        vas_mode = str(vas.ModeOfTransaction.value).lower()
        pos_mode = str(pos.ModeOfTransaction).lower()
        
        if vas_mode != pos_mode:
            triggered_rules.append(f"Payment Mode Mismatch (VAS: {vas.ModeOfTransaction.value}, POS: {pos.ModeOfTransaction})")

        # Rule 2: Bill not generated
        if not vas.ReceiptGenerationStatus:
            triggered_rules.append("Bill not generated in VAS")

        # Rule 3: High Discount
        if pos.DiscountPercent > 20:
             triggered_rules.append(f"High Discount ({pos.DiscountPercent}%)")

        # Rule 4: Refund
        if pos.RefundAmount > 0:
            triggered_rules.append(f"Refund Processed ({pos.RefundAmount})")

        # Determine Risk
        if any("Mismatch" in r for r in triggered_rules):
            risk_level = "High"
        elif any("Bill not generated" in r for r in triggered_rules): 
            risk_level = "Medium"
        elif triggered_rules:
             risk_level = "Medium"

        status = TransactionStatus.GENUINE
        if risk_level == "High": status = TransactionStatus.FRAUDULENT
        elif risk_level == "Medium": status = TransactionStatus.SUSPICIOUS

        # Create Transaction
        transaction_id = f"TXN-{vas.SessionId}"
        transaction = Transaction(
            id=transaction_id,
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

        # Send to Dashboard
        await self.update_callback("NEW_TRANSACTION", transaction)
        
        if risk_level in ["High", "Medium"]:
             await self._create_alert(vas, pos, triggered_rules, risk_level, transaction_id)

        # Move Data
        await self._move_data_processed(vas, pos)

    async def _raise_missing_alert(self, vas: VASEvent):
        """
        Raised when VAS exists but no POS found after timeout.
        """
        triggered_rules = ["Corresponding object is not present in VAS for POS and vise versa"]
        risk_level = "High"
        
        # Create Phantom Transaction for display
        transaction_id = f"TXN-{vas.SessionId}-MISSING"
        transaction = Transaction(
            id=transaction_id,
            shop_id=vas.StoreId,
            cam_id=vas.CamId,
            pos_id="Unknown", 
            cashier_name="Unknown", 
            timestamp=datetime.fromtimestamp(vas.SessionEnd),
            transaction_total=0.0,
            risk_level=risk_level,
            triggered_rules=triggered_rules,
            status=TransactionStatus.FRAUDULENT,
            fraud_category=triggered_rules[0],
            notes="POS Data Missing"
        )
        await self.update_callback("NEW_TRANSACTION", transaction)
        await self._create_alert(vas, None, triggered_rules, risk_level, transaction_id)
        
        # Remove from pending
        await self._remove_vas_event(vas)

    async def _create_alert(self, vas: VASEvent, pos: Optional[POSEvent], rules: List[str], risk_level: str, transaction_id: str):
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

    async def _move_data_processed(self, vas: VASEvent, pos: POSEvent):
        """
        Moves matched events to processed files.
        """
        # 1. Append to Archive Files
        await self._append_to_file(VAS_EVENTS_FILE, vas.model_dump())
        await self._append_to_file(SALES_DATA_FILE, pos.model_dump())

        # 2. Remove from Source Files
        await self._remove_vas_event(vas)
        await self._remove_pos_event(pos)

    async def _append_to_file(self, filename: str, data: dict):
        # Simple append, no lock? Ideally lock if multiple writers.
        # Assuming we are single writer (FraudEngine) for archives.
        try:
            items = []
            if os.path.exists(filename):
                try:
                    with open(filename, "r") as f:
                        content = f.read()
                        if content: items = json.loads(content)
                except: pass
            
            items.append(data)
            
            with open(filename, "w") as f:
                json.dump(items, f, indent=4)
        except Exception as e:
            print(f"[FraudEngine] Error appending to {filename}: {e}")

    async def _remove_vas_event(self, vas: VASEvent):
        try:
            if not os.path.exists(VAS_DATA_FILE): return
            
            with open(VAS_DATA_FILE, "r") as f:
                items = json.loads(f.read())
            
            new_items = [i for i in items if i.get("SessionId") != vas.SessionId]
            
            with open(VAS_DATA_FILE, "w") as f:
                json.dump(new_items, f, indent=4)
            print(f"[FraudEngine] Removed VAS {vas.SessionId} from {VAS_DATA_FILE}")
            
        except Exception as e:
            print(f"[FraudEngine] Error removing VAS {vas.SessionId}: {e}")

    async def _remove_pos_event(self, pos: POSEvent):
        async with self.pos_lock:
            try:
                if not os.path.exists(POS_DATA_FILE): return

                with open(POS_DATA_FILE, "r") as f:
                    items = json.loads(f.read())
                
                new_items = []
                removed = False
                for item in items:
                    if not removed and \
                       item.get("StoreId") == pos.StoreId and \
                       item.get("POSId") == pos.POSId and \
                       item.get("SessionTime") == pos.SessionTime:
                        removed = True
                        continue
                    new_items.append(item)
                
                with open(POS_DATA_FILE, "w") as f:
                    json.dump(new_items, f, indent=4)
                print(f"[FraudEngine] Removed POS {pos.POSId} from {POS_DATA_FILE}")

            except Exception as e:
                print(f"[FraudEngine] Error removing POS {pos.POSId}: {e}")
