"""Frozen public DTOs. docs/api-contract.md is the source of truth."""

from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema


def _money(value: Any) -> Decimal:
    if not isinstance(value, (str, Decimal)):
        raise ValueError("Money must be a decimal string, never float")
    try:
        number = Decimal(value)
        if not number.is_finite() or number < 0 or number > Decimal("9999999999.99"):
            raise ValueError("Money outside supported range")
        if number != number.quantize(Decimal("0.01")):
            raise ValueError("Money must have at most two decimal places")
        return number.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError("Invalid decimal amount") from exc


Money = Annotated[
    Decimal,
    BeforeValidator(_money),
    PlainSerializer(lambda value: format(value, ".2f"), return_type=str),
    WithJsonSchema({"type": "string", "pattern": r"^\d{1,10}\.\d{2}$"}),
]
Identifier = Annotated[str, Field(min_length=1, max_length=128)]
IdempotencyKey = Identifier


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaimStatus(StrEnum):
    NEW = "NEW"
    AUDITING = "AUDITING"
    PENDING_HUMAN = "PENDING_HUMAN"
    PENDING_ERP = "PENDING_ERP"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Decision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class Outcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class Severity(StrEnum):
    INFO = "INFO"
    REVIEW = "REVIEW"
    BLOCK = "BLOCK"


class ERPStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class RunMode(StrEnum):
    LIVE = "LIVE"
    SHADOW = "SHADOW"


class Employee(DTO):
    employee_id: Identifier
    name: Annotated[str, Field(min_length=1, max_length=200)]
    department: Identifier
    legal_entity: Identifier


class Expense(DTO):
    category: Literal["HOTEL", "TRANSPORT", "MEAL", "OTHER"]
    city: Identifier
    amount: Money = Field(gt=0)
    currency: Literal["CNY"] = "CNY"
    incurred_on: date
    nights: int = Field(default=1, ge=1)


class Document(DTO):
    document_id: Identifier
    file_name: Annotated[str, Field(min_length=1, max_length=255)]
    content_type: Identifier
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    parse_status: Literal["UNPARSED", "PARSED", "FAILED"] = "UNPARSED"


class HardGateResult(DTO):
    gate_id: Identifier
    outcome: Outcome
    reason: str


class RuleResult(DTO):
    rule_id: Identifier
    outcome: Outcome
    severity: Severity
    reason: str
    actual: Money | None = None
    limit: Money | None = None
    policy_version_id: Identifier | None = None


class PolicyEvidence(DTO):
    policy_version_id: Identifier
    policy_key: Identifier
    version: Identifier
    effective_from: date
    effective_to: date | None = None
    chunk_id: Identifier
    source_uri: str
    quote: Annotated[str, Field(min_length=1)]
    relevance_score: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def valid_interval(self):
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("Policy validity uses a non-empty half-open interval")
        return self


class LLMAnalysis(DTO):
    summary: str
    anomalies: list[str] = Field(default_factory=list)
    suggested_risk: RiskLevel
    confidence: float = Field(ge=0, le=1)
    provider: Identifier
    model: Identifier


class HumanReview(DTO):
    decision: Literal["APPROVE", "REJECT"]
    reviewer_id: Identifier
    comment: Annotated[str, Field(min_length=1, max_length=2000)]
    reviewed_at: AwareDatetime


class AgentError(DTO):
    node: Identifier
    code: Identifier
    message: str
    retryable: bool
    occurred_at: AwareDatetime


