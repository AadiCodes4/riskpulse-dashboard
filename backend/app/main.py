"""
RiskPulse FastAPI application.

Run with:
    uvicorn app.main:app --reload --port 8000

Endpoints
---------
POST   /records            create + auto-score a new record
GET    /records             list/filter records
GET    /records/{id}        fetch a single record
GET    /stats               aggregate counts / avg risk by category
WS     /ws                  live push of newly-created records
GET    /health               basic liveness check

NOTE: All data flowing through this API in this repo's demo scripts is
synthetic (see seed_and_simulate.py). This is an independent portfolio
project; the risk-scoring logic is a simplified educational
demonstration, not a real financial risk model.
"""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models, schemas
from .database import get_db, init_db, SessionLocal
from .risk_engine import score_record


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="RiskPulse API",
    description=(
        "Educational, fully-synthetic real-time risk monitoring dashboard API. "
        "Not affiliated with, and does not contain data or logic from, any employer's systems."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # demo project: wide open so the static frontend can hit it from anywhere
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# WebSocket connection manager for live broadcasting of new records
# ---------------------------------------------------------------------------


class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self.active.append(ws)

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            if ws in self.active:
                self.active.remove(ws)

    async def broadcast(self, message: dict):
        dead = []
        for ws in list(self.active):
            try:
                await ws.send_text(json.dumps(message, default=str))
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)


manager = ConnectionManager()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # We don't expect clients to send anything meaningful; just keep
            # the connection alive and drain any pings/messages.
            await websocket.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(websocket)


ANOMALY_HISTORY_WINDOW = 20  # how many recent same-category records to use for the z-score check


def _recent_amounts_for_category(db: Session, category: models.RecordCategory, exclude_id: Optional[str] = None):
    q = (
        db.query(models.Record.amount)
        .filter(models.Record.category == category)
        .order_by(models.Record.submitted_at.desc())
        .limit(ANOMALY_HISTORY_WINDOW)
    )
    return [row[0] for row in q.all()]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/records", response_model=schemas.RecordOut, status_code=201)
async def create_record(payload: schemas.RecordCreate, db: Session = Depends(get_db)):
    recent_amounts = _recent_amounts_for_category(db, payload.category)
    result = score_record(payload.category, payload.amount, recent_amounts=recent_amounts)

    record = models.Record(
        category=payload.category,
        amount=payload.amount,
        applicant=payload.applicant or "demo-user",
        submitted_at=payload.submitted_at or datetime.now(timezone.utc),
        risk_score=result.score,
        status=result.status,
        score_reason=result.reason,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    # Push the new record to any connected WebSocket clients. This endpoint
    # is `async def` (runs on the main event loop, unlike plain `def`
    # endpoints which FastAPI offloads to a threadpool) specifically so this
    # await works correctly.
    await manager.broadcast({"type": "new_record", "record": record.to_dict()})

    return record


@app.get("/records", response_model=schemas.RecordList)
def list_records(
    category: Optional[models.RecordCategory] = None,
    status: Optional[models.RecordStatus] = None,
    min_risk: Optional[float] = Query(default=None, ge=0, le=100),
    max_risk: Optional[float] = Query(default=None, ge=0, le=100),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(models.Record)
    if category is not None:
        q = q.filter(models.Record.category == category)
    if status is not None:
        q = q.filter(models.Record.status == status)
    if min_risk is not None:
        q = q.filter(models.Record.risk_score >= min_risk)
    if max_risk is not None:
        q = q.filter(models.Record.risk_score <= max_risk)

    total = q.count()
    items = q.order_by(models.Record.submitted_at.desc()).offset(offset).limit(limit).all()
    return {"total": total, "items": items}


@app.get("/records/{record_id}", response_model=schemas.RecordOut)
def get_record(record_id: str, db: Session = Depends(get_db)):
    record = db.query(models.Record).filter(models.Record.id == record_id).first()
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")
    return record


@app.get("/stats", response_model=schemas.StatsOut)
def get_stats(db: Session = Depends(get_db)):
    total_records = db.query(func.count(models.Record.id)).scalar() or 0
    overall_avg = db.query(func.avg(models.Record.risk_score)).scalar() or 0.0

    status_rows = (
        db.query(models.Record.status, func.count(models.Record.id)).group_by(models.Record.status).all()
    )
    status_breakdown = {s.value: 0 for s in models.RecordStatus}
    for status_val, count in status_rows:
        key = status_val.value if hasattr(status_val, "value") else status_val
        status_breakdown[key] = count

    by_category_rows = (
        db.query(
            models.Record.category,
            func.count(models.Record.id),
            func.avg(models.Record.risk_score),
            func.max(models.Record.risk_score),
            func.avg(models.Record.amount),
        )
        .group_by(models.Record.category)
        .all()
    )
    by_category = [
        schemas.CategoryStats(
            category=cat.value if hasattr(cat, "value") else cat,
            count=count,
            avg_risk_score=round(avg_risk or 0.0, 2),
            max_risk_score=round(max_risk or 0.0, 2),
            avg_amount=round(avg_amount or 0.0, 2),
        )
        for cat, count, avg_risk, max_risk, avg_amount in by_category_rows
    ]

    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    high_risk_last_hour = (
        db.query(func.count(models.Record.id))
        .filter(models.Record.submitted_at >= one_hour_ago)
        .filter(models.Record.risk_score >= 75)
        .scalar()
        or 0
    )

    return schemas.StatsOut(
        total_records=total_records,
        overall_avg_risk_score=round(overall_avg, 2),
        status_breakdown=status_breakdown,
        by_category=by_category,
        high_risk_last_hour=high_risk_last_hour,
    )
