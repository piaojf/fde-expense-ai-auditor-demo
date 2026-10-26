"""Advice from current declared data; finance owns verification and final action."""

from typing import Literal

from pydantic import Field

from domain.advisory_v3 import EvidenceKey
from domain.schemas import DTO


class CurrentDataFinding(DTO):
    topic: Literal[
        "BUSINESS_PURPOSE", "POLICY_APPLICABILITY", "FACT_POLICY_CONFLICT",
        "EVIDENCE_SUFFICIENCY", "DUPLICATE_SPLIT_CATEGORY", "AUTHORIZATION",
        "UNRESOLVED_QUESTIONS", "EXCEPTION_REASON",
    ]
    basis: Literal["BUSINESS", "POLICY", "HOTEL", "INVOICE", "AUTHORIZATION",
                   "DUPLICATE", "EXCEPTION", "ALLOWANCE", "ENTERTAINMENT"]
    severity: Literal["LOW", "MEDIUM", "HIGH"]
    explanation: str = Field(min_length=1, max_length=500)
    finance_check: str = Field(min_length=1, max_length=300)
    policy_ref_keys: list[EvidenceKey] = Field(min_length=1, max_length=12)
    fact_refs: list[str] = Field(min_length=1, max_length=12)


class CityClassificationV4(DTO):
    city: str = Field(min_length=1, max_length=80)
    tier: Literal["I", "II", "III", "IV", "UNKNOWN"]
    explanation: str = Field(min_length=1, max_length=300)
    policy_ref_keys: list[EvidenceKey] = Field(min_length=1, max_length=4)


class AdvisoryOutputV4(DTO):
    schema_version: Literal["p4b.advisory.4"] = "p4b.advisory.4"
    evidence_snapshot_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    risk_level: Literal["LOW", "MEDIUM", "HIGH", "UNDETERMINED"]
    recommendation: Literal[
        "RECOMMEND_APPROVAL", "RECOMMEND_REJECTION", "REQUEST_EVIDENCE", "ESCALATE", "UNDETERMINED",
    ]
    summary: str = Field(min_length=1, max_length=1000)
    # Required by validation for hotel claims; null for expense types without city tiers.
    city_classification: CityClassificationV4 | None = None
    issues: list[CurrentDataFinding] = Field(max_length=8)
    # Verification questions live in finance_check, never in a mandatory evidence list.
    missing_evidence: list[str] = Field(default_factory=list, max_length=0)
    policy_ref_keys: list[EvidenceKey] = Field(min_length=1, max_length=16)
    fact_refs: list[str] = Field(min_length=1, max_length=64)
    requires_human_review: Literal[True]
