"""Business authority before exact PostgreSQL cosine distance; no memory fallback."""

import asyncio
from datetime import UTC, date, datetime
from time import perf_counter

from sqlalchemy import exists, or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, aliased

from database.models import PolicyChunk, PolicyPublicationEvent, PolicyVersion
from domain.schemas import PolicyEvidence
from rag.embeddings.providers import EmbeddingProvider, validate_vectors
from rag.retrieval.memory import PolicyDocument


def scope_predicates(*, legal_entity, category, city, incurred_on, audit_started_at=None,
                     require_effective_start=True, current_authority_only=False):
    """Scope a policy to a claim date.

    ``current_authority_only`` is the fallback for a claim dated before every version
    start: the version no longer in force is then simply replaced, whatever the claim
    date, instead of being revived by an event dated after the claim.
    """
    predicates = [
        PolicyVersion.status == "ACTIVE",
        PolicyVersion.legal_entity == legal_entity,
        PolicyVersion.category == category,
        PolicyVersion.city.in_([city, "*"]),
        # Existing frozen contract uses half-open intervals [from, to): the cutoff date
        # itself is not covered. Only this cutoff rejects a claim; a claim may predate
        # the stated start date, so the start date is a preference, not a requirement.
        or_(PolicyVersion.effective_to.is_(None), PolicyVersion.effective_to > incurred_on),
    ]
    if require_effective_start:
        predicates.append(PolicyVersion.effective_from <= incurred_on)
    visible_at = audit_started_at or datetime.now(UTC)
    predicates.append(PolicyVersion.published_at <= visible_at)
    replacement_scope = [] if current_authority_only else [
        PolicyPublicationEvent.effective_on <= incurred_on
    ]
    predicates.append(
        ~exists(
            select(PolicyPublicationEvent.event_id).where(
                PolicyPublicationEvent.predecessor_id == PolicyVersion.policy_version_id,
                *replacement_scope,
                PolicyPublicationEvent.recorded_at <= visible_at,
            )
        )
    )
    retired = aliased(PolicyVersion)
    predicates.append(
        ~exists(
            select(PolicyPublicationEvent.event_id)
            .join(retired, PolicyPublicationEvent.predecessor_id == retired.policy_version_id)
            .where(
                PolicyPublicationEvent.kind == "RETIRE",
                *replacement_scope,
                PolicyPublicationEvent.recorded_at <= visible_at,
                retired.legal_entity == legal_entity,
                retired.category == category,
                retired.city.in_([city, "*"]),
                PolicyVersion.effective_from < PolicyPublicationEvent.effective_on,
                # A later explicit publication is not retired by an older scope event.
                PolicyVersion.published_at <= PolicyPublicationEvent.recorded_at,
            )
        )
    )
    return predicates


def scope_rows(session, *, extra=(), **filters):
    """Resolve ordinary intervals, with an explicit published-hotel fallback.

    Strict in-range selection is tried first so an ordinary claim keeps the version
    in force on its own date. Only when no version covers that date exactly is the
    stated start date ignored for a published authoritative hotel matrix. Its
    cutoff is never relaxed; ordinary policies retain their effective interval.
    """
    strict = session.scalars(
        select(PolicyVersion).where(*scope_predicates(**filters), *extra)
    ).all()
    if strict:
        return strict
    return session.scalars(
        select(PolicyVersion).where(
            *scope_predicates(**filters, require_effective_start=False,
                              current_authority_only=True),
            PolicyVersion.category == "HOTEL",
            PolicyVersion.authority_level >= 100,
            PolicyVersion.authoritative_rules["rule_type"].as_string() == "HOTEL_LIMIT_MATRIX",
            *extra,
        )
    ).all()


def resolve_authority(candidates, city):
    if not candidates:
        return None

    def precedence(p):
        return p.authority_level, p.effective_from, int(p.city == city)

    rank = max(map(precedence, candidates))
    top = [p for p in candidates if precedence(p) == rank]
    # Conflicting equal authority/version dates remain UNKNOWN, never arbitrary approval.
    return top[0] if len(top) == 1 else None


def resolve_in_session(session, **filters):
    candidates = scope_rows(session, **filters)
    # Once this scope is governed, expiry must not fall back to a legacy FAQ
    # with a lower authority than the explicitly published uniform hotel rule.
    governed = session.scalar(
        select(
            exists(
                select(PolicyPublicationEvent.event_id)
                .join(
                    PolicyVersion,
                    PolicyPublicationEvent.policy_version_id == PolicyVersion.policy_version_id,
                )
                .where(
                    PolicyPublicationEvent.kind.in_(["PUBLISH", "ROLLBACK"]),
                    PolicyPublicationEvent.effective_on <= filters["incurred_on"],
                    PolicyPublicationEvent.recorded_at
                    <= (filters.get("audit_started_at") or datetime.now(UTC)),
                    PolicyVersion.legal_entity == filters["legal_entity"],
                    PolicyVersion.category == filters["category"],
                    PolicyVersion.city.in_([filters["city"], "*"]),
                )
            )
        )
    )
    if governed:
        candidates = [p for p in candidates if p.authority_level >= 100]
    return resolve_authority(candidates, filters["city"])


