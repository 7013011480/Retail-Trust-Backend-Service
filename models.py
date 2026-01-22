from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from enum import Enum

class TransactionMode(str, Enum):
    CASH = "Cash"
    CARD = "Card"
    UPI = "UPI"

class TransactionStatus(str, Enum):
    GENUINE = "genuine"
    FRAUDULENT = "fraudulent"
    SUSPICIOUS = "suspicious"
    PENDING = "pending"

class AlertStatus(str, Enum):
    NEW = "new"
    REVIEWING = "reviewing"
    RESOLVED = "resolved"
    FRAUDULENT = "Fraudulent"
    PENDING_REVIEW = "Pending for review"
    GENUINE = "Genuine"

# Input Events
class VASEvent(BaseModel):
    StoreId: str
    CamId: str
    SellerWindowId: str
    SessionId: str
    BillDate: str # YYYY-MM-DD
    SessionStart: float # Unix Timestamp
    SessionEnd: float # Unix Timestamp
    ModeOfTransaction: TransactionMode
    ReceiptGenerationStatus: bool = Field(..., description="true if receipt generated, false otherwise")

class POSEvent(BaseModel):
    StoreId: str
    CashierName: str
    POSId: str
    BillDate: str
    SessionTime: float
    ModeOfTransaction: TransactionMode
    TransactionTotal: float = Field(default=0.0) # Added to match frontend Transaction needs

# Output Models (Matching Frontend)
class Transaction(BaseModel):
    id: str
    shop_id: str
    cam_id: str
    pos_id: str
    cashier_name: str
    timestamp: datetime
    transaction_total: float
    fraud_probability_score: float
    status: TransactionStatus = TransactionStatus.PENDING
    fraud_category: Optional[str] = None
    notes: Optional[str] = None

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }

class Alert(BaseModel):
    id: str
    transaction_id: str
    shop_id: str
    cashier_name: str
    fraud_probability_score: float
    timestamp: datetime
    status: AlertStatus = AlertStatus.NEW

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }
