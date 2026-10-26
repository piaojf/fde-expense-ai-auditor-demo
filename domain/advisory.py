"""Versioned, non-authoritative AI advice. Finance authority is a separate record."""

from typing import Literal

from pydantic import Field, model_validator

from domain.schemas import DTO, IdempotencyKey


class AdvisoryIssue(DTO):
    topic: Literal[
        "BUSINESS_PURPOSE", "POLICY_APPLICABILITY", "EXCEPTION_REASON", "FACT_POLICY_CONFLICT",
        "EVIDENCE_SUFFICIENCY", "DUPLICATE_SPLIT_CATEGORY", "AUTHORIZATION", "UNRESOLVED_QUESTIONS",
    ]
    basis: Literal[
        "BUSINESS", "POLICY", "EXCEPTION", "HOTEL", "ALLOWANCE", "ENTERTAINMENT",
        "INVOICE", "AUTHORIZATION", "DUPLICATE",
    ]
    severity: Literal["LOW", "MEDIUM", "HIGH"]
    policy_ref_ids: list[str] = Field(min_length=1, max_length=12)
    fact_refs: list[str] = Field(min_length=1, max_length=16)
    explanation: str = Field(min_length=1, max_length=400)
    finance_check: str = Field(min_length=1, max_length=300)


class AdvisoryOutput(DTO):
    schema_version: Literal["p4b.advisory.1"] = "p4b.advisory.1"
    risk_level: Literal["LOW", "MEDIUM", "HIGH", "UNDETERMINED"]
    recommendation: Literal[
        "RECOMMEND_APPROVAL", "RECOMMEND_REJECTION", "REQUEST_EVIDENCE", "ESCALATE", "UNDETERMINED",
    ]
    issues: list[AdvisoryIssue] = Field(max_length=16)
    missing_evidence: list[str] = Field(max_length=24)
    policy_ref_ids: list[str] = Field(min_length=1, max_length=16)
    fact_refs: list[str] = Field(min_length=1, max_length=20)
    requires_human_review: Literal[True]


class ManualAdvisoryReview(DTO):
    report_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_revision: int = Field(ge=0)
    outcome: Literal["CLEAR", "RETURN_FOR_EVIDENCE", "REJECT", "ESCALATE"]
    exception_required: bool = Field(strict=True)
    unreliable_advice_acknowledged: Literal[True]
    source_reference: str = Field(min_length=1, max_length=1000)
    basis: str = Field(min_length=1, max_length=2000)
    idempotency_key: IdempotencyKey

    @model_validator(mode="after")
    def valid_review(self):
        if not self.source_reference.strip() or not self.basis.strip():
            raise ValueError("Independent manual evidence and basis required")
        if self.outcome == "CLEAR" and self.exception_required:
            raise ValueError("Exception payment is not authorized by this demo")
        return self
