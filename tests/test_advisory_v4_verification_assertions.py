"""Negated verification disclaimers must not hide affirmative claims."""

from copy import deepcopy
from datetime import date

import pytest

from domain.advisory_v4 import AdvisoryOutputV4
from domain.compliance import ComplianceRequest, SourceEvidence
from integrations.llm.advisory_v4 import validate_advisory_v4

DISCLAIMER = "本建议不构成住宿真实性、发票验真或事前审批已核实的结论。"


@pytest.fixture
def advisory(monkeypatch):
    # This unit tests assertion validation with wholly synthetic evidence.
    # Production source selection is a separate boundary and is not changed.
    evidence = SourceEvidence(
        document_id="synthetic-document", document_sha256="a" * 64,
        policy_version_id="synthetic-policy", section="Synthetic invoice guidance",
        clause_id="第三十八条", chunk_id="synthetic-clause",
        source_text="合成测试条款：保留凭据供财务核查。",
        source_position={"part": "word/document.xml", "body_index": 1},
        effective_from=date(2026, 1, 1), effective_to=None,
    )
    request = ComplianceRequest(
        claim_id="synthetic-claim", policy_version_id="synthetic-policy",
        facts={"candidate": {"incurred_on": "2026-06-01"},
               "trusted": {"expense_type": "ALLOWANCE"}},
        rule_results=[], evidence=[evidence],
    )
    manifest = {"evidence_snapshot_id": "b" * 64,
                "entries": [{"short_id": "E01", "original_chunk_id": evidence.chunk_id}]}
    monkeypatch.setattr("integrations.llm.advisory_v4.selector_manifest", lambda _: manifest)
    data = dict(
        evidence_snapshot_id=manifest["evidence_snapshot_id"], risk_level="MEDIUM",
        recommendation="ESCALATE", summary="最终由财务独立判断。",
        policy_ref_keys=["E01"], fact_refs=["candidate.incurred_on"], requires_human_review=True,
        issues=[dict(topic="UNRESOLVED_QUESTIONS", basis="INVOICE", severity="MEDIUM",
                     explanation="请财务结合原始材料判断。", finance_check="由财务决定。",
                     policy_ref_keys=["E01"], fact_refs=["candidate.incurred_on"])],
    )
    return request, data


def validate(advisory, text, field="summary"):
    request, original = advisory
    data = deepcopy(original)
    target = data if field == "summary" else data["issues"][0]
    target[field] = text
    before = deepcopy(data)
    _, errors = validate_advisory_v4(AdvisoryOutputV4.model_validate(data), request)
    assert data == before
    return errors


@pytest.mark.parametrize("field", ["summary", "explanation", "finance_check"])
@pytest.mark.parametrize("text", [
    DISCLAIMER,
    "本建议不代表住宿事实已核实的结论。",
    "本建议不意味着发票已验真的证明。",
    "金额比较不构成已入账或已付款的证明。",
])
def test_explicit_disclaimer_is_accepted(advisory, field, text):
    assert validate(advisory, text, field) == []


@pytest.mark.parametrize("field", ["summary", "explanation", "finance_check"])
@pytest.mark.parametrize("text", [
    "住宿事实已核实，发票已验真。",
    DISCLAIMER + "但发票已验真。",
    DISCLAIMER + "已入账，已付款。",
    "本建议不构成住宿真实性的结论，事前审批已核实。",
    "本建议不构成住宿真实性、但发票已验真的结论。",
    "并非本建议不构成住宿事实已核实的结论。",
    "不能说本建议不代表发票已验真的结论。",
])
def test_unsupported_or_ambiguous_assertion_stays_rejected(advisory, field, text):
    assert any(e["code"] == "UNSUPPORTED_VERIFIED_ASSERTION"
               for e in validate(advisory, text, field))


def test_disclaimer_cannot_span_model_fields(advisory):
    request, data = advisory
    data["summary"] = "本建议不构成住宿真实性、"
    data["issues"][0]["explanation"] = "事前审批已核实的结论。"
    _, errors = validate_advisory_v4(AdvisoryOutputV4.model_validate(data), request)
    assert any(e["code"] == "UNSUPPORTED_VERIFIED_ASSERTION" for e in errors)
