"""Independent invoice DTOs. OCR is evidence, never an approval authority."""

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator

from domain.schemas import DTO, Money
from domain.submission import CreateClaimRequest, SubmissionFields, SubmissionResponse


class Capability(DTO):
    token: UUID


class DraftRequest(Capability):
    draft_id: UUID


class RecognizedField(DTO):
    value: str | None = None
    status: Literal["RECOGNIZED", "UNKNOWN"] = "UNKNOWN"
    confidence: float | None = None
    page: int | None = None
    line: int | None = None
    source: str | None = None


class OCRResult(DTO):
    provider: str
    engine_version: str
    methods: list[str]
    fields: dict[str, RecognizedField]
    # Sensitive evidence is stored in SQLite, never ordinary logs.
    lines: list[dict]


class ConfirmedFields(DTO):
    invoice_number: Annotated[str, Field(max_length=64)] = ""
    invoice_code: Annotated[str, Field(max_length=64)] = ""
    issue_date: date | None = None
    seller_name: Annotated[str, Field(max_length=200)] = ""
    buyer_name: Annotated[str, Field(max_length=200)] = ""
    total_amount: Money | None = Field(default=None, gt=0)
    tax_amount: Money | None = Field(default=None, ge=0)

    @field_validator("invoice_number", "invoice_code", "seller_name", "buyer_name")
    @classmethod
    def trim(cls, value):
        return value.strip()


class CheckRequest(Capability):
    fields: SubmissionFields
    confirmed_fields: ConfirmedFields


class InvoiceClaimRequest(CreateClaimRequest, Capability):
    draft_id: UUID
    attachment_id: UUID
    confirmed: Literal[True]
    confirmed_fields: ConfirmedFields
    discrepancy_reason: Annotated[str, Field(max_length=2000)] = ""


class InvoiceResponse(SubmissionResponse):
    schema_version: str = "1.2.0"
    invoice_evidence: dict | None = None
