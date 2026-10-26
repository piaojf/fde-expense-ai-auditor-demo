"""Strict synthetic evaluation input; never part of the public API schema."""

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from domain.schemas import DTO, ClaimDetail, Decision, RiskLevel

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "evals/datasets/m5a_golden.json"


class PolicyLabel(DTO):
    policy_version_id: str | None
    version: str | None

    @model_validator(mode="after")
    def paired(self):
        if (self.policy_version_id is None) != (self.version is None):
            raise ValueError("Policy ID and version must both be present or absent")
        return self


class GoldenCase(DTO):
    case_id: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1)
    data_source: Literal["Synthetic"]
    claim: ClaimDetail
    invoices: list[dict]
    applicable_policy: PolicyLabel
    expected_decision: Decision
    expected_risk: RiskLevel
    requires_human: bool
    expected_policy_evidence: list[str]
    policy_basis: str = Field(min_length=1)
    label_reason: str = Field(min_length=1)
    label_source: Literal["RULE_DERIVED", "HUMAN_REVIEW_REQUIRED"]
    risk_category: str = Field(min_length=1)
    hard_block: bool
    llm_fault: Literal[
        "NONE",
        "HOSTILE_LOW",
        "INVALID_OUTPUT",
        "INVALID_RESPONSE",
        "TIMEOUT_ALWAYS",
        "TIMEOUT_ONCE",
    ] = "NONE"
    retrieval_fault: Literal["NONE", "TIMEOUT_ALWAYS", "EMPTY"] = "NONE"
    probe: Literal["HISTORICAL_SIMILARITY", "INJECTED_QUOTE"] | None = None
    catalog_mode: Literal["DEFAULT", "DRAFT_ONLY", "RETIRED_ONLY"] = "DEFAULT"

    @model_validator(mode="after")
    def consistent(self):
        if self.case_id != self.claim.claim_id:
            raise ValueError("Case and claim IDs must match")
        expected = {"LOW": "APPROVE", "MEDIUM": "HUMAN_REVIEW", "HIGH": "REJECT"}
        if expected[self.expected_risk] != self.expected_decision:
            raise ValueError("Risk and decision labels conflict")
        if self.requires_human != (self.expected_decision == Decision.HUMAN_REVIEW):
            raise ValueError("Human label conflicts with decision")
        if self.hard_block and self.expected_decision != Decision.REJECT:
            raise ValueError("Hard block requires rejection")
        ids = self.expected_policy_evidence
        if len(ids) != len(set(ids)) or any(
            p != self.applicable_policy.policy_version_id for p in ids
        ):
            raise ValueError("Evidence labels must match applicable policy")
        return self


class GoldenDataset(DTO):
    dataset_id: str = Field(min_length=1)
    data_source: Literal["Synthetic"]
    label_method: str = Field(min_length=1)
    policy_fixture: str
    cases: list[GoldenCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique(self):
        if len({c.case_id for c in self.cases}) != len(self.cases):
            raise ValueError("Duplicate case IDs")
        # Reuse the existing versioned seed; custom datasets may reference it only.
        if self.policy_fixture != "rag/seed/policies-m4a.json":
            raise ValueError("Unsupported policy fixture")
        return self


def load_dataset(path=DEFAULT_DATASET) -> GoldenDataset:
    return GoldenDataset.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
