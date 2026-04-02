import asyncio
import httpx
import json
import time
import logging
from datetime import datetime, timedelta, timezone

_IST = timezone(timedelta(hours=5, minutes=30))
import os
import ssl
import truststore

class SalesPoller:
    def __init__(self, file_lock: asyncio.Lock = None, storage_path: str = "pos_data.json"):
        self.output_file = storage_path
        self.file_lock = file_lock
        
        # Load config from env
        from dotenv import load_dotenv
        load_dotenv()
        
        self.api_url = os.getenv("EXTERNAL_SALES_URL")
        token = os.getenv("EXTERNAL_SALES_HEADER_TOKEN")
        self.headers = {"X-Nukkad-API-Token": token}
        self.stores = self._load_stores()
        self.mapping = {}
        self._load_mapping()

    def _load_stores(self) -> list:
        try:
            if os.path.exists("stores.json"):
                with open("stores.json", "r") as f:
                    stores = json.load(f)
                    print(f"Loaded {len(stores)} stores.")
                    return stores
        except Exception as e:
            print(f"Error loading stores.json: {e}")
        return [{"cin": "NDCIN1223", "name": "Ram Ki Bandi"}]

    def _load_mapping(self):
        try:
            if os.path.exists("mapping.json"):
                with open("mapping.json", "r") as f:
                    self.mapping = json.load(f)
                    print(f"Loaded {len(self.mapping)} mappings.")
            else:
                print("mapping.json not found.")
        except Exception as e:
            print(f"Error loading mapping.json: {e}")

    async def fetch_sales(self):
        """Fetches sales data from the API for all stores."""
        now = datetime.now(_IST)
        to_time = int(now.timestamp())
        from_time = int((now - timedelta(minutes=2)).timestamp())

        print(f"[{datetime.now(_IST).strftime('%H:%M:%S')}] Polling {len(self.stores)} stores...")

        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

        for store in self.stores:
            cin = store["cin"]
            payload = {
                "cin": cin,
                "from": str(from_time),
                "to": str(to_time),
                "pageNo": "1"
            }

            try:
                async with httpx.AsyncClient(verify=ctx, timeout=15.0) as client:
                    response = await client.post(self.api_url, headers=self.headers, json=payload)

                if response.status_code == 200:
                    data = response.json()
                    if data.get("response") and "data" in data and "bills" in data["data"]:
                        bills = data["data"]["bills"]
                        if bills:
                            await self.process_bills(bills)
                else:
                    print(f"  [{cin}] API failed: {response.status_code}")

            except Exception as e:
                print(f"  [{cin}] Error: {e}")

    async def process_bills(self, bills: list):
        """Processes the list of bills and writes to shared file."""
        processed_data = []
        
        for bill in bills:
            try:
                # Extract required fields with safety checks
                # cashier_details = bill.get("cashierDetails", {})
                pay_modes = bill.get("payModes", [])
                payment_mode = pay_modes[0].get("mode") if pay_modes else "Unknown"
                
                # Parse timestamp
                bill_date = bill.get("billDate", "")
                bill_time_str = bill.get("billTime", "")
                session_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if bill_date and bill_time_str:
                    session_time = f"{bill_date} {bill_time_str}"

                # Extract amounts from API
                total_amount = float(bill.get("actualBillAmt", 0.0))
                disc_amt = float(bill.get("discAmt", 0.0))
                return_amt = float(bill.get("returnAmt", 0.0))

                # Calculate discount percentage from discount amount
                discount_percent = 0.0
                if total_amount > 0 and disc_amt > 0:
                    discount_percent = (disc_amt / total_amount) * 100

                store_id = bill.get("ndcin", "Unknown")
                pos_id = bill.get("terminalName", "Unknown")

                # Resolve SellerWindowId from mapping
                mapping_key = f"{store_id}_{pos_id}"
                seller_window_id = self.mapping.get(mapping_key, "Unknown")

                # Create POSEvent dict
                event = {
                    "StoreId": store_id,
                    "CashierName": bill.get("cashierName", "Unknown"),
                    "POSId": pos_id,
                    "SellerWindowId": seller_window_id,
                    "BillDate": bill_date,
                    "SessionTime": session_time,
                    "ModeOfTransaction": payment_mode,
                    "TransactionTotal": total_amount,
                    "DiscountPercent": round(discount_percent, 2),
                    "RefundAmount": return_amt,
                    "IsComplementary": bill.get("isComplementary", "No"),
                    "BillStatus": bill.get("status", "Completed"),
                    "VoidReason": bill.get("voidReason", ""),
                    "CancelDate": bill.get("cancelDate", ""),
                    "ItemCount": sum(item.get("qty", 1) for item in bill.get("items", [])),
                    "BillAmount": float(bill.get("billAmt", total_amount)),
                    "PaymentReceived": sum(float(pm.get("amt", 0)) for pm in pay_modes),
                    "billNo": bill.get("billNo")
                }
                processed_data.append(event)
            except Exception as e:
                print(f"Error processing bill {bill.get('billNo')}: {e}")

        if not processed_data:
            return

        # Write to shared file with lock
        if self.file_lock:
            async with self.file_lock:
                await self._append_to_file(processed_data)
        else:
            # Standalone mode (no lock)
            await self._append_to_file(processed_data)

    async def _append_to_file(self, new_events: list):
        current_data = []
        if os.path.exists(self.output_file):
            try:
                with open(self.output_file, "r") as f:
                    content = f.read()
                    if content:
                        current_data = json.loads(content)
            except json.JSONDecodeError:
                pass
        
        # Avoid duplicates based on billNo?
        existing_bills = {e.get("billNo") for e in current_data if "billNo" in e}
        
        final_list = current_data
        added_count = 0
        for event in new_events:
            if event.get("billNo") not in existing_bills:
                final_list.append(event)
                added_count += 1
        
        if added_count > 0:
            with open(self.output_file, "w") as f:
                json.dump(final_list, f, indent=4)
            print(f"Added {added_count} new bills to {self.output_file}")

    async def fetch_historical(self, days: int = 10) -> dict:
        """Fetches historical sales data for the last N days across all stores."""
        all_events = []
        raw_bills = []
        now = datetime.now(_IST)
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

        for store in self.stores:
            cin = store["cin"]
            store_count = 0

            for day_offset in range(days, 0, -1):
                day_start = now - timedelta(days=day_offset)
                day_end = now - timedelta(days=day_offset - 1)
                from_ts = str(int(day_start.timestamp()))
                to_ts = str(int(day_end.timestamp()))

                page = 1
                while True:
                    payload = {
                        "cin": cin,
                        "from": from_ts,
                        "to": to_ts,
                        "pageNo": str(page)
                    }
                    try:
                        async with httpx.AsyncClient(verify=ctx, timeout=30.0) as client:
                            response = await client.post(self.api_url, headers=self.headers, json=payload)

                        if response.status_code == 200:
                            data = response.json()
                            if data.get("response") and "data" in data and "bills" in data["data"]:
                                bills = data["data"]["bills"]
                                if not bills:
                                    break
                                for bill in bills:
                                    event = self._bill_to_event(bill)
                                    if event:
                                        all_events.append(event)
                                        raw_bills.append(bill)
                                        store_count += 1

                                page_count = data["data"].get("pageCount", 1)
                                if page >= page_count:
                                    break
                                page += 1
                            else:
                                break
                        else:
                            break
                    except Exception as e:
                        print(f"  [{cin}] Historical fetch error day -{day_offset}: {e}")
                        break

            if store_count > 0:
                print(f"  [{cin}] {store.get('name', '')} - {store_count} bills")

        print(f"Fetched {len(all_events)} total historical bills across {len(self.stores)} stores over {days} days")
        return {"events": all_events, "raw_bills": raw_bills}

    def _bill_to_event(self, bill: dict) -> dict | None:
        """Convert a raw API bill into a POSEvent dict."""
        try:
            pay_modes = bill.get("payModes", [])
            payment_mode = pay_modes[0].get("mode") if pay_modes else "Unknown"

            bill_date = bill.get("billDate", "")
            bill_time_str = bill.get("billTime", "")
            session_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if bill_date and bill_time_str:
                session_time = f"{bill_date} {bill_time_str}"

            total_amount = float(bill.get("actualBillAmt", 0.0))
            disc_amt = float(bill.get("discAmt", 0.0))
            return_amt = float(bill.get("returnAmt", 0.0))

            discount_percent = 0.0
            if total_amount > 0 and disc_amt > 0:
                discount_percent = (disc_amt / total_amount) * 100

            store_id = bill.get("ndcin", "Unknown")
            pos_id = bill.get("terminalName", "Unknown")

            mapping_key = f"{store_id}_{pos_id}"
            seller_window_id = self.mapping.get(mapping_key, "Unknown")

            return {
                "StoreId": store_id,
                "CashierName": bill.get("cashierName", "Unknown"),
                "POSId": pos_id,
                "SellerWindowId": seller_window_id,
                "BillDate": bill_date,
                "SessionTime": session_time,
                "ModeOfTransaction": payment_mode,
                "TransactionTotal": total_amount,
                "DiscountPercent": round(discount_percent, 2),
                "RefundAmount": return_amt,
                "IsComplementary": bill.get("isComplementary", "No"),
                "BillStatus": bill.get("status", "Completed"),
                "VoidReason": bill.get("voidReason", ""),
                "CancelDate": bill.get("cancelDate", ""),
                "ItemCount": sum(item.get("qty", 1) for item in bill.get("items", [])),
                "BillAmount": float(bill.get("billAmt", total_amount)),
                "PaymentReceived": sum(float(pm.get("amt", 0)) for pm in pay_modes),
                "billNo": bill.get("billNo")
            }
        except Exception as e:
            print(f"Error processing bill {bill.get('billNo')}: {e}")
            return None

    async def start_polling(self, interval_seconds: int = 120):
        """Starts the polling loop."""
        while True:
            await self.fetch_sales()
            await asyncio.sleep(interval_seconds)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    poller = SalesPoller()
    print("Starting SalesPoller (Standalone)... Press Ctrl+C to stop.")
    try:
        asyncio.run(poller.start_polling())
    except KeyboardInterrupt:
        print("Stopping SalesPoller...")
