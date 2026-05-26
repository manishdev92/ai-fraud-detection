"""End-to-end tests aligned to Option 1 requirements."""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select

from app.config import Settings, get_settings
from app.database import AuditLog, Base, Finding, Transaction, init_db, reset_engine
from app.main import app
from app.services import FraudRuleEngine


@pytest.fixture
def client():
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ["USE_ADK"] = "true"
    os.environ["GEMINI_API_KEY"] = ""
    get_settings.cache_clear()
    reset_engine()
    init_db()
    with TestClient(app) as c:
        yield c
    reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _txn(account_id, amount, txn_type, hours_ago, **kwargs):
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4

    return Transaction(
        id=str(uuid4()),
        account_id=account_id,
        amount_usd=amount,
        txn_type=txn_type,
        channel=kwargs.get("channel", "wire"),
        country_code=kwargs.get("country_code", "US"),
        is_cross_border=kwargs.get("is_cross_border", False),
        counterparty_account=kwargs.get("counterparty"),
        created_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    )


def test_health(client):
    data = client.get("/health").json()
    assert data["status"] == "ok"
    assert data["use_adk"] is True


def test_option1_full_pipeline(client):
    """All required endpoints: generate → investigate → findings → report."""
    gen = client.post(
        "/generate-transactions",
        json={"count": 300, "fraud_ratio": 0.08, "seed": 42},
    )
    assert gen.status_code == 200
    assert gen.json()["generated"] >= 300

    inv = client.post(
        "/run-fraud-investigation",
        json={"lookback_hours": 336, "generate_report": True},
    )
    assert inv.status_code == 200
    body = inv.json()
    assert body["findings_count"] > 0
    assert body["investigation_id"]
    assert body["orchestration"] == "adk"
    assert body["data_source"] == "sqlite"
    assert body["report_id"]

    inv_id = body["investigation_id"]
    findings_resp = client.get(f"/findings?investigation_id={inv_id}&limit=20")
    assert findings_resp.status_code == 200
    findings = findings_resp.json()
    assert findings["total"] > 0

    sample = findings["findings"][0]
    assert sample["rule_id"] in ("R1", "R2", "R3", "R4", "R5")
    assert sample["explanation"]
    assert sample["evidence"]

    report = client.get(f"/reports/latest?investigation_id={inv_id}")
    assert report.status_code == 200
    assert report.json()["body_markdown"]
    assert report.json()["generated_by"] in ("template", "gemini", "template_fallback")


def test_sync_bigquery_requires_project(client):
    r = client.post("/sync-to-bigquery")
    assert r.status_code == 400


def test_audit_logs_created(client):
    client.post("/generate-transactions", json={"count": 100, "seed": 1})
    client.post("/run-fraud-investigation", json={"lookback_hours": 336})
    # Re-open via new request session — use findings endpoint as proxy; audit in same DB file
    # In-memory DB persists for TestClient lifespan
    from app.database import get_session_factory

    db = get_session_factory()()
    try:
        count = db.scalar(select(func.count()).select_from(AuditLog))
        assert count and count > 0
    finally:
        db.close()


def test_high_value_unusual(db_session):
    acct = "ACC-TEST-001"
    for i in range(10):
        db_session.add(_txn(acct, 100.0, "debit", i + 1))
    db_session.add(_txn(acct, 50_000.0, "debit", 0.5))
    db_session.commit()
    assert any(h.rule_id == "R1" for h in FraudRuleEngine(Settings()).run(db_session, account_id=acct))


def test_rapid_repeated(db_session):
    acct = "ACC-RAPID"
    for i in range(6):
        db_session.add(_txn(acct, 500.0, "transfer", 0.1 + i * 0.01))
    db_session.commit()
    settings = Settings(rapid_transfer_min_count=5)
    assert any(h.rule_id == "R2" for h in FraudRuleEngine(settings).run(db_session, account_id=acct))
