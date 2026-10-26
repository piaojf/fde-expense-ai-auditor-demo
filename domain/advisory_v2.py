"""Explicit v2 findings; model prose never becomes a verified financial fact."""

from typing import Literal

from pydantic import Field

from domain.advisory import AdvisoryOutput
from domain.schemas import DTO

FindingCode = Literal[
    "GENERAL_FINANCE_CHECK", "VERIFIED_STAY", "POLICY_EFFECTIVE", "PRIOR_APPROVAL_VERIFIED",
    "AMOUNT_WITHIN_LIMIT", "BUSINESS_PURPOSE_UNRESOLVED", "MISSING_REQUIRED_EVIDENCE",
    "RULE_FAILURE", "FACT_CONFLICT",
]


class AdvisoryFindingV2(DTO):
    code: FindingCode
    classification: Literal["INFO", "WARNING", "BLOCKER"]
    resolution: Literal["NOT_APPLICABLE", "UNRESOLVED"]
    policy_ref_ids: list[str] = Field(min_length=1, max_length=12)
    fact_refs: list[str] = Field(min_length=1, max_length=16)
    rule_refs: list[str] = Field(max_length=16)
    fact_key: str | None = Field(default=None, max_length=64)
    source_quote: str | None = Field(default=None, max_length=300)
    # A model question is explicitly unverified, never a formal policy/fact explanation.
    question: str | None = Field(default=None, min_length=1, max_length=300)


class AdvisoryOutputV2(AdvisoryOutput):
    schema_version: Literal["p4b.advisory.2"] = "p4b.advisory.2"
    issues: list[AdvisoryFindingV2] = Field(max_length=16)


def parse_advisory(raw):
    version = raw.get("schema_version", "p4b.advisory.1") if isinstance(raw, dict) else None
    if version == "p4b.advisory.4":
        from domain.advisory_v4 import AdvisoryOutputV4

        return AdvisoryOutputV4.model_validate(raw, strict=True)
    if version == "p4b.advisory.3":
        from domain.advisory_v3 import AdvisoryOutputV3

        return AdvisoryOutputV3.model_validate(raw, strict=True)
    dto = AdvisoryOutputV2 if version == "p4b.advisory.2" else AdvisoryOutput
    return dto.model_validate(raw, strict=True)