class DecisionResult(DTO):
    schema_version: Literal["1.0.0"] = "1.0.0"
    claim_id: Identifier
    run_id: UUID
    decision: Decision
    risk_level: RiskLevel
    hard_gate_passed: bool
    rule_results: list[RuleResult]
    policy_evidence: list[PolicyEvidence]
    reason: Annotated[str, Field(min_length=1)]
    confidence: float = Field(ge=0, le=1)
    requires_human: bool
    decided_at: AwareDatetime | None
    # Opt-in enterprise authority receipt; keep the frozen v1 OpenAPI unchanged.
    manual_approval: SkipJsonSchema[Any] = None

    @field_validator("manual_approval", mode="before")
    @classmethod
    def parse_manual_authority(cls, value) -> HumanReview | None:
        # Validate internally without registering an enterprise-only DTO in v1 OpenAPI.
        return None if value is None else HumanReview.model_validate(value)

    @model_serializer(mode="wrap")
    def preserve_legacy_payload(self, handler):
        result = handler(self)
        if self.manual_approval is None:
            result.pop("manual_approval", None)
        return result

    @model_validator(mode="after")
    def enforce_authority(self):
        human = self.decision == Decision.HUMAN_REVIEW
        if self.requires_human != human:
            raise ValueError("requires_human must match HUMAN_REVIEW")
        if human == (self.decided_at is not None):
            raise ValueError("Only final decisions have decided_at")
        if not self.hard_gate_passed and (
            self.decision != Decision.REJECT or self.risk_level != RiskLevel.HIGH
        ):
            raise ValueError("Failed hard gate requires HIGH / REJECT")
        if self.risk_level == RiskLevel.HIGH and self.decision != Decision.REJECT:
            raise ValueError("HIGH requires REJECT")
        if human and self.risk_level != RiskLevel.MEDIUM:
            raise ValueError("Human review requires MEDIUM")
        blocked = any(
            r.severity == Severity.BLOCK and r.outcome == Outcome.FAIL for r in self.rule_results
        )
        if blocked and self.decision != Decision.REJECT:
            raise ValueError("BLOCK / FAIL cannot be overridden")
        if self.manual_approval is not None and (
            self.decision != Decision.APPROVE
            or self.manual_approval.decision != "APPROVE"
            or self.manual_approval.comment != self.reason
            or not self.manual_approval.comment.strip()
            or self.manual_approval.reviewed_at != self.decided_at
            or any(r.outcome == Outcome.FAIL for r in self.rule_results)
        ):
            raise ValueError("Manual approval must match the final human decision")
        if self.decision == Decision.APPROVE:
            uncertain = any(
                r.outcome == Outcome.UNKNOWN
                or (r.outcome == Outcome.FAIL and r.severity == Severity.REVIEW)
                for r in self.rule_results
            )
            if (self.risk_level != RiskLevel.LOW
                    or (uncertain and self.manual_approval is None)
                    or not self.policy_evidence):
                raise ValueError("Approval requires LOW and complete authoritative evidence")
        return self


class ClaimSummary(DTO):
    claim_id: Identifier
    employee: Employee
    expense: Expense
    status: ClaimStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime
    decision_result: DecisionResult | None = None


class ClaimDetail(ClaimSummary):
    documents: list[Document] = Field(default_factory=list)
    erp_status: ERPStatus = ERPStatus.NOT_STARTED
    active_run_id: UUID | None = None
    data_source: Literal["mock"] = "mock"


class ClaimList(DTO):
    items: list[ClaimSummary]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
    data_source: Literal["mock"] = "mock"


class AuditRequest(DTO):
    idempotency_key: IdempotencyKey
    mode: RunMode = RunMode.LIVE


class HumanReviewRequest(DTO):
    run_id: UUID
    expected_revision: int = Field(ge=0)
    decision: Literal["APPROVE", "REJECT"]
    reviewer_id: Identifier
    comment: Annotated[str, Field(min_length=1, max_length=2000)]
    idempotency_key: IdempotencyKey


class ResumeRequest(DTO):
    run_id: UUID
    expected_revision: int = Field(ge=0)
    idempotency_key: IdempotencyKey


class OperationResponse(DTO):
    claim_id: Identifier
    run_id: UUID
    status: ClaimStatus
    revision: int = Field(ge=0)
    decision_result: DecisionResult | None


class AuditEvent(DTO):
    event_id: UUID
    claim_id: Identifier
    run_id: UUID
    event_type: Identifier
    actor: Identifier
    occurred_at: AwareDatetime
    payload: dict[str, Any]


class AuditLogResponse(DTO):
    claim_id: Identifier
    items: list[AuditEvent]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
    data_source: Literal["mock"] = "mock"


class DashboardMetrics(DTO):
    total_claims: int = Field(ge=0)
    by_status: dict[ClaimStatus, int]
    by_decision: dict[Decision, int]
    pending_human: int = Field(ge=0)
    pending_erp: int = Field(ge=0)
    data_source: Literal["mock"] = "mock"


class HealthResponse(DTO):
    status: Literal["ok"] = "ok"
    service: Literal["fde-expense-ai-auditor"] = "fde-expense-ai-auditor"
    version: Literal["0.1.0"] = "0.1.0"
    milestone: Literal["M0"] = "M0"
    data_source: Literal["mock"] = "mock"
    database: Literal["not_checked"] = "not_checked"


class ErrorDetail(DTO):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(DTO):
    error: ErrorDetail
    request_id: UUID
