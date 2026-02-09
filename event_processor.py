import asyncio
import redis
import json
import time
from datetime import datetime
from typing import Optional

from models import VASEvent, POSEvent, TransactionMode, Transaction
from fraud_engine import FraudEngine

# Configuration
REDIS_HOST = 'localhost'
REDIS_PORT = 6379
VAS_STREAM_KEY = "vas_stream"
SALES_STREAM_KEY = "sales_stream" # Real POS data
# SALES_STREAM_KEY = "pos_stream" # Simulated POS data - uncomment to use simulator instead

async def print_alert(type: str, data: any):
    """Callback for FraudEngine to print alerts"""
    if type == "NEW_ALERT":
        print(f"\n[ALERT] {data.risk_level} Risk Alert!")
        print(f"  ID: {data.id}")
        print(f"  Rules: {', '.join(data.triggered_rules)}")
        print(f"  Status: {data.status}")
        print("-" * 30)
    elif type == "NEW_TRANSACTION":
        # Optional: Print transaction details
        # print(f"[TRANSACTION] {data.id} - {data.risk_level}")
        pass

class EventProcessor:
    def __init__(self):
        self.redis = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
        self.fraud_engine = FraudEngine(
            update_callback=print_alert,
            delete_callback=self.delete_event
        )
        self.last_vas_id = "0-0"
        self.last_sales_id = "0-0"

    async def delete_event(self, stream_name: str, event_id: str):
        """Callback to delete event from Redis Stream"""
        try:
            self.redis.xdel(stream_name, event_id)
            # print(f"Deleted {event_id} from {stream_name}")
        except Exception as e:
            print(f"Error deleting event {event_id} from {stream_name}: {e}")
        
    def _parse_vas(self, data: dict) -> Optional[VASEvent]:
        try:
            # Redis returns string values for bools usually, mapping needed?
            # VASEvent expects specific types.
            # data values are strings.
            
            receipt_status = str(data.get("ReceiptGenerationStatus", "true")).lower() == "true"
            
            # Helper to convert mode string to Enum
            mode_str = data.get("ModeOfTransaction")
            try:
                mode = TransactionMode(mode_str)
            except ValueError:
                mode = TransactionMode.CASH # Default safely
            
            return VASEvent(
                StoreId=data.get("StoreId"),
                CamId=data.get("CamId"),
                SellerWindowId=data.get("SellerWindowId"),
                SessionId=data.get("SessionId"),
                BillDate=data.get("BillDate"),
                SessionStart=float(data.get("SessionStart", 0)),
                SessionEnd=float(data.get("SessionEnd", 0)),
                ModeOfTransaction=mode,
                ReceiptGenerationStatus=receipt_status
            )
        except Exception as e:
            print(f"Error parsing VAS event: {e}")
            return None

    def _parse_sales(self, data: dict) -> Optional[POSEvent]:
        try:
            # Map sales_stream fields to POSEvent
            # sales_stream: nscin, billNo, cashierName, billDate, billTime, paymentMode, terminalNo
            
            # Combine Date and Time
            bill_date = data.get("billDate")
            bill_time = data.get("billTime")
            timestamp = time.time() # Default now
            if bill_date and bill_time:
                try:
                    dt_str = f"{bill_date} {bill_time}"
                    # Assumes format. Adjust if needed.
                    # Standard ISO or similar? "2023-10-27" "14:30:00"
                    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                    timestamp = dt.timestamp()
                except:
                    pass
            
            mode_str = data.get("paymentMode", "Cash")
            # Map string to Enum
            # Typical values: Cash, Card, UPI, etc.
            try:
                mode = TransactionMode(mode_str)
            except ValueError:
                # Try title case
                try:
                    mode = TransactionMode(mode_str.title())
                except:
                    mode = TransactionMode.CASH

            return POSEvent(
                StoreId=data.get("nscin", "Unknown"), # Map nscin to StoreId
                CashierName=data.get("cashierName", "Unknown"),
                POSId=data.get("terminalNo", "Unknown"), # Map terminalNo to POSId
                BillDate=bill_date,
                SessionTime=timestamp,
                ModeOfTransaction=mode,
                TransactionTotal=float(data.get("transactionTotal", 0.0)), # Might be missing
                DiscountPercent=0.0, # Missing in stream
                RefundAmount=0.0 # Missing in stream
            )
        except Exception as e:
            print(f"Error parsing Sales event: {e}")
            return None

    async def run(self):
        print("Starting Event Processor...")
        print("Consuming VAS events and checking for corresponding POS events...")
        
        while True:
            # Read new events from both streams
            # blocks for 100ms
            streams = {
                VAS_STREAM_KEY: self.last_vas_id,
                SALES_STREAM_KEY: self.last_sales_id
            }
            
            response = self.redis.xread(streams, count=5, block=100)
            
            if response:
                for stream_name, events in response:
                    for event_id, event_data in events:
                        if stream_name == VAS_STREAM_KEY:
                            self.last_vas_id = event_id
                            vas_event = self._parse_vas(event_data)
                            if vas_event:
                                print(f"Processing VAS: {vas_event.SessionId} (Window: {vas_event.SellerWindowId})")
                                await self.fraud_engine.process_vas(vas_event, event_id)
                                
                        elif stream_name == SALES_STREAM_KEY:
                            self.last_sales_id = event_id
                            pos_event = self._parse_sales(event_data)
                            if pos_event:
                                print(f"Processing POS/Sales: {pos_event.POSId} (Store: {pos_event.StoreId})")
                                await self.fraud_engine.process_pos(pos_event, event_id)
            
            # Yield to asyncio loop
            await asyncio.sleep(0.01)

if __name__ == "__main__":
    processor = EventProcessor()
    try:
        asyncio.run(processor.run())
    except KeyboardInterrupt:
        print("Processor stopped.")
