from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
from enum import Enum

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
    BillDate: str
    SessionStart: str
    SessionEnd: str
    ReceiptGenerationStatus: bool = Field(..., description="true if receipt generated, false otherwise")

class POSEvent(BaseModel):
    StoreId: str
    CashierName: str
    POSId: str
    SellerWindowId: Optional[str] = None
    BillDate: str
    SessionTime: str
    ModeOfTransaction: str = Field(default="Unknown")
    TransactionTotal: float = Field(default=0.0)
    DiscountPercent: float = Field(default=0.0)
    RefundAmount: float = Field(default=0.0)
    IsComplementary: str = Field(default="No")
    BillStatus: str = Field(default="Completed")
    VoidReason: str = Field(default="")
    CancelDate: str = Field(default="")
    ItemCount: int = Field(default=0)
    BillAmount: float = Field(default=0.0)
    PaymentReceived: float = Field(default=0.0)

    class Config:
        extra = "ignore"

# Output Models
class Transaction(BaseModel):
    id: str
    shop_id: str
    shop_name: str = ""
    cam_id: str
    pos_id: str
    cashier_name: str
    timestamp: datetime
    transaction_total: float
    risk_level: str = "Low"
    triggered_rules: List[str] = []
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
    shop_name: str = ""
    cashier_name: str
    risk_level: str
    triggered_rules: List[str] = []
    timestamp: datetime
    status: AlertStatus = AlertStatus.NEW

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }
