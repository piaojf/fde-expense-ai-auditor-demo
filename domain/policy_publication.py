"""Independent v1.4 governance requests; no changes to frozen intake DTOs."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator

from domain.policies import LocalCapability


class RuleConfirmation(LocalCapability):
    actor: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)
    confirmed: Literal[True]
    rule_type: Literal["HOTEL_LIMIT_PER_NIGHT"] = "HOTEL_LIMIT_PER_NIGHT"
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2, allow_inf_nan=False)
    currency: Literal["CNY"] = "CNY"
    clause_start: int = Field(ge=0)
    clause_end: int = Field(gt=0)
    expected_revision: int = Field(ge=0)

    @field_validator("actor", "reason")
    @classmethod
    def audit_label(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("Audit label must contain visible text")
        return value.strip()

    @field_validator("amount", mode="before")
    @classmethod
    def decimal_string(cls, value):
        if not isinstance(value, str):
            raise ValueError("Money must be supplied as a decimal string")
        return value


class PublicationActor(LocalCapability):
    actor: str = Field(min_length=1, max_length=128)

    @field_validator("actor")
    @classmethod
    def visible_actor(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("Audit actor must contain visible text")
        return value.strip()


class PublicationPreview(LocalCapability):
    predecessor_id: str | None = Field(default=None, max_length=128)


class PublishPolicy(PublicationPreview, PublicationActor):
    actor: str = Field(min_length=1, max_length=128)
    confirmed: Literal[True]
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=128)


class LifecyclePreview(LocalCapability):
    kind: Literal["RETIRE", "ROLLBACK"]
    effective_on: date
    restore_id: str | None = Field(default=None, max_length=128)


class ChangeLifecycle(LifecyclePreview, PublicationActor):
    actor: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)
    confirmed: Literal[True]
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=128)
