"""Database models and session management (on-premise SQLite / Postgres)."""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import DateTime, Float, Index, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    counterparty_account: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    amount_usd: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    txn_type: Mapped[str] = mapped_column(String(32))  # debit, credit, transfer
    channel: Mapped[str] = mapped_column(String(32))  # wire, ach, card, internal
    country_code: Mapped[str] = mapped_column(String(2), default="US")
    is_cross_border: Mapped[bool] = mapped_column(default=False)
    merchant_category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    synced_to_bq: Mapped[bool] = mapped_column(default=False)

    __table_args__ = (
        Index("ix_txn_account_created", "account_id", "created_at"),
    )


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(String(36), index=True)
    transaction_id: Mapped[str] = mapped_column(String(36), index=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    rule_id: Mapped[str] = mapped_column(String(64))
    rule_name: Mapped[str] = mapped_column(String(128))
    severity: Mapped[str] = mapped_column(String(16))  # low, medium, high, critical
    risk_score: Mapped[float] = mapped_column(Float)
    explanation: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(String(36), index=True)
    title: Mapped[str] = mapped_column(String(256))
    body_markdown: Mapped[str] = mapped_column(Text)
    generated_by: Mapped[str] = mapped_column(String(64))  # gemini | template
    gcs_uri: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


# --- Session management ---

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
# Base defined above

_engine = None
_SessionLocal = None


def _ensure_sqlite_dir(url: str) -> None:
    if url.startswith("sqlite:///./"):
        rel = url.replace("sqlite:///./", "")
        Path(rel).parent.mkdir(parents=True, exist_ok=True)


def reset_engine() -> None:
    """Reset engine (for tests)."""
    global _engine, _SessionLocal
    _engine = None
    _SessionLocal = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        _ensure_sqlite_dir(settings.database_url)
        connect_args = {}
        pool_kwargs: dict = {"pool_pre_ping": True}
        if settings.database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        if settings.database_url in ("sqlite:///:memory:", "sqlite://"):
            pool_kwargs = {"poolclass": StaticPool, "connect_args": connect_args}
        else:
            pool_kwargs["connect_args"] = connect_args
        _engine = create_engine(settings.database_url, **pool_kwargs)
        _SessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)
    return _engine


def init_db() -> None:
    engine = get_engine()
    Base.metadata.create_all(bind=engine)


def get_session_factory():
    get_engine()
    return _SessionLocal


def get_db() -> Generator[Session, None, None]:
    factory = get_session_factory()
    db = factory()
    try:
        yield db
    finally:
        db.close()
