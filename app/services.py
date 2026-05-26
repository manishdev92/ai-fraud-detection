"""
Business logic: fraud rules, mock transactions, audit trail, BigQuery, and GCS.

Consolidates fraud detection, transaction generation, audit logging,
BigQuery sync/query, and compliance report storage.
"""

from __future__ import annotations

import json
import logging
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from faker import Faker
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import AuditLog, Finding, Report, Transaction

try:
    from google.cloud import bigquery
except ImportError:  # pragma: no cover
    bigquery = None  # type: ignore

try:
    from google.cloud import storage
except ImportError:  # pragma: no cover
    storage = None  # type: ignore

logger = logging.getLogger(__name__)
faker = Faker()

__all__ = [
    "BigQueryService",
    "FraudRuleEngine",
    "RuleHit",
    "StorageService",
    "generate_transactions",
    "log_audit",
    "sync_transactions_to_bigquery",
]


# ---------------------------------------------------------------------------
# Fraud rule engine
# ---------------------------------------------------------------------------


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class RuleHit:
    transaction_id: str
    account_id: str
    rule_id: str
    rule_name: str
    severity: str
    risk_score: float
    explanation: str
    evidence: dict[str, Any]


class FraudRuleEngine:
    def __init__(self, settings: Settings):
        self.settings = settings

    def run(
        self,
        db: Session,
        *,
        account_id: Optional[str] = None,
        lookback_hours: int = 168,
    ) -> list[RuleHit]:
        since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        stmt = select(Transaction)
        if account_id:
            stmt = stmt.where(Transaction.account_id == account_id)
        txns = list(db.scalars(stmt).all())
        txns = [t for t in txns if _as_utc(t.created_at) >= since]
        if not txns:
            return []

        by_account: dict[str, list[Transaction]] = defaultdict(list)
        for t in txns:
            t.created_at = _as_utc(t.created_at)
            by_account[t.account_id].append(t)

        hits: list[RuleHit] = []
        for acct, acct_txns in by_account.items():
            acct_txns.sort(key=lambda x: x.created_at)
            hits.extend(self._rule_high_value_unusual(acct, acct_txns))
            hits.extend(self._rule_rapid_repeated(acct, acct_txns))
            hits.extend(self._rule_mule_pattern(acct, acct_txns))
            hits.extend(self._rule_cross_border(acct, acct_txns))
            hits.extend(self._rule_velocity_spike(acct, acct_txns))

        return hits

    def _rule_high_value_unusual(self, account_id: str, txns: list[Transaction]) -> list[RuleHit]:
        hits: list[RuleHit] = []
        amounts = [t.amount_usd for t in txns]
        if not amounts:
            return hits
        avg = sum(amounts) / len(amounts)
        threshold = max(self.settings.high_value_threshold_usd, avg * 3)

        for t in txns:
            if t.amount_usd >= threshold and t.amount_usd > avg * 3:
                hits.append(
                    RuleHit(
                        transaction_id=t.id,
                        account_id=account_id,
                        rule_id="R1",
                        rule_name="high_value_unusual",
                        severity="high" if t.amount_usd > threshold * 2 else "medium",
                        risk_score=min(100.0, 40 + (t.amount_usd / threshold) * 20),
                        explanation=(
                            f"Transaction ${t.amount_usd:,.2f} exceeds account average "
                            f"${avg:,.2f} by >3x and global threshold ${threshold:,.2f}."
                        ),
                        evidence={
                            "amount_usd": t.amount_usd,
                            "account_avg_usd": round(avg, 2),
                            "threshold_usd": round(threshold, 2),
                            "channel": t.channel,
                        },
                    )
                )
        return hits

    def _rule_rapid_repeated(self, account_id: str, txns: list[Transaction]) -> list[RuleHit]:
        hits: list[RuleHit] = []
        window = timedelta(minutes=self.settings.rapid_transfer_window_minutes)
        min_count = self.settings.rapid_transfer_min_count

        transfer_txns = [t for t in txns if t.txn_type in ("transfer", "debit")]
        for anchor in transfer_txns:
            anchor_at = _as_utc(anchor.created_at)
            cluster = [
                t
                for t in transfer_txns
                if anchor_at <= _as_utc(t.created_at) <= anchor_at + window
            ]
            if len(cluster) >= min_count:
                hits.append(
                    RuleHit(
                        transaction_id=anchor.id,
                        account_id=account_id,
                        rule_id="R2",
                        rule_name="rapid_repeated_transfers",
                        severity="high",
                        risk_score=75.0 + min(20, len(cluster)),
                        explanation=(
                            f"{len(cluster)} transfers within "
                            f"{self.settings.rapid_transfer_window_minutes} minutes "
                            f"starting at {anchor_at.isoformat()}."
                        ),
                        evidence={
                            "cluster_size": len(cluster),
                            "window_minutes": self.settings.rapid_transfer_window_minutes,
                            "transaction_ids": [t.id for t in cluster[:10]],
                        },
                    )
                )
                break
        return hits

    def _rule_mule_pattern(self, account_id: str, txns: list[Transaction]) -> list[RuleHit]:
        hits: list[RuleHit] = []
        now = datetime.now(timezone.utc)
        window = timedelta(hours=self.settings.mule_outbound_hours)

        recent = [t for t in txns if _as_utc(t.created_at) >= now - window]
        credits = [t for t in recent if t.txn_type == "credit"]
        debits = [t for t in recent if t.txn_type == "debit"]

        sources = {t.counterparty_account for t in credits if t.counterparty_account}
        if len(sources) >= self.settings.mule_inbound_min_sources and debits:
            large_out = max(debits, key=lambda x: x.amount_usd)
            inbound_sum = sum(t.amount_usd for t in credits)
            if large_out.amount_usd >= inbound_sum * 0.7:
                hits.append(
                    RuleHit(
                        transaction_id=large_out.id,
                        account_id=account_id,
                        rule_id="R3",
                        rule_name="mule_account_pattern",
                        severity="critical",
                        risk_score=92.0,
                        explanation=(
                            f"Account received funds from {len(sources)} distinct sources "
                            f"(${inbound_sum:,.2f} inbound) then outbound wire "
                            f"${large_out.amount_usd:,.2f} within {self.settings.mule_outbound_hours}h."
                        ),
                        evidence={
                            "inbound_sources": len(sources),
                            "inbound_total_usd": round(inbound_sum, 2),
                            "outbound_usd": large_out.amount_usd,
                            "outbound_country": large_out.country_code,
                        },
                    )
                )
        return hits

    def _rule_cross_border(self, account_id: str, txns: list[Transaction]) -> list[RuleHit]:
        hits: list[RuleHit] = []
        domestic = [t for t in txns if not t.is_cross_border and t.country_code == "US"]
        cross = [t for t in txns if t.is_cross_border]

        if len(domestic) < 5 and not cross:
            return hits

        domestic_ratio = len(domestic) / max(len(txns), 1)
        for t in cross:
            if domestic_ratio > 0.85 or t.amount_usd >= 5000:
                hits.append(
                    RuleHit(
                        transaction_id=t.id,
                        account_id=account_id,
                        rule_id="R4",
                        rule_name="cross_border_anomaly",
                        severity="medium" if t.amount_usd < 10000 else "high",
                        risk_score=55.0 + min(35, t.amount_usd / 1000),
                        explanation=(
                            f"Cross-border {t.country_code} transaction ${t.amount_usd:,.2f} "
                            f"deviates from account domestic ratio {domestic_ratio:.0%}."
                        ),
                        evidence={
                            "country_code": t.country_code,
                            "amount_usd": t.amount_usd,
                            "domestic_ratio": round(domestic_ratio, 3),
                            "channel": t.channel,
                        },
                    )
                )
        return hits

    def _rule_velocity_spike(self, account_id: str, txns: list[Transaction]) -> list[RuleHit]:
        hits: list[RuleHit] = []
        if len(txns) < 10:
            return hits

        hourly: dict[str, list[Transaction]] = defaultdict(list)
        for t in txns:
            key = _as_utc(t.created_at).strftime("%Y-%m-%d-%H")
            hourly[key].append(t)

        counts = [len(v) for v in hourly.values()]
        avg_hourly = sum(counts) / len(counts)
        threshold = avg_hourly * self.settings.velocity_spike_multiplier

        for hour_key, hour_txns in hourly.items():
            if len(hour_txns) >= max(threshold, 15):
                anchor = hour_txns[0]
                hits.append(
                    RuleHit(
                        transaction_id=anchor.id,
                        account_id=account_id,
                        rule_id="R5",
                        rule_name="account_velocity_spike",
                        severity="high",
                        risk_score=70.0 + min(25, len(hour_txns) - threshold),
                        explanation=(
                            f"{len(hour_txns)} transactions in hour {hour_key} vs "
                            f"baseline {avg_hourly:.1f}/hour "
                            f"(>{self.settings.velocity_spike_multiplier}x)."
                        ),
                        evidence={
                            "hour": hour_key,
                            "txn_count": len(hour_txns),
                            "baseline_hourly_avg": round(avg_hourly, 2),
                            "multiplier_threshold": self.settings.velocity_spike_multiplier,
                        },
                    )
                )
                break
        return hits


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


