"""Metadata authority first, lexical retrieval second; no vector service is claimed."""

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from domain.schemas import DTO, Money, PolicyEvidence
from integrations.faults import FailureMode, FaultInjector


class PolicyDocument(DTO):
    document_id: str
    title: str
    version: str
    effective_from: date
    effective_to: date | None
    status: Literal["ACTIVE", "RETIRED", "DRAFT"]
    region: str
    expense_type: str
    authority_level: int = Field(ge=0)
    legal_entity: str
    content: str
    hotel_limit: Money = Field(gt=0)


class PolicyCatalog:
    def __init__(self, documents: list[PolicyDocument] | None = None):
        path = Path(__file__).parents[1] / "seed" / "policies.json"
        self.documents = (
            documents
            if documents is not None
            else [
                PolicyDocument.model_validate(item)
                for item in json.loads(path.read_text(encoding="utf-8"))
            ]
        )
        if len({p.document_id for p in self.documents}) != len(self.documents):
            raise ValueError("Duplicate policy version IDs")
        if any(p.effective_to and p.effective_to <= p.effective_from for p in self.documents):
            raise ValueError("Invalid policy effective interval")

    def applicable(self, *, legal_entity: str, category: str, city: str, incurred_on: date):
        return [
            policy
            for policy in self.documents
            if policy.status == "ACTIVE"
            and policy.legal_entity == legal_entity
            and policy.region == city
            and policy.expense_type == category
            and policy.effective_from <= incurred_on
            and (policy.effective_to is None or incurred_on < policy.effective_to)
        ]

    def resolve(self, **filters) -> PolicyDocument | None:
        candidates = self.applicable(**filters)
        if not candidates:
            return None
        authority = max((p.authority_level, p.effective_from) for p in candidates)
        top = [p for p in candidates if (p.authority_level, p.effective_from) == authority]
        # Equal authority with conflicting versions is uncertainty, not auto-approval.
        return top[0] if len(top) == 1 else None


class InMemoryPolicyRetriever:
    def __init__(self, catalog: PolicyCatalog | None = None,
                 failure_mode: FailureMode = FailureMode.NONE):
        self.catalog = catalog or PolicyCatalog()
        self.faults = FaultInjector(failure_mode)

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
        self.faults.before_call()
        candidates = self.catalog.applicable(
            legal_entity=legal_entity, category=category, city=city, incurred_on=incurred_on
        )
        selected = self.catalog.resolve(
            legal_entity=legal_entity, category=category, city=city, incurred_on=incurred_on
        )
        if selected is None or selected.document_id != authoritative_version_id:
            return []
        tokens = set(re.findall(r"\w", query))
        scored = []
        for policy in candidates:
            if policy.document_id != authoritative_version_id:
                continue
            content = set(re.findall(r"\w", policy.content))
            score = len(tokens & content) / max(1, len(tokens | content))
            scored.append((score, policy))
        return [
            PolicyEvidence(
                policy_version_id=p.document_id,
                policy_key="CN-TRAVEL-HOTEL",
                version=p.version,
                effective_from=p.effective_from,
                effective_to=p.effective_to,
                chunk_id=f"{p.document_id}-HOTEL-1",
                source_uri=f"mock://policies/{p.document_id}",
                quote=p.content,
                relevance_score=score,
            )
            for score, p in sorted(scored, key=lambda item: item[0], reverse=True)
        ]
