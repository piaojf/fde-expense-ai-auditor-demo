"""Composition root only; LangGraph nodes remain independent of backend selection."""

from sqlalchemy import create_engine

from rag.embeddings.providers import (
    DeterministicTestEmbedding,
    RemoteEmbeddingProvider,
    SentenceTransformerEmbedding,
)
from rag.retrieval.pgvector import PgPolicyCatalog, PgVectorPolicyRetriever


def create_embedding(settings):
    if settings.embedding_provider == "remote":
        return RemoteEmbeddingProvider(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            model_name=settings.embedding_model,
            dimension=settings.embedding_dimension,
            timeout=settings.embedding_timeout,
            max_attempts=settings.embedding_max_attempts,
            batch_size=settings.embedding_batch_size,
        )
    if settings.embedding_provider == "deterministic":
        if settings.app_env != "test":
            raise ValueError("Deterministic embeddings are restricted to test configuration")
        if settings.embedding_dimension != 1024:
            raise ValueError("M4A embedding dimension must be 1024")
        return DeterministicTestEmbedding()
    if settings.embedding_provider == "sentence_transformers":
        return SentenceTransformerEmbedding(
            settings.embedding_model, settings.embedding_dimension, settings.embedding_device,
        )
    raise ValueError("Unsupported embedding provider")


def create_policy_adapters(settings):
    engine = create_engine(
        settings.database_url, pool_pre_ping=True, connect_args={"connect_timeout": 3},
    )
    try:
        embedding = create_embedding(settings)
        return engine, PgPolicyCatalog(engine), PgVectorPolicyRetriever(engine, embedding)
    except Exception:
        engine.dispose()
        raise
