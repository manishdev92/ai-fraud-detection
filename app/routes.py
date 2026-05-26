"""FastAPI REST routes."""

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.agents import FraudInvestigationOrchestrator
from app.config import get_settings
from app.database import Finding, Report, get_db
from app.schemas import (
    FindingOut,
    FindingsResponse,
    GenerateTransactionsRequest,
    GenerateTransactionsResponse,
    InvestigationRequest,
    InvestigationResponse,
    ReportOut,
    SyncBigQueryResponse,
)
from app.services import generate_transactions, sync_transactions_to_bigquery

router = APIRouter()


@router.get("/health")
def health():
    settings = get_settings()
    return {
        "status": "ok",
        "app_env": settings.app_env,
        "use_adk": settings.use_adk,
        "gemini_enabled": settings.gemini_enabled,
        "bigquery_enabled": settings.gcp_enabled,
        "gcs_enabled": bool(settings.gcs_bucket_name),
    }


@router.post("/generate-transactions", response_model=GenerateTransactionsResponse)
def post_generate_transactions(
    body: GenerateTransactionsRequest,
    db: Session = Depends(get_db),
):
    settings = get_settings()
    count, accounts = generate_transactions(
        db, count=body.count, fraud_ratio=body.fraud_ratio, seed=body.seed
    )
    sync_msg = ""
    if settings.auto_sync_bigquery and settings.gcp_project_id:
        try:
            sync_result = sync_transactions_to_bigquery(db, settings)
            sync_msg = f" {sync_result['message']}"
        except Exception as exc:
            sync_msg = f" BigQuery sync skipped: {exc}"
    return GenerateTransactionsResponse(
        generated=count,
        accounts=accounts,
        message=f"Stored {count} transactions across {accounts} accounts (local DB).{sync_msg}",
    )


@router.post("/sync-to-bigquery", response_model=SyncBigQueryResponse)
def post_sync_to_bigquery(db: Session = Depends(get_db)):
    settings = get_settings()
    try:
        result = sync_transactions_to_bigquery(db, settings)
        return SyncBigQueryResponse(synced=int(result["synced"]), message=str(result["message"]))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/run-fraud-investigation", response_model=InvestigationResponse)
def post_run_fraud_investigation(
    body: InvestigationRequest,
    db: Session = Depends(get_db),
):
    settings = get_settings()
    sync_bq = body.sync_bigquery or (
        settings.auto_sync_bigquery and bool(settings.gcp_project_id)
    )
    result = FraudInvestigationOrchestrator(settings).run_pipeline(
        db,
        account_id=body.account_id,
        lookback_hours=body.lookback_hours,
        generate_report=body.generate_report,
        sync_bigquery=sync_bq,
    )
    return InvestigationResponse(
        investigation_id=result["investigation_id"],
        findings_count=result["findings_count"],
        summary=result["summary"],
        data_source=result.get("data_source"),
        orchestration=result.get("orchestration"),
        report_id=result.get("report_id"),
        report_preview=result.get("report_preview"),
    )


@router.get("/findings", response_model=FindingsResponse)
def get_findings(
    investigation_id: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    stmt = select(Finding).order_by(desc(Finding.created_at)).limit(limit)
    if investigation_id:
        stmt = stmt.where(Finding.investigation_id == investigation_id)
    rows = list(db.scalars(stmt).all())
    findings = [
        FindingOut(
            id=f.id,
            investigation_id=f.investigation_id,
            transaction_id=f.transaction_id,
            account_id=f.account_id,
            rule_id=f.rule_id,
            rule_name=f.rule_name,
            severity=f.severity,
            risk_score=f.risk_score,
            explanation=f.explanation,
            evidence=json.loads(f.evidence_json),
            created_at=f.created_at,
        )
        for f in rows
    ]
    inv = investigation_id or (findings[0].investigation_id if findings else None)
    return FindingsResponse(investigation_id=inv, total=len(findings), findings=findings)


@router.get("/reports/latest", response_model=ReportOut)
def get_latest_report(
    investigation_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    stmt = select(Report).order_by(desc(Report.created_at)).limit(1)
    if investigation_id:
        stmt = stmt.where(Report.investigation_id == investigation_id)
    report = db.scalar(stmt)
    if not report:
        raise HTTPException(status_code=404, detail="No reports found. Run an investigation first.")
    return ReportOut(
        id=report.id,
        investigation_id=report.investigation_id,
        title=report.title,
        body_markdown=report.body_markdown,
        generated_by=report.generated_by,
        gcs_uri=report.gcs_uri,
        created_at=report.created_at,
    )
