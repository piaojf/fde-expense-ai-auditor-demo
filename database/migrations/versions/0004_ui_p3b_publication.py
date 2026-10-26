"""Append-only policy governance; keep old authority and upload rows untouched."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_ui_p3b"
down_revision = "0003_ui_p3a"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "policy_rule_reviews",
        sa.Column("review_id", sa.String(36), primary_key=True),
        sa.Column(
            "upload_id", sa.String(36), sa.ForeignKey("policy_uploads.upload_id"), nullable=False
        ),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("extraction", JSONB, nullable=False),
        sa.Column("confirmed_rule", JSONB, nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("upload_id", "revision"),
        sa.CheckConstraint("revision > 0", name="positive_revision"),
    )
    op.create_table(
        "policy_publication_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column(
            "policy_version_id",
            sa.String(128),
            sa.ForeignKey("policy_versions.policy_version_id"),
            unique=True,
        ),
        sa.Column(
            "predecessor_id", sa.String(128), sa.ForeignKey("policy_versions.policy_version_id")
        ),
        sa.Column("restore_id", sa.String(128), sa.ForeignKey("policy_versions.policy_version_id")),
        sa.Column("upload_id", sa.String(36), sa.ForeignKey("policy_uploads.upload_id")),
        sa.Column("review_id", sa.String(36), sa.ForeignKey("policy_rule_reviews.review_id")),
        sa.Column("effective_on", sa.Date, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("summary", JSONB, nullable=False),
        sa.Column("result", JSONB, nullable=False),
        sa.CheckConstraint("kind IN ('PUBLISH','RETIRE','ROLLBACK')", name="publication_kind"),
    )
    op.create_index(
        "ix_policy_publication_events_predecessor",
        "policy_publication_events",
        ["predecessor_id", "effective_on"],
    )
    # Journal rows may only be appended; protected sources cannot be erased by v1.3.
    op.execute("""
        CREATE FUNCTION policy_governance_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Policy governance history is immutable'; END; $$
    """)
    for table in ("policy_rule_reviews", "policy_publication_events"):
        op.execute(
            f"CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION policy_governance_immutable()"
        )
    op.execute("""
        CREATE FUNCTION policy_source_protected() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF EXISTS (SELECT 1 FROM policy_publication_events WHERE upload_id=OLD.upload_id)
          THEN RAISE EXCEPTION 'Published original is immutable'; END IF;
          RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
        END; $$
    """)
    op.execute(
        "CREATE TRIGGER published_source BEFORE UPDATE OR DELETE ON policy_uploads "
        "FOR EACH ROW EXECUTE FUNCTION policy_source_protected()"
    )
    op.execute("""
        CREATE FUNCTION policy_version_protected() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF EXISTS (SELECT 1 FROM policy_publication_events WHERE
            policy_version_id=OLD.policy_version_id OR predecessor_id=OLD.policy_version_id
            OR restore_id=OLD.policy_version_id)
          THEN RAISE EXCEPTION 'Governed policy version is immutable'; END IF;
          RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
        END; $$
    """)
    for table in ("policy_versions", "policy_chunks"):
        op.execute(
            f"CREATE TRIGGER published_version BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION policy_version_protected()"
        )


def downgrade():
    for table in ("policy_versions", "policy_chunks"):
        op.execute(f"DROP TRIGGER published_version ON {table}")
    op.execute("DROP FUNCTION policy_version_protected()")
    op.execute("DROP TRIGGER published_source ON policy_uploads")
    op.execute("DROP FUNCTION policy_source_protected()")
    op.drop_table("policy_publication_events")
    op.drop_table("policy_rule_reviews")
    op.execute("DROP FUNCTION policy_governance_immutable()")
