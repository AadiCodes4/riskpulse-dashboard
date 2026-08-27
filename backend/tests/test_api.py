"""
API tests for RiskPulse using FastAPI's TestClient.

Each test gets a fresh isolated SQLite database (a temp file) so tests
don't interfere with each other or with any real dev database.
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    """Build a TestClient wired to a brand-new temp SQLite DB per test."""
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    # Import (or re-import) app modules *after* setting DATABASE_URL so the
    # engine binds to our temp file rather than the default dev DB.
    import importlib
    from app import database as database_module

    importlib.reload(database_module)
    from app import models as models_module

    importlib.reload(models_module)
    from app import main as main_module

    importlib.reload(main_module)

    with TestClient(main_module.app) as test_client:
        yield test_client

    os.remove(db_path)


def make_record_payload(category="retail_purchase", amount=100.0, applicant="alice"):
    return {"category": category, "amount": amount, "applicant": applicant}


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_create_record_returns_scored_record(client):
    resp = client.post("/records", json=make_record_payload(category="wire_transfer", amount=30000))
    assert resp.status_code == 201
    body = resp.json()
    assert body["category"] == "wire_transfer"
    assert body["amount"] == 30000
    assert body["risk_score"] == 80.0  # 35 (category) + 45 (amount tier)
    assert body["status"] == "critical"
    assert "id" in body and body["id"]
    assert "submitted_at" in body


def test_create_record_rejects_negative_amount(client):
    resp = client.post("/records", json=make_record_payload(amount=-10))
    assert resp.status_code == 422


def test_create_record_rejects_bad_category(client):
    resp = client.post("/records", json=make_record_payload(category="not_a_real_category"))
    assert resp.status_code == 422


def test_list_records_empty_initially(client):
    resp = client.get("/records")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["items"] == []


def test_list_and_get_record_roundtrip(client):
    create_resp = client.post("/records", json=make_record_payload(category="subscription", amount=15))
    created = create_resp.json()

    list_resp = client.get("/records")
    assert list_resp.status_code == 200
    listed = list_resp.json()
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == created["id"]

    get_resp = client.get(f"/records/{created['id']}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == created["id"]


def test_get_missing_record_returns_404(client):
    resp = client.get("/records/does-not-exist")
    assert resp.status_code == 404


def test_list_records_filters_by_category_and_risk(client):
    client.post("/records", json=make_record_payload(category="subscription", amount=15))
    client.post("/records", json=make_record_payload(category="wire_transfer", amount=30000))

    by_category = client.get("/records", params={"category": "subscription"}).json()
    assert by_category["total"] == 1
    assert by_category["items"][0]["category"] == "subscription"

    high_risk = client.get("/records", params={"min_risk": 50}).json()
    assert high_risk["total"] == 1
    assert high_risk["items"][0]["category"] == "wire_transfer"


def test_stats_reflects_created_records(client):
    client.post("/records", json=make_record_payload(category="subscription", amount=15))
    client.post("/records", json=make_record_payload(category="wire_transfer", amount=30000))
    client.post("/records", json=make_record_payload(category="wire_transfer", amount=100))

    stats = client.get("/stats").json()
    assert stats["total_records"] == 3

    cats = {c["category"]: c for c in stats["by_category"]}
    assert cats["subscription"]["count"] == 1
    assert cats["wire_transfer"]["count"] == 2
    assert stats["status_breakdown"]["critical"] >= 1


def test_websocket_receives_new_record_broadcast(client):
    with client.websocket_connect("/ws") as ws:
        client.post("/records", json=make_record_payload(category="online_order", amount=250))
        message = ws.receive_json()
        assert message["type"] == "new_record"
        assert message["record"]["category"] == "online_order"
