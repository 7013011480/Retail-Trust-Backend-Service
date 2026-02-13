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

*   **Mechanism**: Direct String Matching.
*   **Key**: `SellerWindowId`.
*   **Process**:
    1.  **VAS Arrival**: A VAS event arrives and waits in a buffer.
    2.  **POS Arrival**: A POS event is fetched. `SalesPoller` calculates its `SellerWindowId` using `mapping.json`.
    3.  **Comparison**: The Orchestrator compares:
        ```python
        if pos_event.SellerWindowId == vas_event.SellerWindowId:
            # Match Found!
        ```
    4.  **Result**: If matched, the pair is sent to the **Fraud Engine**.

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
*   **Scenario**: VAS Event arrives, but no matching POS event arrives within timeout (120s).
*   **Logic**:
    ```python
    if timeout_reached and vas_event_pending:
        Trigger Alert("Corresponding object is not present in VAS for POS and vise versa")
    ```
*   **Risk Level**: **High**.

---

## 4. Output

*   **Transaction**: A consolidated record containing IDs from both sources, the risk level, and any triggered rules.
*   **Alert**: Generated if Risk Level is **High** or **Medium**.