def log_audit(
    db: Session,
    *,
    event_type: str,
    actor: str,
    resource_type: str,
    message: str,
    resource_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> AuditLog:
    entry = AuditLog(
        event_type=event_type,
        actor=actor,
        resource_type=resource_type,
        resource_id=resource_id,
        message=message,
        metadata_json=json.dumps(metadata) if metadata else None,
    )
    db.add(entry)
    db.flush()
    return entry


# ---------------------------------------------------------------------------
# Mock transaction generator
# ---------------------------------------------------------------------------

COUNTRIES = ["US", "US", "US", "US", "CA", "GB", "MX", "NG", "IN", "AE"]
CHANNELS = ["ach", "wire", "card", "internal"]
MCC = ["5411", "6011", "4829", "7995", "5732"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _make_txn(
    *,
    account_id: str,
    amount: float,
    txn_type: str,
    channel: str,
    country: str,
    created_at: datetime,
    counterparty: Optional[str] = None,
    cross_border: bool = False,
    description: str = "",
) -> Transaction:
    return Transaction(
        id=str(uuid4()),
        account_id=account_id,
        counterparty_account=counterparty,
        amount_usd=round(amount, 2),
        currency="USD",
        txn_type=txn_type,
        channel=channel,
        country_code=country,
        is_cross_border=cross_border,
        merchant_category=random.choice(MCC),
        description=description or faker.sentence(nb_words=6),
        created_at=created_at,
    )


def _inject_fraud_patterns(
    accounts: list[str],
    base_time: datetime,
) -> list[Transaction]:
    """Create deliberate fraud patterns for rule validation."""
    txns: list[Transaction] = []
    mule = accounts[0]
    victim = accounts[1] if len(accounts) > 1 else accounts[0]

    txns.append(
        _make_txn(
            account_id=victim,
            amount=87_500.0,
            txn_type="debit",
            channel="wire",
            country="US",
            created_at=base_time - timedelta(hours=2),
            cross_border=False,
            description="FRAUD_SEED: high_value_unusual",
        )
    )

    rapid_account = accounts[2] if len(accounts) > 2 else victim
    for i in range(7):
        txns.append(
            _make_txn(
                account_id=rapid_account,
                amount=random.uniform(200, 800),
                txn_type="transfer",
                channel="internal",
                country="US",
                created_at=base_time - timedelta(minutes=25 - i * 3),
                counterparty=random.choice(accounts),
                description="FRAUD_SEED: rapid_repeated",
            )
        )

    sources = accounts[3:7] if len(accounts) >= 7 else accounts[:4]
    for i, src in enumerate(sources):
        txns.append(
            _make_txn(
                account_id=mule,
                amount=random.uniform(900, 2500),
                txn_type="credit",
                channel="ach",
                country="US",
                created_at=base_time - timedelta(hours=20 - i),
                counterparty=src,
                description="FRAUD_SEED: mule_inbound",
            )
        )
    txns.append(
        _make_txn(
            account_id=mule,
            amount=sum(
                t.amount_usd for t in txns if t.account_id == mule and t.txn_type == "credit"
            ),
            txn_type="debit",
            channel="wire",
            country="NG",
            created_at=base_time - timedelta(hours=2),
            cross_border=True,
            description="FRAUD_SEED: mule_outbound",
        )
    )

    txns.append(
        _make_txn(
            account_id=accounts[5] if len(accounts) > 5 else victim,
            amount=15_200.0,
            txn_type="debit",
            channel="wire",
            country="RU",
            created_at=base_time - timedelta(hours=5),
            cross_border=True,
            description="FRAUD_SEED: cross_border_anomaly",
        )
    )

    velocity_acct = accounts[6] if len(accounts) > 6 else victim
    for i in range(25):
        txns.append(
            _make_txn(
                account_id=velocity_acct,
                amount=random.uniform(50, 400),
                txn_type="debit",
                channel="card",
                country="US",
                created_at=base_time - timedelta(hours=1, minutes=50 - i * 2),
                description="FRAUD_SEED: velocity_spike",
            )
        )

    return txns


def generate_transactions(
    db: Session,
    *,
    count: int,
    fraud_ratio: float,
    seed: Optional[int] = None,
) -> tuple[int, int]:
    if seed is not None:
        random.seed(seed)
        Faker.seed(seed)

    num_accounts = max(20, count // 25)
    accounts = [f"ACC-{faker.uuid4()[:8].upper()}" for _ in range(num_accounts)]

    base_time = _utc_now()
    transactions: list[Transaction] = []

    for _ in range(count):
        acct = random.choice(accounts)
        amount = round(random.lognormvariate(5.5, 1.2), 2)
        amount = min(amount, 5000.0)
        country = random.choice(COUNTRIES)
        cross = country != "US" and random.random() < 0.15
        created = base_time - timedelta(
            hours=random.randint(0, 336),
            minutes=random.randint(0, 59),
        )
        transactions.append(
            _make_txn(
                account_id=acct,
                amount=amount,
                txn_type=random.choice(["debit", "credit", "transfer"]),
                channel=random.choice(CHANNELS),
                country=country,
                created_at=created,
                counterparty=random.choice(accounts) if random.random() < 0.4 else None,
                cross_border=cross,
            )
        )

    fraud_inject = max(1, int(count * fraud_ratio))
    if fraud_inject > 0:
        transactions.extend(_inject_fraud_patterns(accounts, base_time))

    db.bulk_save_objects(transactions)
    db.commit()

    log_audit(
        db,
        event_type="transactions.generated",
        actor="system",
        resource_type="transaction_batch",
        message=f"Generated {len(transactions)} mock transactions",
        metadata={"count": len(transactions), "accounts": num_accounts, "seed": seed},
    )
    db.commit()

    return len(transactions), num_accounts


# ---------------------------------------------------------------------------
# BigQuery
# ---------------------------------------------------------------------------

TRANSACTIONS_SCHEMA = [
    bigquery.SchemaField("id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("account_id", "STRING"),
    bigquery.SchemaField("counterparty_account", "STRING"),
    bigquery.SchemaField("amount_usd", "FLOAT"),
    bigquery.SchemaField("currency", "STRING"),
    bigquery.SchemaField("txn_type", "STRING"),
    bigquery.SchemaField("channel", "STRING"),
    bigquery.SchemaField("country_code", "STRING"),
    bigquery.SchemaField("is_cross_border", "BOOL"),
    bigquery.SchemaField("merchant_category", "STRING"),
    bigquery.SchemaField("description", "STRING"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
] if bigquery else []

FINDINGS_SCHEMA = [
    bigquery.SchemaField("id", "STRING"),
    bigquery.SchemaField("investigation_id", "STRING"),
    bigquery.SchemaField("transaction_id", "STRING"),
    bigquery.SchemaField("account_id", "STRING"),
    bigquery.SchemaField("rule_id", "STRING"),
    bigquery.SchemaField("rule_name", "STRING"),
    bigquery.SchemaField("severity", "STRING"),
    bigquery.SchemaField("risk_score", "FLOAT"),
    bigquery.SchemaField("explanation", "STRING"),
    bigquery.SchemaField("evidence_json", "STRING"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
] if bigquery else []

REPORTS_SCHEMA = [
    bigquery.SchemaField("id", "STRING"),
    bigquery.SchemaField("investigation_id", "STRING"),
    bigquery.SchemaField("title", "STRING"),
    bigquery.SchemaField("body_markdown", "STRING"),
    bigquery.SchemaField("generated_by", "STRING"),
    bigquery.SchemaField("gcs_uri", "STRING"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
] if bigquery else []


class BigQueryService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: Optional[Any] = None

    @property
    def enabled(self) -> bool:
        return self.settings.gcp_enabled and bigquery is not None

    @property
    def client(self):
        if not self.enabled:
            raise RuntimeError("BigQuery is not enabled. Set USE_BIGQUERY=true and GCP_PROJECT_ID.")
        if self._client is None:
            self._client = bigquery.Client(
                project=self.settings.gcp_project_id,
                location=self.settings.bigquery_location,
            )
        return self._client

    def dataset_ref(self) -> str:
        return f"{self.settings.gcp_project_id}.{self.settings.bigquery_dataset_id}"

    def table_id(self, name: str) -> str:
        return f"{self.dataset_ref()}.{name}"

    def ensure_tables(self) -> None:
        if not self.enabled:
            return
        dataset = bigquery.Dataset(self.dataset_ref())
        dataset.location = self.settings.bigquery_location
        self.client.create_dataset(dataset, exists_ok=True)

        for table_name, schema in [
            ("transactions", TRANSACTIONS_SCHEMA),
            ("findings", FINDINGS_SCHEMA),
            ("reports", REPORTS_SCHEMA),
        ]:
            table = bigquery.Table(self.table_id(table_name), schema=schema)
            self.client.create_table(table, exists_ok=True)

    def insert_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        if not rows or not self.enabled:
            return
        errors = self.client.insert_rows_json(self.table_id(table_name), rows)
        if errors:
            raise RuntimeError(f"BigQuery insert errors: {errors}")

    def query_transactions(
        self,
        *,
        account_id: Optional[str] = None,
        lookback_hours: int = 168,
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            return []

        filter_acct = ""
        if account_id:
            filter_acct = "AND account_id = @account_id"

        sql = f"""
        SELECT *
        FROM `{self.table_id("transactions")}`
        WHERE created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @hours HOUR)
        {filter_acct}
        ORDER BY created_at DESC
        LIMIT 50000
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("hours", "INT64", lookback_hours),
            ]
        )
        if account_id:
            job_config.query_parameters.append(
                bigquery.ScalarQueryParameter("account_id", "STRING", account_id)
            )

        result = self.client.query(sql, job_config=job_config).result()
        return [dict(row) for row in result]

    @staticmethod
    def txn_row_from_orm(txn: Transaction) -> dict[str, Any]:
        return {
            "id": txn.id,
            "account_id": txn.account_id,
            "counterparty_account": txn.counterparty_account,
            "amount_usd": txn.amount_usd,
            "currency": txn.currency,
            "txn_type": txn.txn_type,
            "channel": txn.channel,
            "country_code": txn.country_code,
            "is_cross_border": txn.is_cross_border,
            "merchant_category": txn.merchant_category,
            "description": txn.description,
            "created_at": txn.created_at.isoformat() if txn.created_at else None,
        }

    @staticmethod
    def finding_row(finding: Finding) -> dict[str, Any]:
        return {
            "id": finding.id,
            "investigation_id": finding.investigation_id,
            "transaction_id": finding.transaction_id,
            "account_id": finding.account_id,
            "rule_id": finding.rule_id,
            "rule_name": finding.rule_name,
            "severity": finding.severity,
            "risk_score": finding.risk_score,
            "explanation": finding.explanation,
            "evidence_json": finding.evidence_json,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def report_row(report: Report) -> dict[str, Any]:
        return {
            "id": report.id,
            "investigation_id": report.investigation_id,
            "title": report.title,
            "body_markdown": report.body_markdown[:100_000],
            "generated_by": report.generated_by,
            "gcs_uri": report.gcs_uri,
            "created_at": report.created_at.isoformat() if report.created_at else None,
        }


# ---------------------------------------------------------------------------
# Cloud Storage
# ---------------------------------------------------------------------------


class StorageService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: Optional[Any] = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.gcs_bucket_name.strip()) and storage is not None

    @property
    def client(self):
        if not self.enabled:
            raise RuntimeError("GCS is not configured. Set GCS_BUCKET_NAME.")
        if self._client is None:
            self._client = storage.Client(project=self.settings.gcp_project_id or None)
        return self._client

    def upload_report(
        self,
        *,
        investigation_id: str,
        report_id: str,
        content: str,
    ) -> str:
        bucket = self.client.bucket(self.settings.gcs_bucket_name)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        blob_name = f"reports/{investigation_id}/{report_id}_{ts}.md"
        blob = bucket.blob(blob_name)
        blob.upload_from_string(content, content_type="text/markdown")
        return f"gs://{self.settings.gcs_bucket_name}/{blob_name}"


# ---------------------------------------------------------------------------
# BigQuery sync
# ---------------------------------------------------------------------------


def sync_transactions_to_bigquery(
    db: Session,
    settings: Settings | None = None,
    *,
    batch_size: int = 500,
) -> dict[str, int | str]:
    settings = settings or get_settings()
    if not settings.gcp_project_id:
        raise ValueError("GCP_PROJECT_ID is required for BigQuery sync")

    settings.use_bigquery = True
    bq = BigQueryService(settings)
    bq.ensure_tables()

    pending = list(
        db.scalars(select(Transaction).where(Transaction.synced_to_bq.is_(False))).all()
    )
    if not pending:
        return {"synced": 0, "message": "No pending transactions to sync."}

    rows = [BigQueryService.txn_row_from_orm(t) for t in pending]
    for i in range(0, len(rows), batch_size):
        bq.insert_rows("transactions", rows[i : i + batch_size])

    for t in pending:
        t.synced_to_bq = True
    db.commit()

    return {
        "synced": len(pending),
        "message": f"Synced {len(pending)} transactions to {bq.table_id('transactions')}.",
    }
