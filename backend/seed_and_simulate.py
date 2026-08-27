#!/usr/bin/env python3
"""
seed_and_simulate.py
=====================

Populates RiskPulse with SYNTHETIC, FABRICATED-FOR-DEMO data in two phases:

1. SEED  -- writes a batch of historical records directly into the SQLite
   database (fast, no running server required) spread over the last few
   days, so the dashboard has something to look at (and so the risk
   engine's anomaly check has real recent history to compare against).

2. SIMULATE -- with the FastAPI server already running (`uvicorn
   app.main:app`), POSTs a steady stream of new synthetic records to the
   live REST API, one every `--interval` seconds, so a connected
   dashboard (via WebSocket or polling) sees live updates.

None of the data here represents real people, companies, or
transactions -- it is randomly generated for demonstration purposes only.

Usage
-----
    # from the backend/ directory, with the venv active:
    python seed_and_simulate.py --seed-count 200 --stream-count 30 --interval 1.0

    # seed only (no server needed):
    python seed_and_simulate.py --seed-count 200 --stream-count 0

    # stream only against an already-seeded DB / running server:
    python seed_and_simulate.py --seed-count 0 --stream-count 50 --interval 0.5
"""
import argparse
import random
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from app.database import SessionLocal, init_db
from app.models import Record, RecordCategory
from app.risk_engine import score_record

# --- Synthetic data generation helpers --------------------------------------

# (mean, stdev, min_floor) per category -- purely fabricated "typical"
# amount distributions for this toy domain, not derived from any real data.
CATEGORY_AMOUNT_PROFILE = {
    RecordCategory.RETAIL_PURCHASE: (40, 20, 3),
    RecordCategory.ONLINE_ORDER: (120, 60, 5),
    RecordCategory.SUBSCRIPTION: (15, 5, 1),
    RecordCategory.REFUND_REQUEST: (80, 50, 5),
    RecordCategory.LOAN_APPLICATION: (8000, 4000, 500),
    RecordCategory.WIRE_TRANSFER: (2500, 2000, 50),
}

ANOMALY_SPIKE_PROBABILITY = 0.06  # fraction of simulated records that are deliberate outliers
ANOMALY_SPIKE_MULTIPLIER_RANGE = (5, 12)

HISTORY_WINDOW = 20  # must match app.main.ANOMALY_HISTORY_WINDOW


def random_amount(category: RecordCategory, rng: random.Random, force_spike: bool = False) -> float:
    mean, stdev, floor = CATEGORY_AMOUNT_PROFILE[category]
    amount = max(floor, rng.gauss(mean, stdev))
    if force_spike:
        amount *= rng.uniform(*ANOMALY_SPIKE_MULTIPLIER_RANGE)
    return round(amount, 2)


def random_applicant(rng: random.Random) -> str:
    return f"demo-acct-{rng.randint(1000, 9999)}"


# --- Phase 1: seed historical data directly into the DB ---------------------


def seed_historical(count: int, days_back: int, seed: int) -> None:
    if count <= 0:
        print("[seed] --seed-count <= 0, skipping historical seed.")
        return

    rng = random.Random(seed)
    init_db()
    db = SessionLocal()

    categories = list(RecordCategory)
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days_back)

    # Generate `count` timestamps spread (with jitter) evenly across the
    # window, then sort so we insert in chronological order -- this way the
    # anomaly check's "recent history" for each new row only ever looks
    # backwards in time, same as it would in production.
    span_seconds = max(1, int((now - start).total_seconds()))
    timestamps = sorted(start + timedelta(seconds=rng.uniform(0, span_seconds)) for _ in range(count))

    created = 0
    for ts in timestamps:
        category = rng.choice(categories)
        force_spike = rng.random() < ANOMALY_SPIKE_PROBABILITY
        amount = random_amount(category, rng, force_spike=force_spike)

        recent = [
            row[0]
            for row in (
                db.query(Record.amount)
                .filter(Record.category == category)
                .filter(Record.submitted_at < ts)
                .order_by(Record.submitted_at.desc())
                .limit(HISTORY_WINDOW)
                .all()
            )
        ]

        result = score_record(category, amount, recent_amounts=recent)
        record = Record(
            submitted_at=ts,
            category=category,
            amount=amount,
            applicant=random_applicant(rng),
            risk_score=result.score,
            status=result.status,
            score_reason=result.reason,
        )
        db.add(record)
        created += 1
        if created % 50 == 0:
            db.commit()  # periodic commit so `recent` queries above see committed rows

    db.commit()
    db.close()
    print(f"[seed] inserted {created} synthetic historical records spanning the last {days_back} day(s).")


# --- Phase 2: simulate a live stream against the running API ---------------


def simulate_stream(count: int, interval: float, api_url: str, seed: int) -> None:
    if count <= 0:
        print("[stream] --stream-count <= 0, skipping live simulation.")
        return

    rng = random.Random(seed + 1)  # different stream than the seed phase
    categories = list(RecordCategory)
    endpoint = api_url.rstrip("/") + "/records"

    print(f"[stream] posting {count} synthetic record(s) to {endpoint} every {interval}s ...")
    ok = 0
    for i in range(count):
        category = rng.choice(categories)
        force_spike = rng.random() < ANOMALY_SPIKE_PROBABILITY
        amount = random_amount(category, rng, force_spike=force_spike)
        payload = {
            "category": category.value,
            "amount": amount,
            "applicant": random_applicant(rng),
        }
        try:
            resp = requests.post(endpoint, json=payload, timeout=5)
            resp.raise_for_status()
            body = resp.json()
            ok += 1
            spike_tag = " <-- SPIKE" if force_spike else ""
            print(
                f"[stream] {i + 1}/{count} category={body['category']:<16} "
                f"amount=${body['amount']:>10,.2f} risk_score={body['risk_score']:>5.1f} "
                f"status={body['status']:<8}{spike_tag}"
            )
        except requests.RequestException as exc:
            print(f"[stream] request {i + 1}/{count} FAILED: {exc}", file=sys.stderr)

        if i < count - 1:
            time.sleep(interval)

    print(f"[stream] done. {ok}/{count} records successfully posted.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed-count", type=int, default=150, help="number of historical records to seed directly into the DB")
    parser.add_argument("--days-back", type=int, default=7, help="spread seeded historical records over this many past days")
    parser.add_argument("--stream-count", type=int, default=30, help="number of live records to POST to the running API")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds to sleep between simulated live POSTs")
    parser.add_argument("--api-url", type=str, default="http://127.0.0.1:8000", help="base URL of the running FastAPI server")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for reproducible synthetic data")
    args = parser.parse_args()

    seed_historical(args.seed_count, args.days_back, args.seed)
    simulate_stream(args.stream_count, args.interval, args.api_url, args.seed)


if __name__ == "__main__":
    main()
