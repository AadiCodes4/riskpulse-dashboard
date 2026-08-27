"""
SQLAlchemy ORM models for RiskPulse.

RiskPulse tracks generic incoming "Records" -- these could represent
transactions, applications, or any other categorized event a small
business might want to monitor for risk. All data used with this model
in this repo is synthetic / fabricated for demo purposes (see
seed_and_simulate.py and the top-level README).
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, Float, DateTime, Enum, Integer
from sqlalchemy.orm import validates

from .database import Base


def _utcnow():
    return datetime.now(timezone.utc)


def _new_id():
    return uuid.uuid4().hex


class RecordStatus(str, enum.Enum):
    """Bucketed risk status derived from risk_score."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RecordCategory(str, enum.Enum):
    """A small, fixed set of demo categories for incoming records."""

    RETAIL_PURCHASE = "retail_purchase"
    ONLINE_ORDER = "online_order"
    WIRE_TRANSFER = "wire_transfer"
    SUBSCRIPTION = "subscription"
    LOAN_APPLICATION = "loan_application"
    REFUND_REQUEST = "refund_request"


class Record(Base):
    """
    A single monitored event/application/transaction.

    Fields
    ------
    id: opaque string primary key (uuid4 hex)
    submitted_at: when the record was submitted/received (UTC)
    category: one of RecordCategory
    amount: the monetary (or magnitude) value associated with the record
    applicant: a free-text label for who/what submitted it (demo only)
    risk_score: 0-100 score computed by risk_engine.score_record
    status: bucketed risk level derived from risk_score
    """

    __tablename__ = "records"

    id = Column(String(32), primary_key=True, default=_new_id)
    submitted_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)
    category = Column(Enum(RecordCategory), nullable=False, index=True)
    amount = Column(Float, nullable=False)
    applicant = Column(String(120), nullable=False, default="demo-user")
    risk_score = Column(Float, nullable=False, default=0.0, index=True)
    status = Column(Enum(RecordStatus), nullable=False, default=RecordStatus.LOW, index=True)

    # A short human-readable explanation of *why* the score came out the way
    # it did -- purely informational, built by the risk engine.
    score_reason = Column(String(400), nullable=False, default="")

    @validates("amount")
    def validate_amount(self, key, value):
        if value < 0:
            raise ValueError("amount must be non-negative")
        return value

    def to_dict(self):
        return {
            "id": self.id,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "category": self.category.value if isinstance(self.category, enum.Enum) else self.category,
            "amount": self.amount,
            "applicant": self.applicant,
            "risk_score": self.risk_score,
            "status": self.status.value if isinstance(self.status, enum.Enum) else self.status,
            "score_reason": self.score_reason,
        }
