import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
from typing import Dict, Optional, List, Callable
from models import VASEvent, POSEvent, Transaction, Alert, TransactionStatus, AlertStatus #, TransactionMode
# from utils import acquire_file_lock

# File Constants
POS_DATA_FILE = "pos_data.json"
VAS_DATA_FILE = "vas_data.json"
SALES_DATA_FILE = "pos_event.json"
VAS_EVENTS_FILE = "vas_event.json"
RULE_CONFIG_FILE = "rule_config.json"

def load_rule_config() -> dict:
    """Load configurable rule thresholds."""
    defaults = {
        "discount_threshold_percent": 20,
        "refund_amount_threshold": 0,
        "high_value_threshold": 2000,
        "bulk_quantity_threshold": 10,
        "idle_pos_minutes": 30
    }
    try:
        if os.path.exists(RULE_CONFIG_FILE):
            with open(RULE_CONFIG_FILE, "r") as f:
                return {**defaults, **json.load(f)}
    except Exception:
        pass
    return defaults

class FraudEngine:
    def __init__(self, update_callback: Callable, pos_lock: asyncio.Lock):
        self.update_callback = update_callback
        self.pos_lock = pos_lock

    def _ist_to_unix(self, ist_str: str) -> float:
        """Helper to convert IST timestamp string to Unix timestamp."""
        try:
            dt = datetime.strptime(ist_str, "%Y-%m-%d %H:%M:%S")
            return dt.timestamp()
        except Exception as e:
            print(f"[FraudEngine] Error parsing IST timestamp {ist_str}: {e}")
            return datetime.now().timestamp()

    async def run_vas_batch_process(self):
        """
        Process all pending VAS events:
        1. Read VAS data.
        2. For each event, check for POS match.
        3. If match found -> Validate & Move.
        4. If not found:
           - If older than 2 mins -> Raise Alert (Missing POS) & Move.
           - If recent -> Skip (wait for next cycle).
        """
        print("[FraudEngine] Starting VAS Batch Process...")
        vas_events = await self._read_vas_file()
        
        current_time = datetime.now().timestamp()
        
        for vas in vas_events:
            try:
                # Check for POS match
                match = await self._find_pos_match(vas)
                
                if match:
                    print(f"[FraudEngine] Matched VAS {vas.SessionId} with POS {match.POSId}")
                    await self._execute_judgment(vas, match)
                else:
                    # Check age (Assuming SessionEnd is the event timestamp)
                    # We accept up to 2 minutes delay.
                    event_age = current_time - self._ist_to_unix(vas.SessionEnd)
                    if event_age > 120: # 2 minutes
                        print(f"[FraudEngine] VAS {vas.SessionId} unmatched older than 2 mins. Raising Missing Alert.")
                        await self._raise_missing_alert(vas, missing_type="POS")
                    else:
                        # Too recent, leave it for now
                        pass
                        
            except Exception as e:
                print(f"[FraudEngine] Error processing VAS {vas.SessionId}: {e}")

    async def run_pos_batch_process(self):
        """
        Process all pending POS events:
        1. Read POS data.
        2. For each event, check for VAS match.
        3. If match found -> Validate & Move.
           (Note: Usually VAS process runs first, so matches should be handled there, but this handles race/order issues)
        4. If not found:
           - If older than 2 mins -> Raise Alert (Missing VAS) & Move.
           - If recent -> Skip.
        """
        print("[FraudEngine] Starting POS Batch Process...")
        pos_events = await self._read_pos_file()
        
        current_time = datetime.now().timestamp()
        
        for pos in pos_events:
            try:
                # Check for VAS match
                match = await self._find_vas_match(pos)
                
                if match:
                    print(f"[FraudEngine] Matched POS {pos.POSId} with VAS {match.SessionId}")
                    await self._execute_judgment(match, pos) # Note order: vas, pos
                else:
                    event_age = current_time - self._ist_to_unix(pos.SessionTime)
                    if event_age > 120:
                         print(f"[FraudEngine] POS {pos.POSId} unmatched older than 2 mins. Raising Missing Alert.")
                         await self._raise_missing_alert(pos, missing_type="VAS")
                    else:
                        pass
            except Exception as e:
                print(f"[FraudEngine] Error processing POS {pos.POSId}: {e}")

    async def _read_vas_file(self) -> List[VASEvent]:
        if not os.path.exists(VAS_DATA_FILE): return []
        try:
            with open(VAS_DATA_FILE, "r") as f:
                content = f.read()
                if not content: return []
                data = json.loads(content)
                return [VASEvent(**item) for item in data]
        except Exception as e:
            print(f"[FraudEngine] Error reading VAS file: {e}")
            return []

    async def _read_pos_file(self) -> List[POSEvent]:
        async with self.pos_lock:
            if not os.path.exists(POS_DATA_FILE): return []
            try:
                with open(POS_DATA_FILE, "r") as f:
                    content = f.read()
                    if not content: return []
                    data = json.loads(content)
                    return [POSEvent(**item) for item in data]
            except Exception as e:
                print(f"[FraudEngine] Error reading POS file: {e}")
                return []

    async def _find_pos_match(self, vas_event: VASEvent) -> Optional[POSEvent]:
        """
        Reads POS data and looks for a match based on SellerWindowId and Time.
        Condition: vas.SessionStart <= pos.SessionTime <= vas.SessionEnd
        """
        # We need to re-read or pass the list? 
        # Ideally we read the file again to get latest state, 
        # or we rely on the list we just read? 
        # To be safe and simple, let's look at the file (masked by lock if needed).
        # Actually, self.run_vas_batch_process iterates the file content.
        # But for matching we need the OTHER file.
        
        pos_list = await self._read_pos_file()
        
        for p in pos_list:
            if p.SellerWindowId == vas_event.SellerWindowId:
                # Comparison logic: vas.SessionStart <= pos.SessionTime <= vas.SessionEnd
                vas_start = self._ist_to_unix(vas_event.SessionStart)
                vas_end = self._ist_to_unix(vas_event.SessionEnd)
                pos_time = self._ist_to_unix(p.SessionTime)
                if vas_start <= pos_time <= vas_end:
                    return p
        return None

    async def _find_vas_match(self, pos_event: POSEvent) -> Optional[VASEvent]:
        """
        Reads VAS data and looks for a match.
        Condition: vas.SessionStart <= pos.SessionTime <= vas.SessionEnd
        """
        vas_list = await self._read_vas_file()
        
        for v in vas_list:
             if v.SellerWindowId == pos_event.SellerWindowId:
                vas_start = self._ist_to_unix(v.SessionStart)
                vas_end = self._ist_to_unix(v.SessionEnd)
                pos_time = self._ist_to_unix(pos_event.SessionTime)
                if vas_start <= pos_time <= vas_end:
                    return v
        return None

    async def _execute_judgment(self, vas: VASEvent, pos: POSEvent):
        """
        Validates the pair, sends to dashboard, and moves data to processed files.
        """
        config = load_rule_config()
        triggered_rules = []
        risk_level = "Low"

        # Rule 1: Payment Mode Mismatch (when VAS provides payment mode)
        if hasattr(vas, 'ModeOfTransaction') and vas.ModeOfTransaction:
            vas_mode = str(vas.ModeOfTransaction).lower()
            pos_mode = str(pos.ModeOfTransaction).lower()
            if vas_mode != "unknown" and pos_mode != "unknown" and vas_mode != pos_mode:
                triggered_rules.append(f"Payment Mode Mismatch (VAS: {vas_mode}, POS: {pos_mode})")

        # Rule 2: Bill not generated
        if not vas.ReceiptGenerationStatus:
            triggered_rules.append("Bill not generated in VAS")

        # Rule 3: High Discount (configurable)
        if pos.DiscountPercent > config["discount_threshold_percent"]:
            triggered_rules.append(f"High Discount ({pos.DiscountPercent}%)")

        # Rule 4: Refund (configurable threshold)
        if pos.RefundAmount > config["refund_amount_threshold"]:
            triggered_rules.append(f"Refund Processed (Rs.{pos.RefundAmount})")

        # Rule 5: Complementary order
        if hasattr(pos, 'IsComplementary') and pos.IsComplementary == "Yes":
            triggered_rules.append("Complementary Order")

        # Rule 6: Void/Cancelled transaction
        if hasattr(pos, 'VoidReason') and pos.VoidReason:
            triggered_rules.append(f"Void Transaction ({pos.VoidReason})")
        if hasattr(pos, 'CancelDate') and pos.CancelDate:
            triggered_rules.append("Cancelled Transaction")

        # Rule 7: Negative amount
        if pos.TransactionTotal < 0:
            triggered_rules.append(f"Negative Amount (Rs.{pos.TransactionTotal})")

        # Rule 8: High value transaction (configurable)
        if pos.TransactionTotal > config["high_value_threshold"]:
            triggered_rules.append(f"High Value Transaction (Rs.{pos.TransactionTotal})")

        # Rule 9: Bulk purchase (configurable)
        if hasattr(pos, 'ItemCount') and pos.ItemCount > config["bulk_quantity_threshold"]:
            triggered_rules.append(f"Bulk Purchase ({pos.ItemCount} items)")

        # Determine Risk
        if any(r for r in triggered_rules if "Mismatch" in r or "Void" in r or "Cancelled" in r or "Negative" in r):
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
            cam_id=vas.SellerWindowId,
            pos_id=pos.POSId,
            cashier_name=pos.CashierName,
            timestamp=datetime.strptime(pos.SessionTime, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST),
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

    async def _raise_missing_alert(self, event_obj, missing_type: str):
        """
        Raised when an event is missing its counterpart after timeout.
        event_obj can be VASEvent or POSEvent.
        missing_type is "POS" (if event_obj is VAS) or "VAS" (if event_obj is POS).
        """
        triggered_rules = [f"Corresponding {missing_type} data not found"]
        risk_level = "High"
        
        # Construct Transaction Data based on what we have
        if missing_type == "POS":
            # We have VAS
            vas = event_obj
            pos = None
            t_id = f"TXN-{vas.SessionId}-MISSING"
            timestamp = datetime.strptime(vas.SessionEnd, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
            shop_id = vas.StoreId
            cam_id = vas.SellerWindowId
            pos_id = "Unknown"
            cashier = "Unknown"
            total = 0.0
            
        else:
             # We have POS
            pos = event_obj
            vas = None # We don't have VAS object, but we need to pass something if we want to log it?
                       # Or we create a dummy VAS? Or just handle None in alert creation?
            
            t_id = f"TXN-POS-{pos.POSId}-MISSING"
            timestamp = datetime.strptime(pos.SessionTime, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
            shop_id = pos.StoreId
            cam_id = "Unknown"
            pos_id = pos.POSId
            cashier = pos.CashierName
            total = pos.TransactionTotal

        transaction = Transaction(
            id=t_id,
            shop_id=shop_id,
            cam_id=cam_id,
            pos_id=pos_id,
            cashier_name=cashier,
            timestamp=timestamp,
            transaction_total=total,
            risk_level=risk_level,
            triggered_rules=triggered_rules,
            status=TransactionStatus.FRAUDULENT,
            fraud_category=triggered_rules[0],
            notes=f"{missing_type} Data Missing"
        )
        
        await self.update_callback("NEW_TRANSACTION", transaction)
        
        # Alert needs VAS object mostly for StoreId/CamId?
        # If VAS missing, we pass None?
        await self._create_alert(vas, pos, triggered_rules, risk_level, t_id)
        
        # Archive and Remove
        if missing_type == "POS":
            # Archive VAS event as it is processed (even if missing counterpart)
            # We might want to enrich it or just dump it?
            # Dumping raw VAS event to archive.
            await self._append_to_file(VAS_EVENTS_FILE, vas.model_dump())
            await self._remove_vas_event(vas)
        else:
            # Archive POS event
            await self._append_to_file(SALES_DATA_FILE, pos.model_dump())
            await self._remove_pos_event(pos)

    async def _create_alert(self, vas: Optional[VASEvent], pos: Optional[POSEvent], rules: List[str], risk_level: str, transaction_id: str):
        
        shop_id = vas.StoreId if vas else (pos.StoreId if pos else "Unknown")
        cashier = pos.CashierName if pos else "Unknown"
        ts = datetime.strptime(vas.SessionEnd, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST) if vas else (datetime.strptime(pos.SessionTime, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST) if pos else datetime.now())
        
        alert = Alert(
            id=f"ALT-{uuid.uuid4().hex[:6].upper()}",
            transaction_id=transaction_id,
            shop_id=shop_id,
            cashier_name=cashier,
            risk_level=risk_level,
            triggered_rules=rules,
            timestamp=ts,
            status=AlertStatus.NEW
        )
        await self.update_callback("NEW_ALERT", alert)

    async def _move_data_processed(self, vas: VASEvent, pos: POSEvent):
        """
        Moves matched events to processed files.
        """
        # 1. Append to Archive Files
        if vas: await self._append_to_file(VAS_EVENTS_FILE, vas.model_dump())
        if pos: await self._append_to_file(SALES_DATA_FILE, pos.model_dump())

        # 2. Remove from Source Files
        if vas: await self._remove_vas_event(vas)
        if pos: await self._remove_pos_event(pos)

    async def _append_to_file(self, filename: str, data: dict):
        # Scan file, append, write.
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
            
            # Lock removed as per user request and ineffectiveness against external non-locking process.
            if True: # Kept indentation block for minimal diff or could unindent
                with open(VAS_DATA_FILE, "r") as f:
                    content = f.read()
                    items = json.loads(content) if content else []
                
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
                    content = f.read()
                    items = json.loads(content) if content else []
                
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
