"""Independent UI-P3A intake and preview DTOs; no rule publication fields."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class LocalCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr


class PolicyMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    legal_entity: str = Field(min_length=1, max_length=128)
    region: str = Field(min_length=1, max_length=128)
    expense_type: Literal["HOTEL", "MEAL", "TRANSPORT", "OTHER"]
    version: str = Field(min_length=1, max_length=128)
    effective_from: date
    effective_to: date | None = None

    @field_validator("title", "legal_entity", "region", "version")
    @classmethod
    def known_value(cls, value):
        if value.upper() == "UNKNOWN" or any(ord(c) < 32 for c in value):
            raise ValueError("Administrator must confirm a known value")
        return value

    @model_validator(mode="after")
    def interval(self):
        if self.effective_to and self.effective_to <= self.effective_from:
            raise ValueError("Effective interval must be [from, to)")
        return self


class ConfirmPolicy(LocalCapability):
    metadata: PolicyMetadata
    confirmed: Literal[True]


class IngestPolicy(LocalCapability):
    remote_authorized: bool = False


class PreviewPolicy(LocalCapability):
    query: str = Field(min_length=1, max_length=2000)
    legal_entity: str = Field(min_length=1, max_length=128)
    region: str = Field(min_length=1, max_length=128)
    expense_type: Literal["HOTEL", "MEAL", "TRANSPORT", "OTHER"]
    version: str = Field(min_length=1, max_length=128)
    incurred_on: date
    remote_authorized: bool = False
