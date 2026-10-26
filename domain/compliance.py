"""P4B v1 contracts, independent of every frozen approval/LLM DTO."""

from datetime import date
from typing import Any, Literal

from pydantic import ConfigDict, Field, PrivateAttr, field_validator, model_validator

from domain.compliance_v2 import CompactSemanticAnalysis
from domain.enterprise import PortalInvoice
from domain.positive_evidence import PositiveEvidenceContract
from domain.schemas import DTO, IdempotencyKey, Money

FindingStatus = Literal[
    "COMPLIANT", "NON_COMPLIANT", "INSUFFICIENT_EVIDENCE", "POLICY_CONFLICT", "UNSUPPORTED"
]


class SourceEvidence(DTO):
    document_id: str
    document_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_version_id: str
    section: str
    clause_id: str
    chunk_id: str
    source_text: str
    source_position: dict[str, Any]
    effective_from: date
    effective_to: date | None


class SemanticFinding(DTO):
    finding_id: str = Field(min_length=1, max_length=80)
    issue_type: str = Field(min_length=1, max_length=100)
    conclusion: FindingStatus
    evidence_refs: list[str] = Field(min_length=1, max_length=16)
    factual_basis: list[str] = Field(min_length=1, max_length=20)
    uncertainty: str = Field(max_length=2000)
    explanation: str = Field(min_length=1, max_length=2000)


class SemanticAnalysis(DTO):
    semantic_findings: list[SemanticFinding] = Field(min_length=1, max_length=24)
    missing_evidence: list[str] = Field(max_length=24)
    conflicts: list[str] = Field(max_length=24)
    exceptions: list[str] = Field(max_length=24)
    suggested_risk: Literal["LOW", "MEDIUM", "HIGH"]
    recommended_action: Literal["CHECK_PASSED", "HUMAN_REVIEW", "REQUEST_EVIDENCE"]
    explanation: str = Field(min_length=1, max_length=4000)
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=128)


class ComplianceRequest(DTO):
    _provider_evidence: dict = PrivateAttr(default_factory=dict)
    _evidence_selector: str | None = PrivateAttr(default=None)
    _evidence_selector_attempts: tuple[str, ...] = PrivateAttr(default=())
    claim_id: str
    policy_version_id: str
    facts: dict[str, Any]
    rule_results: list[dict]
    evidence: list[SourceEvidence]


class ComplianceAnalysis(DTO):
    schema_version: Literal["p4b.1"] = "p4b.1"
    claim_id: str
    run_id: str
    policy_version_id: str | None
    fact_revision: int
    expense_type: str
    policy_title: str | None
    trusted_facts: dict
    semantic_assessment: SemanticAnalysis | CompactSemanticAnalysis | None
    rule_results: list[dict]
    semantic_findings: list[SemanticFinding]
    supporting_evidence: list[SourceEvidence]
    missing_evidence: list[str]
    conflicts: list[str]
    exceptions: list[str]
    suggested_risk: Literal["LOW", "MEDIUM", "HIGH"]
    recommended_action: Literal["CHECK_PASSED", "HUMAN_REVIEW", "REQUEST_EVIDENCE", "REJECT"]
    compliance_status: Literal[
        "CHECK_PASSED", "NON_COMPLIANT", "INSUFFICIENT_EVIDENCE", "POLICY_CONFLICT", "UNKNOWN"
    ]
    explanation: str
    provider: str
    model: str
    analysis_status: Literal["SUCCEEDED", "UNSUPPORTED", "FAILED", "NO_POLICY"]
    retrieval_method: Literal["LOCAL_TEXT_ORIGINAL_DOCX"] = "LOCAL_TEXT_ORIGINAL_DOCX"
    provider_calls: list[dict] = Field(default_factory=list)
    positive_evidence: PositiveEvidenceContract | None = None
    semantic_coverage: dict = Field(default_factory=dict)
    citation_bindings: list[dict] = Field(default_factory=list)
    semantic_validation: dict = Field(default_factory=dict)
    summary_bindings: dict = Field(default_factory=dict)


class VerifiedFacts(DTO):
    model_config = ConfigDict(extra="forbid", strict=True)
    business_related: bool | None = None
    invoice_verified: bool | None = None
    prior_approval: bool | None = None
    customer_identified: bool | None = None
    duplicate_clear: bool | None = None
    participants: int | None = Field(default=None, strict=True, ge=1, le=1000)
    entertainment_category: Literal["GENERAL", "MAJOR", "REFRESHMENT", "GIFT"] | None = None
    full_days: int | None = Field(default=None, strict=True, ge=1, le=366)
    lunches: int | None = Field(default=None, strict=True, ge=0, le=366)
    dinners: int | None = Field(default=None, strict=True, ge=0, le=366)
    cancellation_notice: bool | None = None
    itinerary_verified: bool | None = None
    exception_authorized: bool | None = None

    @model_validator(mode="after")
    def meals_fit_verified_days(self):
        if self.full_days is not None and any(
            count is not None and count > self.full_days for count in (self.lunches, self.dinners)
        ):
            raise ValueError("Verified meals exceed verified full days")
        return self


class VerifyFacts(DTO):
    facts: VerifiedFacts
    expected_revision: int = Field(ge=0)
    source_reference: str = Field(min_length=1, max_length=1000)
    basis: str = Field(min_length=1, max_length=2000)
    confirmed: Literal[True]
    idempotency_key: IdempotencyKey

    @field_validator("source_reference", "basis")
    @classmethod
    def visible(cls, value):
        if not value.strip():
            raise ValueError("Human verification requires actual source and basis")
        return value.strip()


class ConfirmScope(DTO):
    extraction_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: Literal[True]
    reason: str = Field(min_length=1, max_length=2000)
    idempotency_key: IdempotencyKey


class HybridSubmission(DTO):
    expense_type: Literal["ALLOWANCE", "ENTERTAINMENT"]
    city: str = Field(min_length=1, max_length=128)
    amount: Money = Field(gt=0)
    incurred_on: date
    trip_start: date
    trip_end: date
    purpose: str = Field(min_length=1, max_length=2000)
    notes: str = Field(default="", max_length=2000)
    seller: str = Field(min_length=1, max_length=200)
    invoice: PortalInvoice | None = None
    candidate_full_days: int | None = Field(default=None, strict=True, ge=1, le=366)
    candidate_lunches: int | None = Field(default=None, strict=True, ge=0, le=366)
    candidate_dinners: int | None = Field(default=None, strict=True, ge=0, le=366)
    idempotency_key: IdempotencyKey

    @model_validator(mode="after")
    def dates(self):
        if not self.trip_start <= self.incurred_on <= self.trip_end:
            raise ValueError("Expense must belong to the declared trip")
        if (self.trip_end - self.trip_start).days > 365:
            raise ValueError("Trip exceeds supported full-day demo scope")
        return self


class HybridInvoice(PortalInvoice):
    amount: Money
    seller: str
    incurred_on: date
    token: str
    idempotency_key: IdempotencyKey


class Reaudit(DTO):
    expected_revision: int = Field(ge=0)
    idempotency_key: IdempotencyKey