class PgPolicyCatalog:
    """The deterministic rule/human recheck adapter reads authority from PostgreSQL."""

    def __init__(self, engine):
        self.engine = engine

    def resolve_for_audit(self, *, audit_started_at, **filters):
        return self.resolve(audit_started_at=audit_started_at, **filters)

    def resolve_pinned(self, *, policy_version_id, audit_started_at, **filters):
        policy = self.resolve(audit_started_at=audit_started_at, **filters)
        return policy if policy and policy.document_id == policy_version_id else None

    def resolve(self, **filters):
        try:
            with Session(self.engine) as session:
                policy = resolve_in_session(session, **filters)
                if policy is None or not policy.authoritative_rules.get("hotel_limit_per_night"):
                    return None
                return PolicyDocument(
                    document_id=policy.policy_version_id,
                    title=policy.title,
                    version=policy.version,
                    status=policy.status,
                    effective_from=policy.effective_from,
                    effective_to=policy.effective_to,
                    region=policy.city,
                    expense_type=policy.category,
                    authority_level=policy.authority_level,
                    legal_entity=policy.legal_entity,
                    content="",
                    hotel_limit=policy.authoritative_rules["hotel_limit_per_night"],
                )
        except OperationalError as error:
            raise ConnectionError("Policy database dependency failed") from error


def vector_statement(policy_id, vector, model_name, *, filters, limit=5):
    distance = PolicyChunk.embedding.cosine_distance(vector).label("distance")
    return (
        select(PolicyChunk, PolicyVersion, distance)
        .join(PolicyVersion, PolicyChunk.policy_version_id == PolicyVersion.policy_version_id)
        .where(
            *scope_predicates(**filters),
            PolicyVersion.policy_version_id == policy_id,
            PolicyChunk.embedding.is_not(None),
            PolicyChunk.source_metadata["embedding_model"].astext == model_name,
        )
        .order_by(distance, PolicyChunk.chunk_id)
        .limit(limit)
    )


class PgVectorPolicyRetriever:
    def __init__(self, engine, embedding: EmbeddingProvider, *, top_k=5):
        if embedding.dimension != PolicyChunk.__table__.c.embedding.type.dim:
            raise ValueError("Provider dimension differs from pgvector schema")
        if not 1 <= top_k <= 100:
            raise ValueError("Invalid retrieval limit")
        self.engine, self.embedding, self.top_k = engine, embedding, top_k
        self.vector_queries = 0
        self.last_latency_ms = None

    async def retrieve(
        self,
        query: str,
        *,
        legal_entity: str,
        category: str,
        city: str,
        incurred_on: date,
        audit_started_at: datetime,
        authoritative_version_id: str,
    ) -> list[PolicyEvidence]:
        if audit_started_at.tzinfo is None:
            raise ValueError("Audit time must include timezone")
        filters = dict(
            legal_entity=legal_entity,
            category=category,
            city=city,
            incurred_on=incurred_on,
            audit_started_at=audit_started_at.astimezone(UTC),
        )
        return await asyncio.to_thread(self._retrieve, query, authoritative_version_id, filters)

    def _retrieve(self, query, authoritative_version_id, filters):
        started = perf_counter()
        try:
            snapshot_engine = self.engine.execution_options(isolation_level="REPEATABLE READ")
            with Session(snapshot_engine) as session:
                selected = resolve_in_session(session, **filters)
                if selected is None or selected.policy_version_id != authoritative_version_id:
                    return []
                vector = validate_vectors(
                    [self.embedding.embed_query(query)],
                    dimension=self.embedding.dimension,
                    count=1,
                )[0]
                models = session.scalars(
                    select(
                        PolicyChunk.source_metadata["embedding_model"].astext,
                    ).where(PolicyChunk.policy_version_id == selected.policy_version_id)
                ).all()
                if not models or any(m != self.embedding.model_name for m in models):
                    raise ValueError("Policy chunks missing or embedding model mismatch; re-ingest")
                statement = vector_statement(
                    selected.policy_version_id,
                    vector,
                    self.embedding.model_name,
                    # Chunks must be scoped exactly like the version that was selected:
                    # an earlier claim date legitimately ignores the start date.
                    filters={**filters,
                             "require_effective_start":
                                 selected.effective_from <= filters["incurred_on"],
                             "current_authority_only":
                                 selected.effective_from > filters["incurred_on"]},
                    limit=self.top_k,
                )
                rows = session.execute(statement).all()
                clause = selected.authoritative_rules.get("source", {}).get("quote")
                if clause and not any(clause in chunk.content for chunk, _, _ in rows):
                    clause_chunk = session.scalar(
                        select(PolicyChunk)
                        .where(
                            PolicyChunk.policy_version_id == selected.policy_version_id,
                            PolicyChunk.content.contains(clause, autoescape=True),
                            PolicyChunk.source_metadata["embedding_model"].astext
                            == self.embedding.model_name,
                            PolicyChunk.embedding.is_not(None),
                        )
                        .order_by(PolicyChunk.chunk_id)
                        .limit(1)
                    )
                    if clause_chunk is None:
                        raise ValueError("Authoritative rule clause missing from policy evidence")
                    rows = [*rows[: self.top_k - 1], (clause_chunk, selected, 1.0)]
                self.vector_queries += 1
                if not rows:
                    raise ValueError("Authoritative policy has no searchable embeddings")
                return [
                    PolicyEvidence(
                        policy_version_id=policy.policy_version_id,
                        policy_key=policy.policy_key,
                        version=policy.version,
                        effective_from=policy.effective_from,
                        effective_to=policy.effective_to,
                        chunk_id=chunk.chunk_id,
                        source_uri=policy.source_uri,
                        quote=chunk.content,
                        relevance_score=max(0.0, min(1.0, 1.0 - float(distance))),
                    )
                    for chunk, policy, distance in rows
                ]
        except OperationalError as error:
            raise ConnectionError("Policy database dependency failed") from error
        finally:
            self.last_latency_ms = (perf_counter() - started) * 1000
