import asyncio
import redis
import time

r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

async def verify_deletion():
    print("Verifying Event Deletion...")
    
    # 1. Clean streams
    r.del("vas_stream", "sales_stream")
    
    # 2. Inject Matched Pair (should be processed & deleted quickly)
    vas_data = {
        "StoreId": "STR001",
        "CamId": "CAM-01",
        "SellerWindowId": "STR001_CAM-01_W1",
        "SessionId": "SESS-DEL-TEST",
        "BillDate": "2023-10-27",
        "SessionStart": time.time(),
        "SessionEnd": time.time() + 10,
        "ModeOfTransaction": "Cash",
        "ReceiptGenerationStatus": "true"
    }
    
    pos_data = {
        "nscin": "STR001",
        "cashierName": "Test Cashier",
        "terminalNo": "POS-01", 
        "billDate": "2023-10-27",
        "billTime": "10:00:00",
        "paymentMode": "Cash",
        "transactionTotal": 100.0
    }
    
    print("Injecting matched pair...")
    vas_id = r.xadd("vas_stream", vas_data)
    pos_id = r.xadd("sales_stream", pos_data)
    
    print(f"Injected VAS: {vas_id}, POS: {pos_id}")
    
    # Wait for processor to pick up (assuming processor is running in background)
    print("Waiting 10 seconds for processing...")
    await asyncio.sleep(10)
    
    # Check if deleted
    vas_exists = r.xrange("vas_stream", vas_id, vas_id)
    pos_exists = r.xrange("sales_stream", pos_id, pos_id)
    
    if not vas_exists and not pos_exists:
        print("SUCCESS: Both events deleted from stream.")
    else:
        print(f"FAILURE: Events still exist. VAS: {len(vas_exists)}, POS: {len(pos_exists)}")

if __name__ == "__main__":
    asyncio.run(verify_deletion())
