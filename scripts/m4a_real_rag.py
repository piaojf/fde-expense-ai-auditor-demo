"""Explicit real-embedding/pgvector seed and verification, never claims browser E2E."""

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from apps.api.config import Settings  # noqa: E402
from apps.api.main import create_app  # noqa: E402
from rag.embeddings.remote import RemoteEmbeddingError  # noqa: E402
from rag.ingestion.postgres import PostgresPolicyIngestor, load_seed  # noqa: E402
from rag.retrieval.factory import create_embedding  # noqa: E402
from rag.retrieval.pgvector import PgPolicyCatalog, PgVectorPolicyRetriever  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["probe", "seed", "verify"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    engine = None
    report = dict(
        REAL_POSTGRES_VERIFIED=False, REAL_PGVECTOR_VERIFIED=False,
        REAL_EMBEDDING_VERIFIED=False, REAL_VECTOR_QUERY_VERIFIED=False,
        REAL_BROWSER_E2E_WITH_PGVECTOR="NOT_RUN", M4A_STATUS="NOT_CERTIFIED",
    )
    try:
        settings = Settings(policy_retriever="pgvector")
        if settings.embedding_provider != "remote":
            raise ValueError("Mac real verification requires remote embeddings; no local download")
        embedding = create_embedding(settings)
        probe = embedding.embed_query("深圳住宿制度标准")
        assert len(probe) == settings.embedding_dimension
        report.update(
            REAL_EMBEDDING_VERIFIED=True, embedding_provider=type(embedding).__name__,
            embedding_model=embedding.actual_model, embedding_dimension=len(probe),
        )
        if args.action != "probe":
            engine = create_engine(settings.database_url, connect_args={"connect_timeout": 3})
            verify_database(args, settings, engine, embedding, report)
    except Exception as error:
        report.update(ERROR_TYPE=type(error).__name__, RESULT="FAIL")
        if isinstance(error, RemoteEmbeddingError):
            report["EMBEDDING_ERROR"] = error.as_dict()
            if error.code == "API_KEY_REQUIRED" or error.status_code in {401, 403}:
                report.update(RESULT="USER_ACTION_REQUIRED", M4A_STATUS="USER_ACTION_REQUIRED")
        status = 1
    else:
        report["RESULT"] = "PASS"
        status = 0
    finally:
        if engine is not None:
            engine.dispose()
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)
    return status


