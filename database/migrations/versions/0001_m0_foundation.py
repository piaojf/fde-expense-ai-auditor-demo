"""M0 immutable foundation. No imports from live application metadata."""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql as pg

revision = "0001_m0"
down_revision = None
branch_labels = None
depends_on = None


def timestamp(name):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "claims",
        sa.Column("claim_id", sa.String(128), primary_key=True),
        sa.Column("legal_entity", sa.String(128), nullable=False),
        sa.Column("employee_id", sa.String(128), nullable=False),
        sa.Column("employee_snapshot", pg.JSONB(), nullable=False),
        sa.Column("expense_snapshot", pg.JSONB(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="CNY"),
        sa.Column("incurred_on", sa.Date(), nullable=False),
        sa.Column("nights", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(32), nullable=False, server_default="NEW"),
        timestamp("created_at"),
        timestamp("updated_at"),
        sa.CheckConstraint("amount > 0", name="positive_amount"),
        sa.CheckConstraint("nights >= 1", name="positive_nights"),
        sa.CheckConstraint("currency = 'CNY'", name="currency_cny"),
        sa.CheckConstraint(
            "status IN ('NEW','AUDITING','PENDING_HUMAN','PENDING_ERP','COMPLETED','FAILED')",
            name="valid_status",
        ),
    )
    op.create_index("ix_claims_legal_entity_status", "claims", ["legal_entity", "status"])
    op.create_table(
        "documents",
        sa.Column("document_id", sa.String(128), primary_key=True),
        sa.Column("claim_id", sa.String(128), sa.ForeignKey("claims.claim_id"), nullable=False),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("parse_status", sa.String(16), nullable=False, server_default="UNPARSED"),
        sa.Column("parsed_fields", pg.JSONB()),
        timestamp("created_at"),
        sa.CheckConstraint("length(sha256) = 64", name="sha256_length"),
        sa.CheckConstraint(
            "parse_status IN ('UNPARSED','PARSED','FAILED')", name="valid_parse_status"
        ),
    )
    op.create_index("ix_documents_claim_id", "documents", ["claim_id"])
    op.create_table(
        "audit_runs",
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        sa.Column("claim_id", sa.String(128), sa.ForeignKey("claims.claim_id"), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False, server_default="LIVE"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="NEW"),
        sa.Column(
            "checkpoint_snapshot", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("decision_result", pg.JSONB()),
        timestamp("created_at"),
        timestamp("updated_at"),
        sa.UniqueConstraint("claim_id", "idempotency_key"),
        sa.UniqueConstraint("run_id", "claim_id"),
        sa.CheckConstraint("revision >= 0", name="nonnegative_revision"),
        sa.CheckConstraint("mode IN ('LIVE','SHADOW')", name="valid_mode"),
        sa.CheckConstraint(
            "status IN ('NEW','AUDITING','PENDING_HUMAN','PENDING_ERP','COMPLETED','FAILED')",
            name="valid_status",
        ),
    )
    op.create_table(
        "policy_versions",
        sa.Column("policy_version_id", sa.String(128), primary_key=True),
        sa.Column("policy_key", sa.String(128), nullable=False),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column("legal_entity", sa.String(128), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("city", sa.String(128), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date()),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authoritative_rules", pg.JSONB(), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        timestamp("created_at"),
        sa.UniqueConstraint("legal_entity", "policy_key", "version"),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from", name="valid_interval"
        ),
    )
    op.create_index(
        "ix_policy_versions_scope",
        "policy_versions",
        ["legal_entity", "category", "city", "effective_from", "effective_to"],
    )
    op.create_table(
        "policy_chunks",
        sa.Column("chunk_id", sa.String(128), primary_key=True),
        sa.Column(
            "policy_version_id",
            sa.String(128),
            sa.ForeignKey("policy_versions.policy_version_id"),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1024)),
        sa.Column(
            "search_vector",
            pg.TSVECTOR(),
            sa.Computed("to_tsvector('simple', content)", persisted=True),
            nullable=False,
        ),
        sa.Column(
            "source_metadata", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
    )
    op.create_index("ix_policy_chunks_policy_version_id", "policy_chunks", ["policy_version_id"])
    op.create_index(
        "ix_policy_chunks_search_vector", "policy_chunks", ["search_vector"], postgresql_using="gin"
    )
    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.Uuid(), primary_key=True),
        sa.Column("claim_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        timestamp("occurred_at"),
        sa.Column("payload", pg.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id", "claim_id"], ["audit_runs.run_id", "audit_runs.claim_id"]
        ),
        sa.UniqueConstraint("run_id", "sequence"),
        sa.CheckConstraint("sequence >= 0", name="nonnegative_sequence"),
    )
    op.create_index(
        "ix_audit_events_claim_id_occurred_at", "audit_events", ["claim_id", "occurred_at"]
    )


def downgrade():
    for table in (
        "audit_events",
        "policy_chunks",
        "policy_versions",
        "audit_runs",
        "documents",
        "claims",
    ):
        op.drop_table(table)
    # Extension belongs to the database; preserve it for other applications.
