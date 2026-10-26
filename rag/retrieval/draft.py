"""UI-P3A preview only. No resolver/fallback and no approval-facing API."""

from sqlalchemy import or_, select

from database.models import PolicyChunk, PolicyVersion
from rag.embeddings.providers import validate_vectors


def draft_vector_statement(policy_id, vector, model, dimension, body, *, limit=5):
    distance = PolicyChunk.embedding.cosine_distance(vector).label("distance")
    return (
        select(PolicyChunk, distance)
        .join(PolicyVersion, PolicyChunk.policy_version_id == PolicyVersion.policy_version_id)
        .where(
            PolicyVersion.policy_version_id == policy_id,
            PolicyVersion.status == "DRAFT",
            PolicyVersion.legal_entity == body.legal_entity,
            PolicyVersion.city == body.region,
            PolicyVersion.category == body.expense_type,
            PolicyVersion.version == body.version,
            PolicyVersion.effective_from <= body.incurred_on,
            or_(
                PolicyVersion.effective_to.is_(None), PolicyVersion.effective_to > body.incurred_on
            ),
            PolicyChunk.embedding.is_not(None),
            PolicyChunk.source_metadata["embedding_model"].astext == model,
            PolicyChunk.source_metadata["embedding_dimension"].as_integer() == dimension,
        )
        .order_by(distance, PolicyChunk.chunk_id)
        .limit(limit)
    )


class PgVectorDraftRetriever:
    def __init__(self, embedding):
        if embedding.dimension != PolicyChunk.__table__.c.embedding.type.dim:
            raise ValueError("Draft embedding dimension mismatch")
        self.embedding = embedding

    def retrieve(self, session, policy_id, body):
        chunks = session.scalars(
            select(PolicyChunk).where(
                PolicyChunk.policy_version_id == policy_id,
            )
        ).all()
        if not chunks or any(
            c.source_metadata.get("embedding_model") != self.embedding.model_name
            or c.source_metadata.get("embedding_dimension") != self.embedding.dimension
            for c in chunks
        ):
            raise ValueError("Draft model/dimension mismatch; cannot preview")
        vector = validate_vectors(
            [self.embedding.embed_query(body.query)], dimension=self.embedding.dimension, count=1
        )[0]
        rows = session.execute(
            draft_vector_statement(
                policy_id,
                vector,
                self.embedding.model_name,
                self.embedding.dimension,
                body,
            )
        ).all()
        return [
            dict(
                chunk_id=chunk.chunk_id,
                policy_version_id=policy_id,
                quote=chunk.content,
                relevance_score=max(0.0, min(1.0, 1.0 - float(distance))),
                source=chunk.source_metadata,
            )
            for chunk, distance in rows
        ]
