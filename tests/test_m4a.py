"""Offline preparation tests do NOT certify a real PostgreSQL/embedding gate."""

import asyncio
import sys
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.exc import OperationalError

from apps.api.config import Settings
from apps.api.main import create_app
from database.models import PolicyChunk, PolicyVersion
from rag.embeddings.providers import (
    DeterministicTestEmbedding,
    SentenceTransformerEmbedding,
    validate_vectors,
)
from rag.ingestion.postgres import PostgresPolicyIngestor, chunk_policy, load_seed
from rag.retrieval.factory import create_embedding
from rag.retrieval.pgvector import PgVectorPolicyRetriever, resolve_authority, vector_statement


def filters(**overrides):
    return dict(
        legal_entity="DEMO-CN", category="HOTEL", city="深圳", incurred_on=date(2026, 9, 20),
        audit_started_at=datetime(2026, 10, 7, tzinfo=UTC), **overrides,
    )


def test_seed_versions_and_distractors_are_utf8_and_valid():
    documents = load_seed()
    assert len(documents) == 8
    assert [str(p.hotel_limit) for p in documents[:3]] == ["600.00", "900.00", "700.00"]
    assert {p.region for p in documents} == {"深圳", "北京"}
    assert {p.expense_type for p in documents} == {"HOTEL", "TRANSPORT", "MEAL"}
    assert any(p.status == "RETIRED" for p in documents)
    assert any(p.authority_level == 1 for p in documents)
    assert all("?" not in p.content for p in documents)


def test_deterministic_vectors_dimension_and_validation():
    provider = DeterministicTestEmbedding()
    assert provider.embed_query("住宿") == provider.embed_query("住宿")
    assert provider.embed_query("住宿") != provider.embed_query("交通")
    vectors = provider.embed_documents(["住宿", "交通"])
    assert len(validate_vectors(vectors, dimension=1024, count=2)) == 2


@pytest.mark.parametrize("vector", [[0.0] * 1024, [1.0], [float("nan")] * 1024,
                                   [float("inf")] * 1024])
def test_embedding_rejects_zero_wrong_dimension_nonfinite(vector):
    with pytest.raises(ValueError):
        validate_vectors([vector], dimension=1024, count=1)


def test_embedding_count_mismatch():
    with pytest.raises(ValueError, match="count"):
        validate_vectors([], dimension=1024, count=1)


def test_real_provider_is_lazy_and_validates_actual_model_dimension(monkeypatch):
    class Model:
        def get_sentence_embedding_dimension(self):
            return 768

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(
        SentenceTransformer=lambda *args, **kwargs: Model(),
    ))
    provider = SentenceTransformerEmbedding()
    assert provider._model is None
    with pytest.raises(ValueError, match="dimension"):
        provider.embed_query("深圳住宿")
    assert provider._model is None


def test_real_provider_mocked_inference_validates_vectors(monkeypatch):
    vector = DeterministicTestEmbedding().embed_query("深圳")
    model = SimpleNamespace(
        get_sentence_embedding_dimension=lambda: 1024,
        encode=lambda texts, **kwargs: SimpleNamespace(tolist=lambda: [vector for _ in texts]),
    )
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(
        SentenceTransformer=lambda *args, **kwargs: model,
    ))
    provider = SentenceTransformerEmbedding()
    assert provider.embed_query("深圳") == vector
    assert provider.embed_documents([]) == []


@pytest.mark.parametrize("dimension", [768, 0, -1])
def test_schema_dimension_mismatch(dimension):
    with pytest.raises(ValueError):
        SentenceTransformerEmbedding(dimension=dimension)
    embedding = SimpleNamespace(dimension=dimension)
    with pytest.raises(ValueError):
        PostgresPolicyIngestor(None, embedding)
    with pytest.raises(ValueError):
        PgVectorPolicyRetriever(None, embedding)


def test_deterministic_embeddings_only_in_test_configuration():
    with pytest.raises(ValueError, match="test configuration"):
        create_embedding(Settings(embedding_provider="deterministic", _env_file=None))
    provider = create_embedding(Settings(
        app_env="test", embedding_provider="deterministic", _env_file=None,
    ))
    assert isinstance(provider, DeterministicTestEmbedding)


