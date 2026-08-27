"""
RiskPulse risk scoring engine.

This is an intentionally SIMPLE, EDUCATIONAL, rule-based + lightweight
statistical scorer. It is NOT a real financial risk model and should not
be treated as one -- it exists to make a portfolio dashboard project have
a genuine, testable, non-trivial piece of logic behind it.

The final risk_score (0-100) is the sum of three independent components,
clamped to [0, 100]:

1. Category weight (0-35)
   A fixed, hand-picked weight per category representing how inherently
   risky that *kind* of record tends to be in this toy domain (e.g. a
   wire transfer is treated as riskier than a subscription renewal).

2. Amount tier (0-45)
   A step function of the record's `amount`. Larger amounts are
   considered progressively riskier. Tiers are deterministic so they are
   easy to unit test.

3. Anomaly component (0-20)
   A basic z-score check: if we're given a rolling window of recent
   amounts for the same category, we measure how many standard
   deviations away from that recent mean the new amount is. Values far
   outside recent norms add extra risk. With fewer than
   MIN_HISTORY_FOR_ANOMALY samples, this component is skipped (0).

The three components are independent by design (easy to reason about
and to test), which is a deliberate simplification -- a production risk
model would likely use interaction terms, calibrated probabilities, and
a lot more data.
"""
from dataclasses import dataclass, field
from statistics import mean, pstdev
from typing import Iterable, List, Optional

from .models import RecordCategory, RecordStatus

# --- 1. Category weights ----------------------------------------------------

CATEGORY_WEIGHTS = {
    RecordCategory.RETAIL_PURCHASE: 5,
    RecordCategory.ONLINE_ORDER: 10,
    RecordCategory.SUBSCRIPTION: 3,
    RecordCategory.REFUND_REQUEST: 18,
    RecordCategory.LOAN_APPLICATION: 30,
    RecordCategory.WIRE_TRANSFER: 35,
}

# --- 2. Amount tiers ---------------------------------------------------------
# (upper_bound_inclusive, score). The first tier whose upper bound is >=
# amount wins. The last tier's bound is effectively infinite.
AMOUNT_TIERS = [
    (50, 0),
    (250, 5),
    (1_000, 10),
    (5_000, 20),
    (10_000, 30),
    (25_000, 40),
    (float("inf"), 45),
]

# --- 3. Anomaly / z-score component -----------------------------------------

MIN_HISTORY_FOR_ANOMALY = 3
ANOMALY_Z_FLOOR = 1.0  # z-scores below this contribute nothing
ANOMALY_MAX_SCORE = 20.0
ANOMALY_SCALE = 8.0  # points added per unit of z above the floor

# --- Status buckets ----------------------------------------------------------

STATUS_THRESHOLDS = [
    (25, RecordStatus.LOW),
    (50, RecordStatus.MEDIUM),
    (75, RecordStatus.HIGH),
    (float("inf"), RecordStatus.CRITICAL),
]


@dataclass
class ScoreResult:
    score: float
    status: RecordStatus
    reason: str
    category_component: float
    amount_component: float
    anomaly_component: float


def category_score(category: RecordCategory) -> float:
    """Base risk contribution from the record's category."""
    return float(CATEGORY_WEIGHTS.get(category, 15))  # unknown categories -> mid weight


def amount_score(amount: float) -> float:
    """Deterministic step-function risk contribution from the amount."""
    if amount < 0:
        raise ValueError("amount must be non-negative")
    for upper_bound, score in AMOUNT_TIERS:
        if amount <= upper_bound:
            return float(score)
    return float(AMOUNT_TIERS[-1][1])  # unreachable, safety net


def anomaly_score(amount: float, recent_amounts: Optional[Iterable[float]]) -> float:
    """
    Z-score based anomaly contribution.

    Compares `amount` against the mean/std-dev of `recent_amounts` (e.g.
    the last N records in the same category). Returns 0 if there isn't
    enough history, or if the recent history has zero variance and the
    new amount matches it exactly.
    """
    if recent_amounts is None:
        return 0.0
    history: List[float] = list(recent_amounts)
    if len(history) < MIN_HISTORY_FOR_ANOMALY:
        return 0.0

    hist_mean = mean(history)
    hist_std = pstdev(history)

    if hist_std == 0:
        # No variance in recent history -- any deviation at all is notable.
        return 0.0 if amount == hist_mean else ANOMALY_MAX_SCORE

    z = abs(amount - hist_mean) / hist_std
    if z <= ANOMALY_Z_FLOOR:
        return 0.0
    return float(min(ANOMALY_MAX_SCORE, (z - ANOMALY_Z_FLOOR) * ANOMALY_SCALE))


def status_for_score(score: float) -> RecordStatus:
    for upper_bound, status in STATUS_THRESHOLDS:
        if score <= upper_bound:
            return status
    return RecordStatus.CRITICAL  # unreachable, safety net


def score_record(
    category: RecordCategory,
    amount: float,
    recent_amounts: Optional[Iterable[float]] = None,
) -> ScoreResult:
    """
    Compute a full risk assessment for a candidate record.

    Parameters
    ----------
    category: RecordCategory (or matching string value)
    amount: non-negative float
    recent_amounts: optional iterable of recent amounts *for the same
        category*, most-recent-window, used for the anomaly check.

    Returns
    -------
    ScoreResult with the final clamped score, bucketed status, a short
    human-readable reason string, and the three raw components (useful
    for tests/debugging/UI tooltips).
    """
    if isinstance(category, str):
        category = RecordCategory(category)

    cat_c = category_score(category)
    amt_c = amount_score(amount)
    anom_c = anomaly_score(amount, recent_amounts)

    raw_total = cat_c + amt_c + anom_c
    total = max(0.0, min(100.0, raw_total))
    status = status_for_score(total)

    reason_parts = [
        f"category={category.value}(+{cat_c:g})",
        f"amount=${amount:,.2f}(+{amt_c:g})",
    ]
    if anom_c > 0:
        reason_parts.append(f"anomaly-vs-recent-history(+{anom_c:.1f})")
    reason = ", ".join(reason_parts)

    return ScoreResult(
        score=round(total, 2),
        status=status,
        reason=reason,
        category_component=cat_c,
        amount_component=amt_c,
        anomaly_component=round(anom_c, 2),
    )
