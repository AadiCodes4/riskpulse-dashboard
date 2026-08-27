"""Pydantic request/response schemas for the RiskPulse API."""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, ConfigDict

from .models import RecordCategory, RecordStatus


class RecordCreate(BaseModel):
    """Payload for POST /records. risk_score/status are computed server-side."""

    category: RecordCategory
    amount: float = Field(..., ge=0, description="Monetary amount associated with the record")
    applicant: str = Field(default="demo-user", max_length=120)
    submitted_at: Optional[datetime] = Field(
        default=None, description="Defaults to now (UTC) if omitted; useful for seeding historical data."
    )


class RecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    submitted_at: datetime
    category: RecordCategory
    amount: float
    applicant: str
    risk_score: float
    status: RecordStatus
    score_reason: str


class RecordList(BaseModel):
    total: int
    items: List[RecordOut]


class CategoryStats(BaseModel):
    category: str
    count: int
    avg_risk_score: float
    max_risk_score: float
    avg_amount: float


class StatsOut(BaseModel):
    total_records: int
    overall_avg_risk_score: float
    status_breakdown: dict
    by_category: List[CategoryStats]
    high_risk_last_hour: int
