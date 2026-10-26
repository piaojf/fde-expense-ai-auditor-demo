"""Explicit short-key wire contract; frozen v2 identities retain their meaning."""

from typing import Annotated, Literal

from pydantic import Field

from domain.advisory_v2 import FindingCode
from domain.schemas import DTO

EvidenceKey = Annotated[str, Field(pattern=r"^E[0-9]{2,}$")]


class AdvisoryFindingV3(DTO):
    code: FindingCode
    classification: Literal["INFO", "WARNING", "BLOCKER"]
    resolution: Literal["NOT_APPLICABLE", "UNRESOLVED"]
    policy_ref_keys: list[EvidenceKey] = Field(min_length=1, max_length=12)
    fact_refs: list[str] = Field(min_length=1, max_length=16)
    rule_refs: list[str] = Field(max_length=16)
    fact_key: str | None = Field(default=None, max_length=64)
    source_quote: str | None = Field(default=None, max_length=300)
    question: str | None = Field(default=None, min_length=1, max_length=300)


class AdvisoryOutputV3(DTO):
    schema_version: Literal["p4b.advisory.3"] = "p4b.advisory.3"
    evidence_snapshot_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    risk_level: Literal["LOW", "MEDIUM", "HIGH", "UNDETERMINED"]
    recommendation: Literal[
        "RECOMMEND_APPROVAL", "RECOMMEND_REJECTION", "REQUEST_EVIDENCE", "ESCALATE", "UNDETERMINED",
    ]
    issues: list[AdvisoryFindingV3] = Field(max_length=16)
    missing_evidence: list[str] = Field(max_length=24)
    policy_ref_keys: list[EvidenceKey] = Field(min_length=1, max_length=16)
    fact_refs: list[str] = Field(min_length=1, max_length=20)
    requires_human_review: Literal[True]
