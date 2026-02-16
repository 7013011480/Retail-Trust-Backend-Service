# Retail Trust Backend Service - Data Flow & Logic Documentation

This document outlines the complete flow of data from ingestion to fraud analysis, detailing the parameters involved and the comparison logic used.

## 1. Data Ingestion & Structures

### A. VAS Event (Video Analytics System)
**Source**: Inherited from `vas_data.json` (simulated or external input).
**Key Parameters**:
*   `StoreId`: Identifier of the store (e.g., "NSCIN8227").
*   `CamId`: Camera identifier.
*   `SellerWindowId`: **Crucial Key**. Represents the specific sales window/counter. Used for matching.
*   `SessionId`: Unique ID for the video session.
*   `BillDate`: Date of the session.
*   `SessionStart` / `SessionEnd`: Timestamps of the activity.
*   `ModeOfTransaction`: Detected payment mode (Visual).
*   `ReceiptGenerationStatus`: Boolean indicating if a receipt was visually seen being generated.

### B. POS Event (Point of Sale)
**Source**: Polls external API, processes bills, and writes to `sales_data.json`.
**Key Parameters**:
*   `StoreId`: Derived from bill data (`ndcin`).
*   `POSId`: Derived from bill data (`terminalName`).
*   `SellerWindowId`: **Derived Field**. Injected by `SalesPoller`.
    *   **Logic**: `mapping.json` is queried using `{StoreId}_{POSId}` to find the corresponding `SellerWindowId`.
*   `CashierName`: Name of the cashier.
*   `BillDate` / `SessionTime`: Timestamp of the transaction.
*   `ModeOfTransaction`: Payment mode from POS system.
*   `TransactionTotal`: Total amount (`actualBillAmt`).
*   `DiscountPercent`: Discount applied (default 0.0).
*   `RefundAmount`: Refund processed (default 0.0).
*   `billNo`: Unique bill number.

---

## 2. Matching Logic (The Connection)

The system links VAS and POS events to create a "Transaction Pair" for analysis.

*   **Mechanism**: **Scheduled Batch Processing**.
*   **Key**: `SellerWindowId` + `SessionTime`.
*   **Process**:
    1.  **Ingestion**: VAS and POS events are written to `vas_data.json` and `pos_data.json` respectively.
    2.  **Scheduler**: A background task wakes up every **2 minutes**.
    3.  **VAS Processing**: 
        - The `FraudEngine` reads all pending VAS events.
        - For each VAS event, it searches `pos_data.json` for a match (same Window, time overlapping).
    4.  **POS Processing**:
        - The `FraudEngine` reads all pending POS events.
        - For each POS event, it searches `vas_data.json` for a match.
    5.  **Result**: If matched, the pair is validated and processed immediately.

---

## 3. Fraud Analysis & Comparisons

Once matched, the `FraudEngine` compares parameters between the VAS object and the POS object.

### Comparison Parameter Map

| Parameter Category | VAS Parameter | POS Parameter | Comparison Logic | Risk Level |
| :--- | :--- | :--- | :--- | :--- |
| **Payment Mode** | `ModeOfTransaction` | `ModeOfTransaction` | `vas.Mode != pos.Mode` | **High** |
| **Receipt Generation** | `ReceiptGenerationStatus` | *N/A* | `vas.ReceiptGenerationStatus == False` | **Medium** |
| **Discount** | *N/A* | `DiscountPercent` | `pos.DiscountPercent > 20` | **Medium** |
| **Refunds** | *N/A* | `RefundAmount` | `pos.RefundAmount > 0` | **Medium** |

### Missing Event Logic (Phantom Scan)
*   **Scenario**: Event (VAS or POS) exists in the file for more than **2 minutes** without a match.
*   **Logic**:
    - During the batch run, if an event's timestamp is older than 2 minutes and no counterpart is found:
    - **Action 1**: Create a "Missing Data" Transaction (Risk: High).
    - **Action 2**: Trigger an Alert.
    - **Action 3**: Archive the event to `vas_event.json` or `pos_event.json`.
    - **Action 4**: Remove the event from the source file.
*   **Risk Level**: **High**.

---

## 4. Output

*   **Transaction**: A consolidated record containing IDs from both sources, the risk level, and any triggered rules.
*   **Alert**: Generated if Risk Level is **High** or **Medium**.