def verify_database(args, settings, engine, embedding, report):
    with engine.connect() as c:
        report["postgres_version"] = c.scalar(text("SHOW server_version"))
        report["REAL_POSTGRES_VERIFIED"] = True
    if args.action == "seed":
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["database_url"] = settings.database_url
        command.upgrade(config, "head")
    with engine.connect() as c:
        extension = c.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
        assert extension, "pgvector extension not installed"
        report.update(pgvector_version=extension, REAL_PGVECTOR_VERIFIED=True)
    ingestor = PostgresPolicyIngestor(engine, embedding)
    if args.action == "seed":
        ingestor.ingest(load_seed())
        before = ingestor.counts()
        assert before == {"policy_documents": 8, "chunks": 8}
        with engine.connect() as c:
            ids = c.execute(text("SELECT chunk_id FROM policy_chunks ORDER BY chunk_id"))\
                .scalars().all()
        ingestor.ingest(load_seed())
        assert ingestor.counts() == before
        with engine.connect() as c:
            assert c.execute(text("SELECT chunk_id FROM policy_chunks ORDER BY chunk_id"))\
                .scalars().all() == ids
        report["IDEMPOTENCY"] = "PASS"
    report.update(ingestor.counts())
    retriever = PgVectorPolicyRetriever(engine, embedding)
    filters = dict(
        legal_entity="DEMO-CN", category="HOTEL", city="深圳", incurred_on=date(2026, 9, 20),
    )
    from datetime import UTC, datetime

    async def retrieve(**overrides):
        return await retriever.retrieve(
            load_seed()[1].content, **{**filters, **overrides},
            audit_started_at=datetime.now(UTC), authoritative_version_id="POL-SZ-2026",
        )
    latency = []
    for _ in range(5):
        evidence = asyncio.run(retrieve())
        assert evidence and all(p.version == "2026" for p in evidence)
        latency.append(round(retriever.last_latency_ms, 3))
    policy = PgPolicyCatalog(engine).resolve(**filters)
    assert policy and str(policy.hotel_limit) == "700.00"
    for overrides in [dict(city="无匹配地区"), dict(legal_entity="OTHER"),
                      dict(category="OTHER"), dict(incurred_on=date(2027, 1, 1))]:
        assert asyncio.run(retrieve(**overrides)) == []
    query_vector = embedding.embed_query(load_seed()[1].content)
    with engine.connect() as c:
        nearest = c.scalar(text(
            "SELECT policy_version_id FROM policy_chunks "
            "ORDER BY embedding <=> CAST(:vector AS vector) LIMIT 1",
        ), {"vector": str(query_vector)})
        assert nearest == "POL-SZ-2025"
        assert c.scalar(text(
            "SELECT count(*) FROM policy_chunks WHERE "
            "source_metadata->>'embedding_model' IS DISTINCT FROM :model "
            "OR embedding IS NULL OR vector_dims(embedding) <> :dimension",
        ), {"model": embedding.model_name, "dimension": embedding.dimension}) == 0
    report.update(
        REAL_VECTOR_QUERY_VERIFIED=True, POLICY_VERSION_RESOLUTION="PASS",
        HISTORICAL_POLICY_TRAP="PASS", NO_MATCHING_POLICY="PASS",
        METADATA_FILTER="PASS", nearest_unfiltered_policy=nearest,
        vector_query_count=retriever.vector_queries, retrieval_latency_ms=latency,
        evidence=[p.model_dump(mode="json") for p in evidence],
    )
    # In-memory claim/checkpoint fixture only; policy authority still reads the REAL database.
    with TestClient(create_app(settings.model_copy(update={"app_env": "test"}))) as client:
        for claim_id, decision, risk in [("CLM-2026-00001", "APPROVE", "LOW"),
                                         ("CLM-2026-00004", "HUMAN_REVIEW", "MEDIUM")]:
            response = client.post(f"/claims/{claim_id}/audit", json={
                "idempotency_key": f"real-{claim_id}",
            })
            assert response.status_code == 202
            operation = response.json()
            result = operation["decision_result"]
            assert result["decision"] == decision and result["risk_level"] == risk
            assert result["rule_results"][0]["outcome"] == "PASS"
            assert result["rule_results"][0]["limit"] == "700.00"
            assert all(p["version"] == "2026" for p in result["policy_evidence"])
            if claim_id == "CLM-2026-00001":
                assert operation["status"] == "COMPLETED"
                assert client.get(f"/claims/{claim_id}").json()["erp_status"] == "SUCCEEDED"
                assert client.app.state.repository.erp_write_count(claim_id) == 1
            else:
                assert operation["status"] == "PENDING_HUMAN"
            report[claim_id] = operation
        operation = report["CLM-2026-00004"]
        review = client.post("/claims/CLM-2026-00004/human-review", json={
            "run_id": operation["run_id"], "expected_revision": operation["revision"],
            "decision": "APPROVE", "reviewer_id": "finance_reviewer_01",
            "comment": "航班取消证明及额外住宿凭证已核实", "idempotency_key": "real-review",
        })
        assert review.status_code == 200
        resumed = client.post("/claims/CLM-2026-00004/resume", json={
            "run_id": operation["run_id"], "expected_revision": review.json()["revision"],
            "idempotency_key": "real-resume",
        })
        assert resumed.status_code == 202 and resumed.json()["status"] == "COMPLETED"
        assert client.get("/claims/CLM-2026-00004").json()["erp_status"] == "SUCCEEDED"
        assert client.app.state.repository.erp_write_count("CLM-2026-00004") == 1
        report["HUMAN_RESUME"] = "PASS"


if __name__ == "__main__":
    raise SystemExit(main())
