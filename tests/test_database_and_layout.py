import io
import json
from pathlib import Path

import yaml
from alembic import command
from alembic.config import Config
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.schema import CreateTable

from database.models import Base, Claim, PolicyChunk


def test_model_postgres_types_and_constraints():
    assert set(Base.metadata.tables) == {
        "claims",
        "documents",
        "audit_runs",
        "audit_events",
        "policy_versions",
        "policy_chunks",
        "policy_uploads",
        "policy_rule_reviews",
        "policy_publication_events",
    }
    ddl = "\n".join(
        str(CreateTable(t).compile(dialect=dialect())) for t in Base.metadata.sorted_tables
    )
    assert "JSONB" in ddl
    assert "VECTOR(1024)" in ddl
    assert "NUMERIC(12, 2)" in ddl
    assert "TIMESTAMP WITH TIME ZONE" in ddl
    assert Claim.__table__.c.amount.nullable is False
    assert PolicyChunk.__table__.c.embedding.nullable is True


def test_migration_sql_upgrade_and_downgrade():
    out = io.StringIO()
    config = Config("alembic.ini", output_buffer=out)
    command.upgrade(config, "head", sql=True)
    sql = out.getvalue()
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    for table in Base.metadata.tables:
        assert f"CREATE TABLE {table}" in sql
    assert "USING gin" in sql
    assert "FOREIGN KEY(run_id, claim_id)" in sql
    out.truncate(0)
    out.seek(0)
    command.downgrade(config, "0001_m0:base", sql=True)
    assert "DROP TABLE claims" in out.getvalue()
    assert "DROP EXTENSION" not in out.getvalue()


def test_required_layout():
    dirs = [
        "apps/api",
        "apps/web",
        "agent/nodes",
        "agent/routing",
        "domain",
        "rules",
        "rag/ingestion",
        "rag/retrieval",
        "rag/embeddings",
        "integrations/erp",
        "integrations/llm",
        "database/models",
        "database/migrations",
        "evals/datasets",
        "evals/metrics",
        "evals/shadow_mode",
        "tests",
        "docs",
    ]
    assert all(Path(p).is_dir() for p in dirs)


def test_compose_static_wiring():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert services["postgres"]["image"].startswith("pgvector/pgvector:")
    assert services["migrate"]["command"] == ["python", "-m", "alembic", "upgrade", "head"]
    assert services["api"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert services["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert services["api"]["environment"]["DATA_SOURCE"] == "mock"
    assert "postgres_data" in compose["volumes"]
    assert "CREATE EXTENSION" in Path("database/init/001-vector.sql").read_text(encoding="utf-8")


def test_mock_policy_versions_and_case_labels():
    versions = json.loads(Path("evals/datasets/policy_versions.json").read_text(encoding="utf-8"))
    assert [v["authoritative_rules"]["hotel_limit_per_night"] for v in versions] == [
        "600.00",
        "900.00",
        "700.00",
    ]
    case = json.loads(Path("evals/datasets/audit_cases.json").read_text(encoding="utf-8"))[0]
    assert case["expected_policy_version_id"] == "POL-SZ-2026"
    assert case["expected_decision"] == "REJECT"
