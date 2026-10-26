"""Policy authority columns; legacy unclassified policies default to DRAFT."""

import sqlalchemy as sa
from alembic import op

revision = "0002_m4a"
down_revision = "0001_m0"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("policy_versions", sa.Column(
        "title", sa.Text(), nullable=False, server_default="",
    ))
    op.add_column("policy_versions", sa.Column(
        "status", sa.String(16), nullable=False, server_default="DRAFT",
    ))
    op.add_column("policy_versions", sa.Column(
        "authority_level", sa.Integer(), nullable=False, server_default="0",
    ))
    op.create_check_constraint(
        "valid_policy_status", "policy_versions", "status IN ('ACTIVE','RETIRED','DRAFT')",
    )
    op.create_check_constraint("nonnegative_authority", "policy_versions", "authority_level >= 0")
    op.create_index(
        "ix_policy_versions_status_authority", "policy_versions", ["status", "authority_level"],
    )
    op.add_column("policy_chunks", sa.Column("chunk_hash", sa.String(64)))
    op.add_column("policy_chunks", sa.Column(
        "section", sa.String(128), nullable=False, server_default="",
    ))
    op.create_unique_constraint(
        "uq_policy_chunks_policy_version_id", "policy_chunks", ["policy_version_id", "chunk_hash"],
    )


def downgrade():
    op.drop_constraint("uq_policy_chunks_policy_version_id", "policy_chunks", type_="unique")
    op.drop_column("policy_chunks", "section")
    op.drop_column("policy_chunks", "chunk_hash")
    op.drop_index("ix_policy_versions_status_authority", table_name="policy_versions")
    op.drop_constraint(op.f("ck_policy_versions_nonnegative_authority"), "policy_versions")
    op.drop_constraint(op.f("ck_policy_versions_valid_policy_status"), "policy_versions")
    for column in ("authority_level", "status", "title"):
        op.drop_column("policy_versions", column)