def test_default_offline_mode_and_explicit_pgvector_no_override():
    assert Settings(_env_file=None).policy_retriever == "inmemory"
    with pytest.raises(ValueError, match="paired PostgreSQL"):
        create_app(Settings(policy_retriever="pgvector", _env_file=None), retriever=object())


def test_chunk_windows_deduplicate_and_preserve_tail():
    chunks = chunk_policy("abcdefghi", size=4, overlap=1)
    assert chunks == ["abcd", "defg", "ghi"]
    assert chunk_policy("aaaaaaaa", size=4, overlap=0) == ["aaaa"]
    with pytest.raises(ValueError):
        chunk_policy("x", size=4, overlap=4)
    with pytest.raises(ValueError):
        chunk_policy(" ")


def test_authority_date_specificity_and_equal_authority_conflict():
    def policy(authority=100, start=date(2026, 1, 1), city="深圳"):
        return SimpleNamespace(authority_level=authority, effective_from=start, city=city)
    top = policy()
    faq = policy(authority=1, start=date(2026, 9, 1))
    broad = policy(city="*")
    old = policy(start=date(2025, 1, 1))
    assert resolve_authority([faq, old, broad, top], "深圳") is top
    assert resolve_authority([top, policy()], "深圳") is None
    assert resolve_authority([], "深圳") is None


def test_vector_query_compiles_real_distance_and_all_filters_before_limit():
    stmt = vector_statement("POL-SZ-2026", [1.0] * 1024, "BAAI/bge-m3", filters=filters())
    sql = str(stmt.compile(dialect=dialect()))
    assert "<=>" in sql and "ORDER BY distance" in sql and "LIMIT" in sql
    for field in ("status", "legal_entity", "category", "city", "effective_from", "effective_to",
                  "policy_version_id", "published_at"):
        assert f"policy_versions.{field}" in sql
    assert "embedding_model" in str(stmt.compile(dialect=dialect()).params)
    assert PolicyVersion.__table__.c.status.nullable is False
    assert PolicyChunk.__table__.c.embedding.type.dim == 1024


def test_ingestion_embedding_failure_before_transaction():
    class BrokenEmbedding(DeterministicTestEmbedding):
        def embed_documents(self, texts):
            raise TimeoutError("embedding unavailable")
    with pytest.raises(TimeoutError):
        PostgresPolicyIngestor(None, BrokenEmbedding()).ingest(load_seed())


def test_connection_exception_is_not_empty_retrieval(monkeypatch):
    class BrokenSession:
        def __init__(self, *args, **kwargs):
            raise OperationalError("query", {}, Exception())
    monkeypatch.setattr("rag.retrieval.pgvector.Session", BrokenSession)
    retriever = PgVectorPolicyRetriever(
        SimpleNamespace(execution_options=lambda **kwargs: None), DeterministicTestEmbedding(),
    )
    with pytest.raises(ConnectionError):
        asyncio.run(retriever.retrieve("住宿", authoritative_version_id="POL-SZ-2026", **filters()))
    assert retriever.vector_queries == 0


def test_pgvector_dependency_failure_preserves_m3_fault_boundary():
    settings = Settings(
        app_env="test", policy_retriever="pgvector", embedding_provider="deterministic",
        database_url="postgresql+psycopg://unused@127.0.0.1:1/unused", _env_file=None,
    )
    with TestClient(create_app(settings)) as client:
        assert client.app.state.policy_retriever == "pgvector"
        operation = client.post("/claims/CLM-2026-00001/audit", json={
            "idempotency_key": "m4a-unreachable-database",
        }).json()
        assert operation["decision_result"]["decision"] != "APPROVE"
        service = client.app.state.audit_service
        async def snapshot():
            async with service.graph_session() as graph:
                return await graph.aget_state({"configurable": {"thread_id": operation["run_id"]}})
        state = asyncio.run(snapshot()).values
        assert state["audit_context"]["retrieval_outcome"] == "RETRIEVAL_SYSTEM_FAILURE"
        assert client.app.state.repository.erp_write_count("CLM-2026-00001") == 0
