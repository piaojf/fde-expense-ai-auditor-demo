"""Opt-in real pgvector SQL tests, isolated schemas in a dedicated test database."""

import asyncio
import os
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DataError

from apps.api.config import Settings
from apps.api.main import create_app
from database.models import Base
from rag.embeddings.providers import DeterministicTestEmbedding
from rag.ingestion.postgres import PostgresPolicyIngestor, load_seed
from rag.retrieval.pgvector import PgPolicyCatalog, PgVectorPolicyRetriever

URL = os.getenv("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL unset; real pgvector gate unverified"),
]


def filters():
    return dict(
        legal_entity="DEMO-CN", category="HOTEL", city="深圳", incurred_on=date(2026, 9, 20),
        audit_started_at=datetime(2026, 10, 7, tzinfo=UTC),
    )


@pytest.fixture
def pg_store():
    url = make_url(URL)
    assert (url.database or "").startswith("fde_m0_test_"), "Dedicated fde_m0_test_* DB required"
    schema = "m4a_test_" + uuid4().hex
    admin = create_engine(url, connect_args={"connect_timeout": 3})
    with admin.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    # Only a freshly generated, fixture-owned schema is ever dropped.
    test_url = url.update_query_dict({"options": f"-csearch_path={schema},public"})
    engine = create_engine(test_url)
    try:
        with engine.begin() as connection:
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            command.upgrade(config, "head")
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
            assert connection.scalar(text(
                "SELECT extversion FROM pg_extension WHERE extname='vector'",
            ))
        embedding = DeterministicTestEmbedding()
        ingestor = PostgresPolicyIngestor(engine, embedding)
        ingestor.ingest(load_seed())
        yield engine, ingestor, embedding, test_url
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_historical_policy_cannot_override_active_policy(pg_store):
    engine, _, embedding, _ = pg_store
    retriever = PgVectorPolicyRetriever(engine, embedding)
    statements = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, stmt, p, ctx, many:
                 statements.append(stmt))
    query = load_seed()[1].content  # Intentionally identical to invalid 2025 text.
    vector = str(embedding.embed_query(query))
    with engine.connect() as connection:
        nearest_unfiltered = connection.scalar(text(
            "SELECT policy_version_id FROM policy_chunks ORDER BY embedding <=> CAST(:v AS vector) "
            "LIMIT 1",
        ), {"v": vector})
    assert nearest_unfiltered == "POL-SZ-2025"
    evidence = asyncio.run(retriever.retrieve(
        query, **filters(), authoritative_version_id="POL-SZ-2026",
    ))
    assert [p.version for p in evidence] == ["2026"]
    policy = PgPolicyCatalog(engine).resolve(
        legal_entity="DEMO-CN", category="HOTEL", city="深圳", incurred_on=date(2026, 9, 20),
    )
    assert str(policy.hotel_limit) == "700.00"
    assert any("<=>" in stmt and "policy_versions.status" in stmt for stmt in statements)
    assert retriever.vector_queries == 1


def test_reingestion_idempotency_and_stable_chunk_ids(pg_store):
    engine, ingestor, _, _ = pg_store
    before = ingestor.counts()
    with engine.connect() as c:
        ids = c.execute(text(
            "SELECT chunk_id FROM policy_chunks ORDER BY chunk_id",
        )).scalars().all()
    ingestor.ingest(load_seed())
    assert ingestor.counts() == before == {"policy_documents": 8, "chunks": 8}
    with engine.connect() as c:
        assert c.execute(text(
            "SELECT chunk_id FROM policy_chunks ORDER BY chunk_id",
        )).scalars().all() == ids
        assert c.scalar(text(
            "SELECT count(*) FROM (SELECT policy_version_id,chunk_hash FROM policy_chunks "
            "GROUP BY policy_version_id,chunk_hash HAVING count(*) > 1) duplicates",
        )) == 0


def test_ingestion_transaction_rolls_back_preceding_document(pg_store):
    engine, ingestor, _, _ = pg_store
    before = ingestor.counts()
    valid = load_seed()[2].model_copy(update={"document_id": "POL-NEW", "version": "next"})
    invalid = valid.model_copy(update={"document_id": "x" * 200, "version": "bad"})
    with pytest.raises(DataError):
        ingestor.ingest([valid, invalid])
    assert ingestor.counts() == before
    with engine.connect() as c:
        assert c.scalar(text(
            "SELECT count(*) FROM policy_versions WHERE policy_version_id='POL-NEW'",
        )) == 0


