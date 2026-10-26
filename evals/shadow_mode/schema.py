"""Internal shadow records; no changes to frozen public claim/API DTOs."""

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from domain.schemas import DTO, Decision, RiskLevel


class ReferenceSource(StrEnum):
    SYNTHETIC_RULE_LABEL = "SYNTHETIC_RULE_LABEL"
    EXPERT_REVIEWED = "EXPERT_REVIEWED"
    HISTORICAL_HUMAN_DECISION = "HISTORICAL_HUMAN_DECISION"


class ShadowResult(DTO):
    shadow_run_id: UUID
    case_id: str
    dataset_version: str
    policy_version: str | None
    policy_fixture_sha256: str
    model_provider: str
    model_name: str
    prompt_version: str
    reference_source: ReferenceSource
    reference_decision: Decision
    agent_decision: Decision | None
    agent_risk: RiskLevel | None
    requires_human: bool | None
    decision_match: bool
    policy_match: bool
    failure_reason: str | None
    execution_status: Literal["COMPLETED", "DEPENDENCY_FAILED", "FAILED"]
    latency_ms: float = Field(ge=0)
    token_usage: dict
    timestamp: AwareDatetime
    evaluation_row: dict
    # Observed boundary probes; persisted per case as well as summed per run.
    side_effects: dict[str, int]

    def metric_row(self):
        return self.evaluation_row


def timestamp() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)
