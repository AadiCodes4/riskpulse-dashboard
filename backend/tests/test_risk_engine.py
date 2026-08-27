"""
Deterministic unit tests for the risk scoring engine.

Each expected score is computed by hand from the constants in
app/risk_engine.py so these tests will actually catch regressions if
someone changes the weights/tiers without updating expectations.
"""
import pytest

from app.models import RecordCategory, RecordStatus
from app.risk_engine import (
    score_record,
    category_score,
    amount_score,
    anomaly_score,
    status_for_score,
)


def test_category_score_known_values():
    assert category_score(RecordCategory.SUBSCRIPTION) == 3
    assert category_score(RecordCategory.WIRE_TRANSFER) == 35
    assert category_score(RecordCategory.LOAN_APPLICATION) == 30


@pytest.mark.parametrize(
    "amount,expected",
    [
        (0, 0),
        (50, 0),
        (250, 5),
        (251, 10),
        (1000, 10),
        (5000, 20),
        (10000, 30),
        (25000, 40),
        (1_000_000, 45),
    ],
)
def test_amount_score_tiers(amount, expected):
    assert amount_score(amount) == expected


def test_amount_score_rejects_negative():
    with pytest.raises(ValueError):
        amount_score(-5)


def test_anomaly_score_no_history_is_zero():
    assert anomaly_score(1000, None) == 0.0
    assert anomaly_score(1000, []) == 0.0
    assert anomaly_score(1000, [900, 950]) == 0.0  # below MIN_HISTORY_FOR_ANOMALY


def test_anomaly_score_within_normal_range_is_zero():
    # Recent history clustered around 1000, new amount close to the mean.
    history = [980, 1000, 1020, 990, 1010]
    assert anomaly_score(1005, history) == 0.0


def test_anomaly_score_flags_large_deviation():
    history = [980, 1000, 1020, 990, 1010]  # mean=1000, pstdev ~ 14.14
    result = anomaly_score(5000, history)
    assert result > 0
    assert result <= 20.0


def test_anomaly_score_zero_variance_history():
    history = [500, 500, 500]
    assert anomaly_score(500, history) == 0.0
    assert anomaly_score(600, history) == 20.0


def test_status_for_score_buckets():
    assert status_for_score(0) == RecordStatus.LOW
    assert status_for_score(25) == RecordStatus.LOW
    assert status_for_score(26) == RecordStatus.MEDIUM
    assert status_for_score(50) == RecordStatus.MEDIUM
    assert status_for_score(51) == RecordStatus.HIGH
    assert status_for_score(75) == RecordStatus.HIGH
    assert status_for_score(76) == RecordStatus.CRITICAL
    assert status_for_score(100) == RecordStatus.CRITICAL


def test_score_record_low_risk_subscription():
    result = score_record(RecordCategory.SUBSCRIPTION, 15.0)
    # category=3, amount tier(15)=0, no anomaly => total 3
    assert result.score == 3.0
    assert result.status == RecordStatus.LOW
    assert "subscription" in result.reason


def test_score_record_high_risk_wire_transfer():
    result = score_record(RecordCategory.WIRE_TRANSFER, 30_000.0)
    # category=35, amount tier(30000)=45 => total 80 => CRITICAL
    assert result.score == 80.0
    assert result.status == RecordStatus.CRITICAL


def test_score_record_with_anomaly_pushes_status_up():
    history = [1000.0, 1050.0, 980.0, 1010.0, 1005.0]
    baseline = score_record(RecordCategory.ONLINE_ORDER, 1010.0, recent_amounts=history)
    spike = score_record(RecordCategory.ONLINE_ORDER, 8000.0, recent_amounts=history)
    assert spike.score > baseline.score
    assert spike.anomaly_component > 0


def test_score_record_clamped_to_100():
    # category (wire=35) + amount tier max (45) + a manufactured huge anomaly
    # should still clamp at 100, never exceed it.
    history = [100.0, 100.0, 100.0, 100.0]
    result = score_record(RecordCategory.WIRE_TRANSFER, 999999.0, recent_amounts=history)
    assert result.score <= 100.0


def test_score_record_accepts_string_category():
    result = score_record("retail_purchase", 10.0)
    assert result.score == category_score(RecordCategory.RETAIL_PURCHASE) + amount_score(10.0)
