"""
Multi-agent fraud investigation (Bot A, Bot B) + Google ADK orchestration.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Optional
from uuid import uuid4

from google.adk.agents import BaseAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types
from sqlalchemy import select
from sqlalchemy.orm import Session
from typing_extensions import override

from app.config import Settings
from app.database import Finding, Report
from app.services import (
    BigQueryService,
    FraudRuleEngine,
    RuleHit,
    StorageService,
    log_audit,
    sync_transactions_to_bigquery,
)

logger = logging.getLogger(__name__)

COMPLIANCE_PROMPT = """You are a senior banking compliance officer drafting a formal SAR-style
investigation report for regulators (FinCEN-style structure, no legal advice).

Use ONLY the findings JSON provided. Do not invent transactions or accounts.

Structure the report in Markdown with these sections:
1. Executive Summary
2. Scope and Methodology
3. Suspicious Activity Overview (table of findings by rule)
4. Detailed Findings (per account, cite transaction IDs and explanations)
5. Risk Assessment
6. Recommended Actions
7. Model Limitations and Human Review Requirements

Tone: formal, precise, audit-ready. Redact no IDs — this is an internal draft.
"""

_pipeline_result: ContextVar[Optional["PipelineResult"]] = ContextVar("pipeline_result", default=None)
_runtime: ContextVar[Optional["RuntimeContext"]] = ContextVar("runtime", default=None)


@dataclass
class PipelineResult:
    investigation_id: str = ""
    findings_count: int = 0
    summary: dict[str, Any] = field(default_factory=dict)
    data_source: str = "sqlite"
    report_id: Optional[str] = None
    report_preview: Optional[str] = None
    report_generated_by: Optional[str] = None
    orchestration: str = "adk"


@dataclass
class RuntimeContext:
    db: Session
    settings: Settings
    account_id: Optional[str] = None
    lookback_hours: int = 168
    generate_report: bool = True
    sync_bigquery: bool = False


def set_runtime(ctx: RuntimeContext) -> None:
    _runtime.set(ctx)


def get_runtime() -> RuntimeContext:
    ctx = _runtime.get()
    if ctx is None:
        raise RuntimeError("ADK runtime context not initialized")
    return ctx


def get_pipeline_result() -> PipelineResult:
    result = _pipeline_result.get()
    if result is None:
        result = PipelineResult()
        _pipeline_result.set(result)
    return result


def reset_pipeline() -> None:
    _pipeline_result.set(PipelineResult())
    _runtime.set(None)


@dataclass
class InvestigationResult:
    investigation_id: str
    hits: list[RuleHit]
    summary: dict[str, Any]
    data_source: str


class FraudInvestigatorAgent:
    """Bot A — queries local SQLite/Postgres or BigQuery; runs fraud rules."""

    name = "fraud_investigator"
    description = "Investigates transactional data and flags suspicious activity."

    def __init__(self, settings: Settings):
        self.settings = settings
        self.engine = FraudRuleEngine(settings)
        self.bq = BigQueryService(settings)

    def investigate(
        self,
        db: Session,
        *,
        account_id: Optional[str] = None,
        lookback_hours: int = 168,
    ) -> InvestigationResult:
        investigation_id = str(uuid4())
        data_source = "sqlite"

        if self.bq.enabled:
            data_source = "bigquery"
            hits = self._investigate_from_bigquery(
                account_id=account_id,
                lookback_hours=lookback_hours,
            )
        else:
            hits = self.engine.run(
                db,
                account_id=account_id,
                lookback_hours=lookback_hours,
            )

        for hit in hits:
            finding = Finding(
                investigation_id=investigation_id,
                transaction_id=hit.transaction_id,
                account_id=hit.account_id,
                rule_id=hit.rule_id,
                rule_name=hit.rule_name,
                severity=hit.severity,
                risk_score=hit.risk_score,
                explanation=hit.explanation,
                evidence_json=json.dumps(hit.evidence),
            )
            db.add(finding)
            log_audit(
                db,
                event_type="finding.flagged",
                actor=self.name,
                resource_type="transaction",
                resource_id=hit.transaction_id,
                message=hit.explanation,
                metadata={
                    "investigation_id": investigation_id,
                    "rule_id": hit.rule_id,
                    "severity": hit.severity,
                    "risk_score": hit.risk_score,
                    "evidence": hit.evidence,
                },
            )

        summary = self._build_summary(hits)
        log_audit(
            db,
            event_type="investigation.completed",
            actor=self.name,
            resource_type="investigation",
            resource_id=investigation_id,
            message=f"Investigation completed with {len(hits)} findings",
            metadata={"summary": summary, "data_source": data_source},
        )
        db.commit()

        if self.bq.enabled and hits:
            self._sync_findings_to_bq(db, investigation_id)

        return InvestigationResult(
            investigation_id=investigation_id,
            hits=hits,
            summary=summary,
            data_source=data_source,
        )

    def _investigate_from_bigquery(
        self,
        *,
        account_id: Optional[str],
        lookback_hours: int,
    ) -> list[RuleHit]:
        """Run rules against BigQuery-fetched rows via in-memory Transaction-like objects."""
        rows = self.bq.query_transactions(
            account_id=account_id,
            lookback_hours=lookback_hours,
        )
        if not rows:
            return []

        class _Txn:
            pass

        txns = []
        for r in rows:
            t = _Txn()
            t.id = r["id"]
            t.account_id = r["account_id"]
            t.counterparty_account = r.get("counterparty_account")
            t.amount_usd = float(r["amount_usd"])
            t.txn_type = r["txn_type"]
            t.channel = r["channel"]
            t.country_code = r.get("country_code", "US")
            t.is_cross_border = bool(r.get("is_cross_border"))
            t.created_at = r["created_at"]
            if hasattr(t.created_at, "tzinfo") and t.created_at.tzinfo is None:
                t.created_at = t.created_at.replace(tzinfo=timezone.utc)
            txns.append(t)

        by_account: dict[str, list] = {}
        for t in txns:
            by_account.setdefault(t.account_id, []).append(t)

        hits: list[RuleHit] = []
        for acct, acct_txns in by_account.items():
            acct_txns.sort(key=lambda x: x.created_at)
            hits.extend(self.engine._rule_high_value_unusual(acct, acct_txns))
            hits.extend(self.engine._rule_rapid_repeated(acct, acct_txns))
            hits.extend(self.engine._rule_mule_pattern(acct, acct_txns))
            hits.extend(self.engine._rule_cross_border(acct, acct_txns))
            hits.extend(self.engine._rule_velocity_spike(acct, acct_txns))
        return hits

    def _sync_findings_to_bq(self, db: Session, investigation_id: str) -> None:
        from sqlalchemy import select

        findings = list(
            db.scalars(
                select(Finding).where(Finding.investigation_id == investigation_id)
            ).all()
        )
        rows = [BigQueryService.finding_row(f) for f in findings]
        self.bq.insert_rows("findings", rows)

    @staticmethod
    def _build_summary(hits: list[RuleHit]) -> dict[str, Any]:
        by_rule: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        accounts: set[str] = set()
        for h in hits:
            by_rule[h.rule_name] = by_rule.get(h.rule_name, 0) + 1
            by_severity[h.severity] = by_severity.get(h.severity, 0) + 1
            accounts.add(h.account_id)
        return {
            "total_findings": len(hits),
            "unique_accounts": len(accounts),
            "by_rule": by_rule,
            "by_severity": by_severity,
            "max_risk_score": max((h.risk_score for h in hits), default=0),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

# ============================================================
# compliance
# ============================================================

class ComplianceReportAgent:
    """
    Bot B — consumes Agent A findings and generates a formal compliance report.
    Uses Gemini API when configured; falls back to deterministic template.
    """

    name = "compliance_report"
    description = "Generates formal regulator-style compliance reports from investigation findings."

    def __init__(self, settings: Settings):
        self.settings = settings
        self.storage = StorageService(settings)
        self.bq = BigQueryService(settings)

    def generate(
        self,
        db: Session,
        *,
        investigation_id: str,
        findings: list[Finding],
        summary: dict[str, Any],
    ) -> Report:
        findings_payload = [
            {
                "transaction_id": f.transaction_id,
                "account_id": f.account_id,
                "rule_id": f.rule_id,
                "rule_name": f.rule_name,
                "severity": f.severity,
                "risk_score": f.risk_score,
                "explanation": f.explanation,
                "evidence": json.loads(f.evidence_json),
            }
            for f in findings
        ]

        context = {
            "investigation_id": investigation_id,
            "summary": summary,
            "findings": findings_payload,
        }

        if self.settings.gemini_enabled:
            body, generated_by = self._generate_with_gemini(context)
        else:
            body, generated_by = self._generate_template(context)

        title = f"Suspicious Activity Investigation Report — {investigation_id[:8].upper()}"
        report = Report(
            id=str(uuid4()),
            investigation_id=investigation_id,
            title=title,
            body_markdown=body,
            generated_by=generated_by,
        )

        if self.storage.enabled:
            report.gcs_uri = self.storage.upload_report(
                investigation_id=investigation_id,
                report_id=report.id,
                content=body,
            )

        db.add(report)
        log_audit(
            db,
            event_type="report.generated",
            actor=self.name,
            resource_type="report",
            resource_id=report.id,
            message=f"Compliance report generated via {generated_by}",
            metadata={
                "investigation_id": investigation_id,
                "gcs_uri": report.gcs_uri,
                "finding_count": len(findings),
            },
        )
        db.commit()

        if self.bq.enabled:
            self.bq.insert_rows("reports", [BigQueryService.report_row(report)])

        return report

    def _generate_with_gemini(self, context: dict[str, Any]) -> tuple[str, str]:
        import google.generativeai as genai

        try:
            genai.configure(api_key=self.settings.gemini_api_key)
            model = genai.GenerativeModel(self.settings.gemini_model)
            prompt = (
                f"{COMPLIANCE_PROMPT}\n\n"
                f"Investigation data:\n```json\n{json.dumps(context, indent=2, default=str)}\n```"
            )
            response = model.generate_content(
                prompt,
                generation_config={
                    "temperature": 0.2,
                    "max_output_tokens": 8192,
                },
            )
            text = response.text or ""
            if not text.strip():
                text, _ = self._generate_template(context)
                return text, "template_fallback"
            return text, "gemini"
        except Exception as exc:
            logger.warning("Gemini report generation failed, using template: %s", exc)
            text, _ = self._generate_template(context)
            return text, "template_fallback"

    def _generate_template(self, context: dict[str, Any]) -> tuple[str, str]:
        inv_id = context["investigation_id"]
        summary = context["summary"]
        findings = context["findings"]
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        lines = [
            f"# Suspicious Activity Investigation Report",
            f"**Investigation ID:** `{inv_id}`  ",
            f"**Generated:** {ts}  ",
            f"**Generator:** Template (set GEMINI_API_KEY for Gemini Enterprise output)",
            "",
            "## 1. Executive Summary",
            f"This automated investigation identified **{summary.get('total_findings', 0)}** "
            f"suspicious indicators across **{summary.get('unique_accounts', 0)}** accounts. "
            f"Maximum risk score: **{summary.get('max_risk_score', 0):.1f}**.",
            "",
            "## 2. Scope and Methodology",
            "Analysis performed by Agent A (Fraud Investigator) using five rule-based detectors: "
            "high-value unusual, rapid repeated transfers, mule-account pattern, "
            "cross-border anomaly, and account velocity spike. All flags include explainability metadata.",
            "",
            "## 3. Suspicious Activity Overview",
            "| Rule | Count |",
            "|------|-------|",
        ]
        for rule, count in (summary.get("by_rule") or {}).items():
            lines.append(f"| {rule} | {count} |")

        lines.extend(["", "## 4. Detailed Findings", ""])
        for i, f in enumerate(findings[:50], 1):
            lines.extend([
                f"### 4.{i} Account `{f['account_id']}` — {f['rule_name']}",
                f"- **Transaction:** `{f['transaction_id']}`",
                f"- **Severity:** {f['severity']} | **Risk score:** {f['risk_score']:.1f}",
                f"- **Explanation:** {f['explanation']}",
                f"- **Evidence:** `{json.dumps(f['evidence'])}`",
                "",
            ])

        lines.extend([
            "## 5. Risk Assessment",
            "Aggregated severity distribution: "
            + json.dumps(summary.get("by_severity", {})),
            "",
            "## 6. Recommended Actions",
            "1. Escalate critical/high findings to AML operations within 24 hours.",
            "2. Place holds on mule-pattern accounts pending human review.",
            "3. File SAR if human analyst confirms suspicious activity.",
            "",
            "## 7. Model Limitations and Human Review",
            "This report was produced with automated rules and optional LLM narrative. "
            "A qualified compliance officer must validate all findings before regulatory filing.",
            "",
            "---",
            "*Responsible AI: No PII beyond synthetic mock data. Human-in-the-loop required.*",
        ])
        return "\n".join(lines), "template"

class FraudInvestigatorBotA(BaseAgent):
    """ADK Bot A — deterministic fraud investigator."""

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        runtime = get_runtime()
        settings = runtime.settings
        result_box = get_pipeline_result()

        if runtime.sync_bigquery and settings.gcp_project_id:
            # imported above

            sync_result = sync_transactions_to_bigquery(runtime.db, settings)
            logger.info("BigQuery pre-sync: %s", sync_result.get("message"))

        if settings.gcp_project_id and (runtime.sync_bigquery or settings.use_bigquery):
            settings.use_bigquery = True

        investigator = FraudInvestigatorAgent(settings)
        investigation = investigator.investigate(
            runtime.db,
            account_id=runtime.account_id,
            lookback_hours=runtime.lookback_hours,
        )

        result_box.investigation_id = investigation.investigation_id
        result_box.findings_count = len(investigation.hits)
        result_box.summary = investigation.summary
        result_box.data_source = investigation.data_source

        payload = {
            "agent": "Bot A — Fraud Investigator",
            "investigation_id": investigation.investigation_id,
            "data_source": investigation.data_source,
            "findings_count": len(investigation.hits),
            "summary": investigation.summary,
            "findings": [
                {
                    "transaction_id": h.transaction_id,
                    "account_id": h.account_id,
                    "rule_id": h.rule_id,
                    "rule_name": h.rule_name,
                    "severity": h.severity,
                    "risk_score": h.risk_score,
                    "explanation": h.explanation,
                    "evidence": h.evidence,
                }
                for h in investigation.hits[:100]
            ],
        }
        text = json.dumps(payload, indent=2, default=str)

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=text)],
            ),
            invocation_id=ctx.invocation_id,
            branch=ctx.branch,
        )

class ComplianceReportBotBCustom(BaseAgent):
    """ADK Bot B — compliance report via Gemini or template."""

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        runtime = get_runtime()
        result_box = get_pipeline_result()

        if not runtime.generate_report:
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="Report generation skipped.")],
                ),
                invocation_id=ctx.invocation_id,
                branch=ctx.branch,
            )
            return

        findings = list(
            runtime.db.scalars(
                select(Finding).where(
                    Finding.investigation_id == result_box.investigation_id
                )
            ).all()
        )
        compliance = ComplianceReportAgent(runtime.settings)
        report = compliance.generate(
            runtime.db,
            investigation_id=result_box.investigation_id,
            findings=findings,
            summary=result_box.summary,
        )

        result_box.report_id = report.id
        result_box.report_generated_by = report.generated_by
        preview = report.body_markdown
        if len(preview) > 1500:
            preview = preview[:1500] + "..."
        result_box.report_preview = preview

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=report.body_markdown)],
            ),
            invocation_id=ctx.invocation_id,
            branch=ctx.branch,
        )

def build_root_agent(settings: Settings | None = None) -> SequentialAgent:
    """ADK SequentialAgent: Bot A → Bot B."""
    settings = settings or Settings()

    bot_a = FraudInvestigatorBotA(
        name="fraud_investigator",
        description=(
            "Bot A — investigates transactional data from on-premise store or "
            "BigQuery and flags suspicious patterns with explainability."
        ),
    )
    bot_b = ComplianceReportBotBCustom(
        name="compliance_report",
        description=(
            "Bot B — generates formal regulator-style compliance reports using "
            "Gemini when configured, with template fallback."
        ),
    )
    bot_c = ComplianceReportBotBCustom(
        name="google_search",
        description=(
            "Bot B — generates formal regulator-style compliance reports using "
            "Gemini when configured, with template fallback."
        ),
    )

    return SequentialAgent(
        name="fraud_investigation_coordinator",
        description="Coordinates fraud investigation and compliance reporting.",
        sub_agents=[bot_a, bot_b, bot_c],
    )


root_agent = build_root_agent()


def _ensure_gemini_env(settings: Settings) -> None:
    if settings.gemini_api_key and not os.environ.get("GOOGLE_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = settings.gemini_api_key
    if settings.gemini_api_key and not os.environ.get("GEMINI_API_KEY"):
        os.environ["GEMINI_API_KEY"] = settings.gemini_api_key


async def run_adk_pipeline_async(
    db: Session,
    settings: Settings,
    *,
    account_id: Optional[str] = None,
    lookback_hours: int = 168,
    generate_report: bool = True,
    sync_bigquery: bool = False,
) -> dict[str, Any]:
    reset_pipeline()
    set_runtime(
        RuntimeContext(
            db=db,
            settings=settings,
            account_id=account_id,
            lookback_hours=lookback_hours,
            generate_report=generate_report,
            sync_bigquery=sync_bigquery,
        )
    )
    _ensure_gemini_env(settings)
    root = build_root_agent(settings)
    session_service = InMemorySessionService()
    runner = Runner(
        app_name="fraud_investigation_platform",
        agent=root,
        session_service=session_service,
    )
    user_id = "api_user"
    session = await session_service.create_session(
        app_name="fraud_investigation_platform", user_id=user_id
    )
    prompt = (
        f"Run fraud investigation. lookback_hours={lookback_hours}"
        f"{f', account_id={account_id}' if account_id else ''}"
        f", generate_report={generate_report}"
    )
    message = types.Content(role="user", parts=[types.Part(text=prompt)])
    async for _event in runner.run_async(
        user_id=user_id, session_id=session.id, new_message=message
    ):
        pass
    result = get_pipeline_result()
    return {
        "investigation_id": result.investigation_id,
        "findings_count": result.findings_count,
        "summary": result.summary,
        "data_source": result.data_source,
        "orchestration": "adk",
        "report_id": result.report_id,
        "report_preview": result.report_preview,
    }


def run_adk_pipeline(db: Session, settings: Settings, **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(run_adk_pipeline_async(db, settings, **kwargs))


class FraudInvestigationOrchestrator:
    """Coordinates Bot A → Bot B (ADK default, legacy fallback)."""

    def __init__(self, settings: Optional[Settings] = None):
        settings = settings or Settings()
        self.investigator = FraudInvestigatorAgent(settings)
        self.compliance = ComplianceReportAgent(settings)
        self.settings = settings

    def run_pipeline(
        self,
        db: Session,
        *,
        account_id: Optional[str] = None,
        lookback_hours: int = 168,
        generate_report: bool = True,
        sync_bigquery: bool = False,
    ) -> dict[str, Any]:
        if self.settings.use_adk:
            try:
                return run_adk_pipeline(
                    db,
                    self.settings,
                    account_id=account_id,
                    lookback_hours=lookback_hours,
                    generate_report=generate_report,
                    sync_bigquery=sync_bigquery or self.settings.auto_sync_bigquery,
                )
            except Exception as exc:
                logger.warning("ADK pipeline failed, using legacy orchestrator: %s", exc)

        return self._run_legacy_pipeline(
            db,
            account_id=account_id,
            lookback_hours=lookback_hours,
            generate_report=generate_report,
        )

    def _run_legacy_pipeline(
        self,
        db: Session,
        *,
        account_id: Optional[str] = None,
        lookback_hours: int = 168,
        generate_report: bool = True,
    ) -> dict[str, Any]:
        investigation = self.investigator.investigate(
            db,
            account_id=account_id,
            lookback_hours=lookback_hours,
        )

        report: Optional[Report] = None
        if generate_report:
            findings = list(
                db.scalars(
                    select(Finding).where(
                        Finding.investigation_id == investigation.investigation_id
                    )
                ).all()
            )
            if findings or investigation.summary.get("total_findings", 0) == 0:
                report = self.compliance.generate(
                    db,
                    investigation_id=investigation.investigation_id,
                    findings=findings,
                    summary=investigation.summary,
                )

        return {
            "investigation_id": investigation.investigation_id,
            "findings_count": len(investigation.hits),
            "summary": investigation.summary,
            "data_source": investigation.data_source,
            "orchestration": "legacy",
            "report_id": report.id if report else None,
            "report_preview": (
                (report.body_markdown[:1500] + "...")
                if report and len(report.body_markdown) > 1500
                else (report.body_markdown if report else None)
            ),
        }