"""Local controlled draft uploads; existing authority schema/rows unchanged."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_ui_p3a"
down_revision = "0002_m4a"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "policy_uploads",
        sa.Column("upload_id", sa.String(36), primary_key=True),
        sa.Column("policy_version_id", sa.String(128), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="DRAFT"),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("original", sa.LargeBinary()),
        sa.Column("parsed", JSONB, nullable=False),
        sa.Column("confirmed_metadata", JSONB),
        sa.Column("error_code", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("status = 'DRAFT'", name="draft_only"),
        sa.CheckConstraint(
            "state IN ('PARSED','CONFIRMED','INDEXED','FAILED','DELETED')",
            name="upload_state",
        ),
        sa.CheckConstraint("size > 0 AND size <= 10485760", name="upload_size"),
        sa.CheckConstraint("length(sha256) = 64", name="upload_sha"),
    )
    op.create_index(
        "ix_policy_uploads_live_sha",
        "policy_uploads",
        ["sha256"],
        unique=True,
        postgresql_where=sa.text("state <> 'DELETED'"),
    )


def downgrade():
    # Explicit migration rollback is restricted to disposable DBs by test harness.
    op.drop_index("ix_policy_uploads_live_sha", table_name="policy_uploads")
    op.drop_table("policy_uploads")
