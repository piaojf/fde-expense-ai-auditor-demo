"""Independent v1.5 contracts for authenticated enterprise workflows."""

from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_serializer

from domain.enterprise_travel import TravelDetails
from domain.invoices import ConfirmedFields
from domain.schemas import DTO, IdempotencyKey
from domain.submission import SubmissionFields

Role = Literal["EMPLOYEE", "FINANCE", "ADMIN"]


class LoginRequest(DTO):
    company_id: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=100)
    password: SecretStr = Field(min_length=1, max_length=128)
    captcha_id: str = Field(default="", max_length=100)
    captcha_code: SecretStr = Field(default_factory=lambda: SecretStr(""), max_length=16)


class CreateUser(DTO):
    username: str = Field(pattern=r"^[a-zA-Z0-9_.-]{3,100}$")
    password: SecretStr = Field(min_length=12, max_length=128)
    name: str = Field(min_length=1, max_length=100)
    department: str = Field(min_length=1, max_length=128)
    position: str = Field(min_length=1, max_length=128)
    role: Role

    @field_validator("name", "department", "position")
    @classmethod
    def visible(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("Visible text required")
        return value.strip()


class SetUserEnabled(DTO):
    enabled: bool


class PortalInvoice(DTO):
    draft_id: UUID
    attachment_id: UUID
    confirmed: Literal[True]
    confirmed_fields: ConfirmedFields
    discrepancy_reason: str = Field(default="", max_length=2000)


class PortalSubmission(DTO):
    # Identity is checked against the session and replaced with server-owned values.
    fields: SubmissionFields
    travel_details: TravelDetails | None = None
    invoice: PortalInvoice | None = None
    idempotency_key: IdempotencyKey


class ResubmitRequest(PortalSubmission):
    expected_revision: int = Field(ge=0)


class FinanceAction(DTO):
    action: Literal["APPROVE", "REJECT", "RETURN"]
    reason: str = Field(min_length=1, max_length=2000)
    expected_revision: int = Field(ge=0)
    idempotency_key: IdempotencyKey
    advisory_report_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_serializer(mode="wrap")
    def preserve_legacy_payload(self, handler):
        result = handler(self)
        if self.advisory_report_digest is None:
            result.pop("advisory_report_digest", None)
        return result

    @field_validator("reason")
    @classmethod
    def visible_reason(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("A visible reason is required")
        return value.strip()


class RetryRequest(DTO):
    expected_revision: int = Field(ge=0)
    idempotency_key: IdempotencyKey
