import asyncio
import httpx
import json
import time
import logging
from datetime import datetime, timedelta
import os
import redis

class SalesPoller:
    def __init__(self, output_file: str = "sales_data.json"):
        self.output_file = output_file
        self.api_url = "https://openapis.nukkadshops.com/v1/sales/getSalesWithItems"
        self.headers = {"X-Nukkad-API-Token": "j6RzQe7rZyyM3McZXD8gZD2XNj8vKfuf"}
        self.cin = "NSCIN8227" # User should replace this
        self.redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
        self.stream_key = "sales_stream"

    async def fetch_sales(self):
        """Fetches sales data from the API."""
        now = datetime.now()
        to_time = int(now.timestamp())
        from_time = int((now - timedelta(minutes=2)).timestamp())

        payload = {
            "cin": self.cin,
            "from": "1765778611", #from_time,
            "to": "1765951411", #to_time,
            "pageNo": "1"
        }

        print(f"[{datetime.now()}] Polling sales data from {from_time} to {to_time}...")
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(self.api_url, headers=self.headers, json=payload)
                
            if response.status_code == 200:
                data = response.json()
                if data.get("response") and "data" in data and "bills" in data["data"]:
                    self.process_bills(data["data"]["bills"])
                else:
                    print(f"API returned success but no bills found or invalid format: {data}")
            else:
                print(f"API request failed: {response.status_code} - {response.text}")
                
        except Exception as e:
            print(f"Error fetching sales data: {e.get('message')}")

    def process_bills(self, bills: list):
        """Processes the list of bills and writes to file."""
        processed_data = []
        
        for bill in bills:
            try:
                # Extract required fields with safety checks
                cashier_details = bill.get("cashierDetails", {})
                pay_modes = bill.get("payModes", [])
                payment_mode = pay_modes[0].get("mode") if pay_modes else "Unknown"
                
                processed_bill = {
                    "nscin": bill.get("nscin"),
                    "billNo": bill.get("billNo"),
                    "cashierName": cashier_details.get("cashierName"),
                    "cashierEmail": cashier_details.get("email"),
                    "billType": bill.get("billType"),
                    "billDate": bill.get("billDate"),
                    "billTime": bill.get("billTime"),
                    "billSyncTime": bill.get("billSyncTime"),
                    "billSource": bill.get("billSource"),
                    "paymentMode": payment_mode,
                    "terminalNo": bill.get("terminalNo")  
                }
                processed_data.append(processed_bill)
            except Exception as e:
                print(f"Error processing bill {bill.get('billNo')}: {e}")

        # Write to file (Overwrite or Append? "Create a new json file" implies fresh or appended)
        # I will append to a list in the file to keep history, or overwrite if it's just a dump
        # The prompt says "create a new json file and write this response into that file"
        # I'll overwrite for now, as accumulating might grow indefinitely without rotation.
        
        try:
            with open(self.output_file, "w") as f:
                json.dump(processed_data, f, indent=4)
            print(f"Successfully wrote {len(processed_data)} bills to {self.output_file}")
            
            # Publish to Redis Stream
            for bill in processed_data:
                try:
                    # Redis streams store dictionaries. We need to ensure values are strings/numbers
                    # JSON dumping the whole object as a field might be easier for complex nested data,
                    # but streams usually want flat k/v. The bill object is flat enough except for lists?
                    # The prompt asked to create objects with specific fields which are all flat strings/ints in my previous step.
                    # Let's double check. processed_bill has strings mostly.
                    
                    # Convert all values to string to be safe for Redis
                    bill_for_redis = {k: str(v) for k, v in bill.items()}
                    
                    self.redis_client.xadd(self.stream_key, bill_for_redis)
                    print(f"Published bill {bill['billNo']} to stream {self.stream_key}")
                except Exception as e:
                    print(f"Error publishing to Redis: {e}")
                    
        except Exception as e:
            print(f"Error writing to file: {e}")

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
