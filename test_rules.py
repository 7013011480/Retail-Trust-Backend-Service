import asyncio
import redis
import json
import time

# Connect to Redis
r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)

async def test_vice_versa_rule():
    print("Testing Vice Versa Rule (POS only)...")
    
    # 1. Clear streams (optional, but good for clean test)
    # r.delete("vas_stream", "pos_stream") 

    # 2. Inject POS Event (without VAS)
    pos_data = {
        "nscin": "STR001",
        "cashierName": "Test Cashier",
        "terminalNo": "POS-99", # Unique ID for test
        "billDate": "2023-10-27",
        "billTime": "10:00:00",
        "paymentMode": "Cash",
        "transactionTotal": 150.0
    }
    
    print(f"Injecting POS event: {pos_data['terminalNo']}")
    r.xadd("sales_stream", pos_data)
    
    # 3. Wait for timeout (150s + buffer)
    # We can't easily wait 150s in a quick test script without blocking.
    # But we can just trigger it and let the server logs show the result.
    print("POS event injected. Watch server logs for 'POS Transaction without Video Verification' alert in ~150 seconds.")

async def test_phantom_scan_rule():
    print("\nTesting Phantom Scan Rule (VAS only)...")
    
    # Inject VAS Event
    vas_data = {
        "StoreId": "STR001",
        "CamId": "CAM-99", # Unique
        "SellerWindowId": "STR001_CAM-99_W1",
        "SessionId": f"SESS-{int(time.time())}",
        "BillDate": "2023-10-27",
        "SessionStart": time.time(),
        "SessionEnd": time.time() + 10,
        "ModeOfTransaction": "Cash",
        "ReceiptGenerationStatus": "true"
    }
    
    print(f"Injecting VAS event: {vas_data['SessionId']}")
    r.xadd("vas_stream", vas_data)
    print("VAS event injected. Watch server logs for 'Phantom Scan' alert in ~120 seconds.")

if __name__ == "__main__":
    asyncio.run(test_vice_versa_rule())
    # asyncio.run(test_phantom_scan_rule())
