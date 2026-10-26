from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from database.models.base import Base


class Claim(Base):
    __tablename__ = "claims"
    __table_args__ = (
        CheckConstraint("amount > 0", name="positive_amount"),
        CheckConstraint("nights >= 1", name="positive_nights"),
        CheckConstraint("currency = 'CNY'", name="currency_cny"),
        CheckConstraint(
            "status IN ('NEW','AUDITING','PENDING_HUMAN','PENDING_ERP','COMPLETED','FAILED')",
            name="valid_status",
        ),
        Index("ix_claims_legal_entity_status", "legal_entity", "status"),
    )
    claim_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    legal_entity: Mapped[str] = mapped_column(String(128))
    employee_id: Mapped[str] = mapped_column(String(128))
    employee_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    expense_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), server_default="CNY")
    incurred_on: Mapped[date] = mapped_column(Date)
    nights: Mapped[int] = mapped_column(Integer, server_default="1")
    status: Mapped[str] = mapped_column(String(32), server_default="NEW")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClaimDocument(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("length(sha256) = 64", name="sha256_length"),
        CheckConstraint(
            "parse_status IN ('UNPARSED','PARSED','FAILED')", name="valid_parse_status"
        ),
        Index("ix_documents_claim_id", "claim_id"),
    )
    document_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    claim_id: Mapped[str] = mapped_column(ForeignKey("claims.claim_id"), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128))
    sha256: Mapped[str] = mapped_column(String(64))
    parse_status: Mapped[str] = mapped_column(String(16), server_default="UNPARSED")
    parsed_fields: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditRun(Base):
    __tablename__ = "audit_runs"
    __table_args__ = (
        UniqueConstraint("claim_id", "idempotency_key"),
        UniqueConstraint("run_id", "claim_id"),
        CheckConstraint("revision >= 0", name="nonnegative_revision"),
        CheckConstraint("mode IN ('LIVE','SHADOW')", name="valid_mode"),
        CheckConstraint(
            "status IN ('NEW','AUDITING','PENDING_HUMAN','PENDING_ERP','COMPLETED','FAILED')",
            name="valid_status",
        ),
    )
    run_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    claim_id: Mapped[str] = mapped_column(ForeignKey("claims.claim_id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(8), server_default="LIVE")
    revision: Mapped[int] = mapped_column(Integer, server_default="0")
    status: Mapped[str] = mapped_column(String(32), server_default="NEW")
    checkpoint_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    decision_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PolicyVersion(Base):
    __tablename__ = "policy_versions"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE','RETIRED','DRAFT')", name="valid_policy_status"),
        CheckConstraint("authority_level >= 0", name="nonnegative_authority"),
        Index("ix_policy_versions_status_authority", "status", "authority_level"),
        UniqueConstraint("legal_entity", "policy_key", "version"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from", name="valid_interval"
        ),
        Index(
            "ix_policy_versions_scope",
            "legal_entity",
            "category",
            "city",
            "effective_from",
            "effective_to",
        ),
    )
    policy_version_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    policy_key: Mapped[str] = mapped_column(String(128))
    version: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(Text, server_default="")
    status: Mapped[str] = mapped_column(String(16), server_default="DRAFT")
    authority_level: Mapped[int] = mapped_column(Integer, server_default="0")
    legal_entity: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(32))
    city: Mapped[str] = mapped_column(String(128))
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    authoritative_rules: Mapped[dict[str, Any]] = mapped_column(JSONB)
    source_uri: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PolicyChunk(Base):
    __tablename__ = "policy_chunks"
    __table_args__ = (
        UniqueConstraint("policy_version_id", "chunk_hash"),
        Index("ix_policy_chunks_policy_version_id", "policy_version_id"),
        Index("ix_policy_chunks_search_vector", "search_vector", postgresql_using="gin"),
    )
    chunk_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    policy_version_id: Mapped[str] = mapped_column(
        ForeignKey("policy_versions.policy_version_id"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text)
    chunk_hash: Mapped[str | None] = mapped_column(String(64))
    section: Mapped[str] = mapped_column(String(128), server_default="")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024))
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', content)", persisted=True)
    )
    source_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )


class AuditEventRecord(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        ForeignKeyConstraint(["run_id", "claim_id"], ["audit_runs.run_id", "audit_runs.claim_id"]),
        UniqueConstraint("run_id", "sequence"),
        CheckConstraint("sequence >= 0", name="nonnegative_sequence"),
        Index("ix_audit_events_claim_id_occurred_at", "claim_id", "occurred_at"),
    )
    event_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    claim_id: Mapped[str] = mapped_column(String(128))
    run_id: Mapped[UUID] = mapped_column()
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(128))
    actor: Mapped[str] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class PolicyUpload(Base):
    """Bounded original + parse provenance; publication is intentionally absent."""

    __tablename__ = "policy_uploads"
    __table_args__ = (
        CheckConstraint("status = 'DRAFT'", name="draft_only"),
        CheckConstraint(
            "state IN ('PARSED','CONFIRMED','INDEXED','FAILED','DELETED')", name="upload_state",
        ),
        CheckConstraint("size > 0 AND size <= 10485760", name="upload_size"),
        CheckConstraint("length(sha256) = 64", name="upload_sha"),
        Index("ix_policy_uploads_live_sha", "sha256", unique=True,
              postgresql_where=text("state <> 'DELETED'")),
    )
    upload_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    policy_version_id: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(16), server_default="DRAFT")
    state: Mapped[str] = mapped_column(String(16))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    original: Mapped[bytes | None] = mapped_column(LargeBinary)
    parsed: Mapped[dict[str, Any]] = mapped_column(JSONB)
    confirmed_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PolicyRuleReview(Base):
    """Append-only human revisions; raw extraction and original file are retained."""

    __tablename__ = "policy_rule_reviews"
    __table_args__ = (
        UniqueConstraint("upload_id", "revision"),
        CheckConstraint("revision > 0", name="positive_revision"),
    )
    review_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    upload_id: Mapped[str] = mapped_column(ForeignKey("policy_uploads.upload_id"))
    revision: Mapped[int] = mapped_column(Integer)
    file_sha256: Mapped[str] = mapped_column(String(64))
    extraction: Mapped[dict[str, Any]] = mapped_column(JSONB)
    confirmed_rule: Mapped[dict[str, Any]] = mapped_column(JSONB)
    actor: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PolicyPublicationEvent(Base):
    """Atomic release journal and dated cutover; no historical row is overwritten."""

    __tablename__ = "policy_publication_events"
    __table_args__ = (
        CheckConstraint("kind IN ('PUBLISH','RETIRE','ROLLBACK')", name="publication_kind"),
        Index("ix_policy_publication_events_predecessor", "predecessor_id", "effective_on"),
    )
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_sha256: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    policy_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("policy_versions.policy_version_id"), unique=True
    )
    predecessor_id: Mapped[str | None] = mapped_column(
        ForeignKey("policy_versions.policy_version_id")
    )
    restore_id: Mapped[str | None] = mapped_column(ForeignKey("policy_versions.policy_version_id"))
    upload_id: Mapped[str | None] = mapped_column(ForeignKey("policy_uploads.upload_id"))
    review_id: Mapped[str | None] = mapped_column(ForeignKey("policy_rule_reviews.review_id"))
    effective_on: Mapped[date] = mapped_column(Date)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(String(128))
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
