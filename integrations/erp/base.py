from typing import Any, Protocol

from domain.schemas import DecisionResult


class ERPAdapter(Protocol):
    async def fetch_claim_context(self, claim_id: str) -> dict[str, Any]: ...

    async def write_decision(
        self, result: DecisionResult, *, idempotency_key: str
    ) -> dict[str, Any]: ...