def test_no_policy_date_scope_status_and_model_mismatch(pg_store):
    engine, _, embedding, _ = pg_store
    retriever = PgVectorPolicyRetriever(engine, embedding)
    for field, value in [("city", "不存在的地区"), ("legal_entity", "OTHER"),
                         ("category", "OTHER"), ("incurred_on", date(2027, 1, 1))]:
        selected_filters = filters()
        selected_filters[field] = value
        assert asyncio.run(retriever.retrieve(
            "住宿", **selected_filters, authoritative_version_id="POL-SZ-2026",
        )) == []
    embedding.model_name = "different-model-with-same-dimension"
    with pytest.raises(ValueError, match="model mismatch"):
        asyncio.run(retriever.retrieve(
            "住宿", **filters(), authoritative_version_id="POL-SZ-2026",
        ))


def test_case1_case4_real_pgvector_and_human_resume(pg_store):
    _, _, _, url = pg_store
    settings = Settings(
        app_env="test", policy_retriever="pgvector", embedding_provider="deterministic",
        database_url=url.render_as_string(hide_password=False), _env_file=None,
    )
    with TestClient(create_app(settings)) as client:
        retriever = client.app.state.audit_service.graph_factory.keywords["retriever"]
        assert isinstance(retriever, PgVectorPolicyRetriever)
        case1 = client.post("/claims/CLM-2026-00001/audit", json={
            "idempotency_key": "pg-case1",
        }).json()
        assert case1["decision_result"]["decision"] == "APPROVE"
        assert case1["decision_result"]["risk_level"] == "LOW"
        assert case1["decision_result"]["rule_results"][0]["limit"] == "700.00"
        case4 = client.post("/claims/CLM-2026-00004/audit", json={
            "idempotency_key": "pg-case4",
        }).json()
        assert case4["status"] == "PENDING_HUMAN"
        assert case4["decision_result"]["decision"] == "HUMAN_REVIEW"
        assert case4["decision_result"]["risk_level"] == "MEDIUM"
        assert case4["decision_result"]["rule_results"][0]["outcome"] == "PASS"
        assert [p["version"] for p in case4["decision_result"]["policy_evidence"]] == ["2026"]
        review = client.post("/claims/CLM-2026-00004/human-review", json={
            "run_id": case4["run_id"], "expected_revision": case4["revision"],
            "decision": "APPROVE", "reviewer_id": "finance_reviewer_01",
            "comment": "已核实航班取消及住宿凭证", "idempotency_key": "pg-review",
        })
        assert review.status_code == 200
        resumed = client.post("/claims/CLM-2026-00004/resume", json={
            "run_id": case4["run_id"], "expected_revision": review.json()["revision"],
            "idempotency_key": "pg-resume",
        })
        assert resumed.status_code == 202 and resumed.json()["status"] == "COMPLETED"
        assert retriever.vector_queries == 2


def test_remote_http_adapter_composes_with_real_pgvector_without_memory_fallback(pg_store,
                                                                                monkeypatch):
    """Real SQL plus MOCK HTTP embeddings; not a live remote embedding acceptance."""
    import json
    from types import SimpleNamespace

    import httpx

    from rag.embeddings.providers import RemoteEmbeddingProvider
    from scripts.m4a_real_rag import verify_database

    engine, _, deterministic, url = pg_store
    calls = []
    def handler(request):
        body = json.loads(request.content)
        inputs = body['input']
        inputs = [inputs] if isinstance(inputs, str) else inputs
        calls.append(inputs)
        return httpx.Response(200, json={
            'model': 'BAAI/bge-m3',
            'data': [dict(index=i, embedding=deterministic.embed_query(t))
                     for i, t in enumerate(inputs)],
        })
    remote = RemoteEmbeddingProvider(
        base_url='https://mock-embeddings.example/v1', api_key='mock-test-only',
        model_name='BAAI/bge-m3', transport=httpx.MockTransport(handler),
    )
    ingestor = PostgresPolicyIngestor(engine, remote)
    ingestor.ingest(load_seed())
    monkeypatch.setattr('rag.retrieval.factory.create_embedding', lambda settings: remote)
    settings = Settings(
        app_env='test', policy_retriever='pgvector', embedding_provider='remote',
        database_url=url.render_as_string(hide_password=False), _env_file=None,
    )
    with TestClient(create_app(settings)) as client:
        retriever = client.app.state.audit_service.graph_factory.keywords['retriever']
        assert isinstance(retriever, PgVectorPolicyRetriever) and retriever.embedding is remote
        response = client.post('/claims/CLM-2026-00001/audit', json={
            'idempotency_key': 'mock-http-real-sql',
        })
        assert response.status_code == 202
        result = response.json()
        assert result['status'] == 'COMPLETED'
        assert client.get('/claims/CLM-2026-00001').json()['erp_status'] == 'SUCCEEDED'
        assert result['decision_result']['decision'] == 'APPROVE'
        assert [p['version'] for p in result['decision_result']['policy_evidence']] == ['2026']
        assert retriever.vector_queries == 1 and len(calls) == 9
    report = {}
    verify_database(SimpleNamespace(action='seed'), settings, engine, remote, report)
    assert report['IDEMPOTENCY'] == 'PASS' and report['HISTORICAL_POLICY_TRAP'] == 'PASS'
    assert report['METADATA_FILTER'] == 'PASS' and report['HUMAN_RESUME'] == 'PASS'


