"""Dynamic declarations also use the existing real SQL / pgvector authority path."""

import os

import pytest
from fastapi.testclient import TestClient
from test_m4a_postgres import pg_store as pg_store
from test_submission import payload

from apps.api.config import Settings
from apps.api.main import create_app
from rag.retrieval.pgvector import PgPolicyCatalog, PgVectorPolicyRetriever


@pytest.mark.integration
@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="Disposable PostgreSQL gate required"
)
def test_dynamic_claim_real_pgvector_workflow(pg_store):
    engine, _, embedding, _ = pg_store
    app = create_app(
        Settings(app_env="test", _env_file=None),
        catalog=PgPolicyCatalog(engine),
        retriever=PgVectorPolicyRetriever(engine, embedding),
    )
    with TestClient(app) as c:
        body = c.post("/api/v1.1/claims", json=payload()).json()
        dr = body["claim"]["decision_result"]
        assert dr["decision"] == "APPROVE"
        assert dr["policy_evidence"][0]["policy_version_id"] == "POL-SZ-2026"
        assert body["llm_analysis"]["provider"] == "mock"
        assert body["claim"]["erp_status"] == "SUCCEEDED"
