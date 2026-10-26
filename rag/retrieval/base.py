from datetime import date, datetime
from typing import Protocol

from domain.schemas import PolicyEvidence


class PolicyRetriever(Protocol):
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
    ) -> list[PolicyEvidence]: ...
