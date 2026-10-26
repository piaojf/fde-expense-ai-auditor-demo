"""Atomic document/chunk ingestion into the existing policy_versions schema."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from database.models import PolicyChunk, PolicyVersion
from rag.embeddings.providers import EmbeddingProvider, validate_vectors
from rag.retrieval.memory import PolicyCatalog, PolicyDocument


def chunk_policy(content: str, size=800, overlap=100) -> list[str]:
    if size < 1 or not 0 <= overlap < size:
        raise ValueError("Invalid chunk window")
    if not content.strip():
        raise ValueError("Empty policy content")
    return list(dict.fromkeys(
        content[start:start + size] for start in range(0, len(content), size - overlap)
    ))


def load_seed(path: Path | None = None) -> list[PolicyDocument]:
    path = path or Path(__file__).parents[1] / "seed" / "policies-m4a.json"
    documents = [
        PolicyDocument.model_validate(v)
        for v in json.loads(path.read_text(encoding="utf-8"))
    ]
    return PolicyCatalog(documents).documents


class PostgresPolicyIngestor:
    def __init__(self, engine, embedding: EmbeddingProvider):
        if embedding.dimension != PolicyChunk.__table__.c.embedding.type.dim:
            raise ValueError("Provider dimension differs from pgvector schema")
        self.engine, self.embedding = engine, embedding

    def ingest(self, documents: list[PolicyDocument]):
        PolicyCatalog(documents)  # Validate IDs and effective intervals before any write.
        prepared = []
        for document in documents:
            chunks = chunk_policy(document.content)
            vectors = validate_vectors(
                self.embedding.embed_documents(chunks),
                dimension=self.embedding.dimension, count=len(chunks),
            )
            prepared.append((document, chunks, vectors))
        # One batch transaction; a constraint failure also rolls back preceding documents.
        with Session(self.engine) as session, session.begin():
            for document, chunks, vectors in prepared:
                values = dict(
                    policy_version_id=document.document_id,
                    policy_key="CN-TRAVEL-" + document.document_id.removesuffix(
                        f"-{document.version}",
                    ),
                    version=document.version, title=document.title, status=document.status,
                    authority_level=document.authority_level, legal_entity=document.legal_entity,
                    category=document.expense_type, city=document.region,
                    effective_from=document.effective_from, effective_to=document.effective_to,
                    published_at=datetime.combine(
                        document.effective_from, datetime.min.time(), UTC,
                    ),
                    authoritative_rules={"hotel_limit_per_night": str(document.hotel_limit)},
                    source_uri=f"seed://policies/{document.document_id}",
                    content_sha256=hashlib.sha256(document.content.encode()).hexdigest(),
                )
                stmt = insert(PolicyVersion).values(**values)
                session.execute(stmt.on_conflict_do_update(
                    index_elements=[PolicyVersion.policy_version_id], set_=values,
                ))
                # Locked parent upsert serializes re-ingestion of this document.
                session.execute(delete(PolicyChunk).where(
                    PolicyChunk.policy_version_id == document.document_id,
                ))
                for index, (content, vector) in enumerate(zip(chunks, vectors, strict=True)):
                    digest = hashlib.sha256(content.encode()).hexdigest()
                    session.add(PolicyChunk(
                        chunk_id=f"{document.document_id}:{digest[:32]}",
                        policy_version_id=document.document_id, chunk_hash=digest,
                        content=content, section=f"section-{index + 1}", embedding=vector,
                        source_metadata={
                            "embedding_model": self.embedding.model_name,
                            "embedding_dimension": self.embedding.dimension,
                            "version": document.version, "section": index + 1,
                        },
                    ))
        return {"documents_ingested": len(prepared),
                "chunks_ingested": sum(len(chunks) for _, chunks, _ in prepared)}

    def counts(self):
        from sqlalchemy import func

        with Session(self.engine) as session:
            return {
                "policy_documents": session.scalar(select(func.count()).select_from(PolicyVersion)),
                "chunks": session.scalar(select(func.count()).select_from(PolicyChunk)),
            }
