"""Versioned compact model contract. No legacy missing-field repair or authority."""

from typing import Annotated, Literal

from pydantic import Field, field_validator

from domain.schemas import DTO

Topic = Literal[
    "BUSINESS_PURPOSE",
    "POLICY_APPLICABILITY",
    "EXCEPTION_REASON",
    "FACT_POLICY_CONFLICT",
    "EVIDENCE_SUFFICIENCY",
    "DUPLICATE_SPLIT_CATEGORY",
    "AUTHORIZATION",
    "UNRESOLVED_QUESTIONS",
]
CheckState = Literal["CLEAR", "ISSUE", "UNKNOWN"]
Basis = Literal[
    "BUSINESS",
    "POLICY",
    "EXCEPTION",
    "HOTEL",
    "ALLOWANCE",
    "ENTERTAINMENT",
    "INVOICE",
    "AUTHORIZATION",
    "DUPLICATE",
]


class SemanticChecks(DTO):
    BUSINESS_PURPOSE: CheckState
    POLICY_APPLICABILITY: CheckState
    EXCEPTION_REASON: CheckState
    FACT_POLICY_CONFLICT: CheckState
    EVIDENCE_SUFFICIENCY: CheckState
    DUPLICATE_SPLIT_CATEGORY: CheckState
    AUTHORIZATION: CheckState
    UNRESOLVED_QUESTIONS: CheckState


def visible(value):
    if not value.strip():
        raise ValueError("EMPTY_SEMANTIC_EXPLANATION")
    return value


class CompactFinding(DTO):
    topic: Topic
    basis: Basis
    conclusion: Literal["NON_COMPLIANT", "INSUFFICIENT_EVIDENCE", "POLICY_CONFLICT", "UNSUPPORTED"]
    evidence_refs: list[str] = Field(min_length=1, max_length=4)
    factual_basis: list[str] = Field(min_length=1, max_length=6)
    explanation: str = Field(min_length=1, max_length=600)
    required_evidence: list[str] = Field(max_length=8)

    _visible_explanation = field_validator("explanation")(visible)


class CompactOutput(DTO):
    # Required discriminator: old invalid answers cannot become valid v2 answers.
    schema_version: Literal["p4b.semantic.2"]
    checks: SemanticChecks
    findings: list[CompactFinding] = Field(max_length=12)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)
    factual_basis: list[str] = Field(min_length=1, max_length=8)
    exceptions: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(max_length=4)
    suggested_risk: Literal["LOW", "MEDIUM", "HIGH"]
    recommended_action: Literal["CHECK_PASSED", "HUMAN_REVIEW", "REQUEST_EVIDENCE"]
    explanation: str = Field(min_length=1, max_length=600)

    _visible_explanation = field_validator("explanation")(visible)

    @field_validator("exceptions")
    @classmethod
    def visible_exceptions(cls, values):
        for value in values:
            visible(value)
        return values


class CompactSemanticAnalysis(CompactOutput):
    # Server provenance from a verified transport envelope, absent from model schema.
    provider: str
    model: str

    @property
    def semantic_findings(self):
        from domain.compliance import SemanticFinding

        return [
            SemanticFinding(
                finding_id=f"F{i + 1}",
                issue_type=f.topic,
                conclusion=f.conclusion,
                evidence_refs=f.evidence_refs,
                factual_basis=f.factual_basis,
                uncertainty=f.explanation,
                explanation=f.explanation,
            )
            for i, f in enumerate(self.findings)
        ]

    @property
    def missing_evidence(self):
        return list(dict.fromkeys(k for f in self.findings for k in f.required_evidence))

    @property
    def conflicts(self):
        # Business-fact contradictions are findings, never policy-version conflicts.
        return [f.explanation for f in self.findings if f.conclusion == "POLICY_CONFLICT"]