def test_780_hard_block_human_approve_409_with_pg_catalog(pg_store, tmp_path):
    import json
    from pathlib import Path

    from apps.api.repository import MockClaimRepository
    from integrations.documents.mock import MockDocumentParser

    _, _, _, url = pg_store
    fixtures = Path('apps/api/mock_data')
    claims = json.loads((fixtures / 'claims.json').read_text(encoding='utf-8'))
    docs = json.loads((fixtures / 'documents.json').read_text(encoding='utf-8'))
    claim_id = 'CLM-2026-00004'
    for item in claims:
        if item['claim_id'] == claim_id:
            item['expense']['amount'] = '780.00'
    for item in docs:
        if item['claim_id'] == claim_id and 'expense' in item:
            item['expense']['amount'] = '780.00'
    claim_path, doc_path = tmp_path / 'claims.json', tmp_path / 'documents.json'
    claim_path.write_text(json.dumps(claims), encoding='utf-8')
    doc_path.write_text(json.dumps(docs), encoding='utf-8')
    repo = MockClaimRepository(claim_path)
    settings = Settings(
        app_env='test', policy_retriever='pgvector', embedding_provider='deterministic',
        database_url=url.render_as_string(hide_password=False), _env_file=None,
    )
    try:
        with TestClient(create_app(settings, repo, parser=MockDocumentParser(doc_path))) as client:
            response = client.post(f'/claims/{claim_id}/audit', json={'idempotency_key': 'pg-780'})
            assert response.status_code == 202
            op = response.json()
            assert op['decision_result']['decision'] == 'REJECT'
            rule = op['decision_result']['rule_results'][0]
            assert rule['actual'] == '780.00' and rule['limit'] == '700.00'
            review = client.post(f'/claims/{claim_id}/human-review', json={
                'run_id': op['run_id'], 'expected_revision': op['revision'],
                'decision': 'APPROVE', 'reviewer_id': 'finance_reviewer_01',
                'comment': 'attempted override', 'idempotency_key': 'pg-780-review',
            })
            assert review.status_code == 409 and repo.erp_write_count(claim_id) == 0
    finally:
        repo.close()


def test_lost_erp_response_retries_once_write_with_real_pgvector(pg_store):
    from apps.api.repository import MockClaimRepository
    from integrations.erp.mock import MockERP
    from integrations.faults import FailureMode

    _, _, _, url = pg_store
    repo = MockClaimRepository()
    settings = Settings(
        app_env='test', policy_retriever='pgvector', embedding_provider='deterministic',
        database_url=url.render_as_string(hide_password=False), _env_file=None,
    )
    try:
        erp = MockERP(repo, failure_mode=FailureMode.SUCCESS_THEN_RESPONSE_LOST)
        with TestClient(create_app(settings, repo, erp=erp)) as client:
            response = client.post('/claims/CLM-2026-00001/audit', json={
                'idempotency_key': 'pg-lost-response',
            })
            assert response.status_code == 202
            assert response.json()['status'] == 'COMPLETED'
            assert client.get('/claims/CLM-2026-00001').json()['erp_status'] == 'SUCCEEDED'
            assert repo.erp_write_count('CLM-2026-00001') == 1
            assert any(e.event_type == 'IDEMPOTENT_REPLAY'
                       for e in repo.events('CLM-2026-00001'))
    finally:
        repo.close()
