"""Business state; defaults do not imply approval or a passed hard gate."""

from typing import Any
from uuid import UUID

from pydantic import Field

from domain.schemas import (
    DTO,
    AgentError,
    ClaimStatus,
    Decision,
    Document,
    Employee,
    ERPStatus,
    Expense,
    HardGateResult,
    HumanReview,
    Identifier,
    LLMAnalysis,
    PolicyEvidence,
    RiskLevel,
    RuleResult,
    RunMode,
)


class ClaimState(DTO):
    claim_id: Identifier
    run_id: UUID | None = None
    revision: int = Field(default=0, ge=0)
    mode: RunMode = RunMode.LIVE
    status: ClaimStatus = ClaimStatus.NEW
    employee: Employee | None = None
    expense: Expense | None = None
    documents: list[Document] = Field(default_factory=list)
    erp_context: dict[str, Any] = Field(default_factory=dict)
    rule_results: list[RuleResult] = Field(default_factory=list)
    hard_gate_results: list[HardGateResult] = Field(default_factory=list)
    policy_evidence: list[PolicyEvidence] = Field(default_factory=list)
    llm_analysis: LLMAnalysis | None = None
    risk_level: RiskLevel | None = None
    decision: Decision | None = None
    requires_human: bool = False
    human_review: HumanReview | None = None
    erp_status: ERPStatus = ERPStatus.NOT_STARTED
    errors: list[AgentError] = Field(default_factory=list)
    audit_context: dict[str, Any] = Field(default_factory=dict)
