"""Pydantic API schemas."""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class GenerateTransactionsRequest(BaseModel):
    count: int = Field(default=500, ge=10, le=50_000)
    fraud_ratio: float = Field(default=0.08, ge=0.0, le=0.5)
    seed: Optional[int] = None


class GenerateTransactionsResponse(BaseModel):
    generated: int
    accounts: int
    message: str


class InvestigationRequest(BaseModel):
    account_id: Optional[str] = None
    lookback_hours: int = Field(default=168, ge=1, le=720)
    generate_report: bool = True
    sync_bigquery: bool = False


class SyncBigQueryResponse(BaseModel):
    synced: int
    message: str


class FindingOut(BaseModel):
    id: str
    investigation_id: str
    transaction_id: str
    account_id: str
    rule_id: str
    rule_name: str
    severity: str
    risk_score: float
    explanation: str
    evidence: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class FindingsResponse(BaseModel):
    investigation_id: Optional[str] = None
    total: int
    findings: list[FindingOut]


class InvestigationResponse(BaseModel):
    investigation_id: str
    findings_count: int
    summary: dict[str, Any]
    data_source: Optional[str] = None
    orchestration: Optional[str] = None
    report_id: Optional[str] = None
    report_preview: Optional[str] = None


class ReportOut(BaseModel):
    id: str
    investigation_id: str
    title: str
    body_markdown: str
    generated_by: str
    gcs_uri: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}
