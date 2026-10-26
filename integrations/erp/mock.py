from typing import Any

from apps.api.repository import MockClaimRepository
from domain.schemas import Decision, DecisionResult
from integrations.faults import FailureMode, FaultInjector


class MockERP:
    """M1 mock read/write adapter; no external financial system is contacted."""

    def __init__(self, repository: MockClaimRepository | None = None,
                 failure_mode: FailureMode = FailureMode.NONE):
        self.repository = repository or MockClaimRepository()
        self.faults = FaultInjector(failure_mode)

    async def fetch_claim_context(self, claim_id: str) -> dict[str, Any]:
        claim = self.repository.get(claim_id)
        if claim is None:
            raise ValueError("Claim not found")
        return {
            "source": "mock",
            "claim_id": claim.claim_id,
            "employee_active": True,
            "duplicate_confirmed": False,
            "employee": claim.employee.model_dump(mode="json"),
            "department": claim.employee.department,
            "claim": {"claim_id": claim.claim_id, "status": claim.status.value},
        }

    async def write_decision(
        self, result: DecisionResult, *, idempotency_key: str
    ) -> dict[str, Any]:
        if not isinstance(result, DecisionResult) or result.decision not in (
            Decision.APPROVE,
            Decision.REJECT,
        ):
            raise ValueError("Mock ERP writes require a validated APPROVE/REJECT result")
        result = DecisionResult.model_validate(result.model_dump())
        if not idempotency_key:
            raise ValueError("ERP write key is required")
        if self.repository.get(result.claim_id) is None:
            raise ValueError("Claim not found")
        status = "APPROVED" if result.decision == Decision.APPROVE else "REJECTED"
        self.faults.before_call()
        if self.faults.mode == FailureMode.INVALID_RESPONSE:
            return {"status": "INVALID"}
        response = self.repository.write_erp(result.claim_id, status, key=idempotency_key)
        self.faults.after_commit()
        return response
