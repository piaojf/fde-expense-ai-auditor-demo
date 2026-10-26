"""FIX3 exact private replay and independently authored zero-HTTP semantic cases."""

import hashlib
import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest
from p4b_fix2_support import authored_review, finding, positive_output, reference
from test_p4b_fix2 import compact_request, normalized

from domain.compliance import ComplianceRequest
from domain.positive_evidence import PositiveEvidenceContract
from integrations.llm.compliance_v2 import (
    AttributionError,
    CompactComplianceDeepSeekProvider,
    validate_compact,
)
from rag.retrieval.compliance import original_chunks

LOCAL = Path("output/p4b/fix3")


def supported_output(req):
    """Independent synthetic judgments: explicit policy, fact and rule references."""
    data = positive_output(req)
    data["evidence_refs"].extend([reference(req, "第四十一条"), reference(req, "第八条")])
    data["factual_basis"].extend(["positive.duplicate_clear", "positive.category_mapping"])
    data["explanation"] = "本次核验范围内未发现重复报销，规则金额检查通过；财务仍须单独批准。"
    return data


def test_independent_correct_fact_policy_rule_references_pass():
    req = compact_request("HOTEL")
    data = supported_output(req)
    result = validate_compact(normalized(data), req)
    assert result.recommended_action == "CHECK_PASSED"
    assert result.model_dump(exclude={"provider", "model"}) == data


@pytest.mark.skipif(not (LOCAL / "replay").is_dir(), reason="Private replay evidence kept local")
def test_exact_historical_failure_stays_failed_and_has_precise_field_and_scope():
    manifest = json.loads((LOCAL / "replay-manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest.items():
        p = LOCAL / "replay" / name
        assert hashlib.sha256(p.read_bytes()).hexdigest() == digest
        assert p.stat().st_mode & 0o222 == 0
    req = ComplianceRequest.model_validate_json(
        (LOCAL / "replay/attempt-01-full-input.json").read_text(encoding="utf-8")
    )
    raw = (LOCAL / "replay/attempt-01-response.json").read_bytes()
    output = CompactComplianceDeepSeekProvider(api_key="offline", model="deepseek-flash")._decode(
        raw, req
    )
    # Exact checkpoint implementation, loaded locally; never used as a production fallback.
    spec = importlib.util.spec_from_file_location(
        "fix2_baseline", LOCAL / "baseline_compliance_v2.py"
    )
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    with pytest.raises(ValueError, match="^CITATION_ATTRIBUTION_FAILURE$"):
        baseline.validate_compact(output, req)
    before = output.model_dump_json()
    with pytest.raises(AttributionError) as raised:
        validate_compact(output, req)
    detail = raised.value.details
    assert detail["code"] == "CITATION_ATTRIBUTION_FAILURE"
    assert detail["field"] == "explanation" and detail["scope"] == "DUPLICATE"
    assert detail["evidence_refs"] == output.evidence_refs
    assert output.findings == []
    assert "无重复、拆分或类别错配" in output.explanation
    assert req.facts["positive"]["duplicate_clear"]["status"] == "VERIFIED"
    assert "positive.duplicate_clear" not in output.factual_basis
    originals = original_chunks(
        Path("tests/fixtures/policies/p4a-222.docx").read_bytes(),
        document_id=req.evidence[0].document_id,
        policy_version_id=req.policy_version_id,
        effective_from=req.evidence[0].effective_from,
        effective_to=req.evidence[0].effective_to,
    )
    by_id = {e.chunk_id: e for e in originals}
    assert all(by_id[e.chunk_id] == e for e in req.evidence)
    from rag.retrieval.compliance_context import permits_basis

    assert not any(permits_basis(by_id[r], "DUPLICATE") for r in output.evidence_refs)
    assert {"第十四条", "第十七条", "第四十一条", "第四十三条", "第五十一条"} <= {
        e.clause_id for e in req.evidence
    }
    assert before == output.model_dump_json()
    (LOCAL / "historical-replay.json").write_text(
        json.dumps(
            dict(
                baseline="CITATION_ATTRIBUTION_FAILURE",
                current=detail,
                raw_response_sha256=manifest["attempt-01-response.json"],
                raw_unchanged=True,
                accepted=False,
                real_http=0,
            ),
            indent=2,
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "case,code",
    [
        ("unrelated_policy", "CITATION_ATTRIBUTION_FAILURE"),
        ("missing_dedupe", "UNSUPPORTED_FACT_CLAIM"),
        ("nonexistent_clause", "UNSUPPORTED_POLICY_CLAUSE"),
        ("employee_declaration", "UNSUPPORTED_FACT_CLAIM"),
        ("overlimit_pass", "RULE_FAILURE_CANNOT_PASS"),
        ("wrong_policy_version", "POLICY_VERSION_REFERENCE_MISMATCH"),
        ("unchecked_topic", "SEMANTIC_COVERAGE_INCOMPLETE"),
        ("contradictory_summary", "SEMANTIC_EXPLANATION_CONTRADICTION"),
        ("correct_id_wrong_meaning", "SEMANTIC_EXPLANATION_CONTRADICTION"),
        ("absolute_absence", "UNSUPPORTED_ABSOLUTE_CLAIM"),
        ("missing_fact_path", "UNSUPPORTED_FACT_CLAIM"),
        ("missing_provenance", "UNSUPPORTED_FACT_CLAIM"),
        ("wrong_rule_explanation", "RULE_EXPLANATION_CONTRADICTION"),
    ],
)
def test_negative_attribution_cases_fail_closed(case, code):
    req = compact_request("HOTEL")
    data = supported_output(req)
    if case == "unrelated_policy":
        data["evidence_refs"].remove(reference(req, "第四十一条"))
    elif case in {"missing_dedupe", "employee_declaration", "missing_provenance"}:
        entry = req.facts["positive"]["duplicate_clear"]
        if case == "missing_provenance":
            entry["verified_by"] = None
        else:
            entry["status"] = "DECLARED" if case == "employee_declaration" else "MISSING"
    elif case == "nonexistent_clause":
        data["explanation"] = "第九十九条规定合规检查通过。"
    elif case == "overlimit_pass":
        data["explanation"] = "必要核验完成，仍须财务批准。"
        req.rule_results[0]["outcome"] = "FAIL"
        req.rule_results[0]["actual"] = "850.00"
    elif case == "wrong_policy_version":
        req.evidence[0] = req.evidence[0].model_copy(update={"policy_version_id": "OTHER"})
    elif case == "unchecked_topic":
        data["checks"]["UNRESOLVED_QUESTIONS"] = "UNKNOWN"
        data["explanation"] = "需要继续核验。"
    elif case == "contradictory_summary":
        fs = [
            finding(
                req,
                topic="EVIDENCE_SUFFICIENCY",
                basis="HOTEL",
                clause="第十八条",
                text="住宿订单存在具体未决事项。",
            )
        ]
        data = authored_review(req, fs)
        data["explanation"] = "合规检查通过，八项均无异常。"
    elif case == "correct_id_wrong_meaning":
        data["explanation"] = "第十五条规定金额通过即整单通过，无需财务批准。"
    elif case == "absolute_absence":
        data["explanation"] = "核验已经证明完全不存在重复报销。"
    elif case == "missing_fact_path":
        data["factual_basis"].remove("positive.duplicate_clear")
    elif case == "wrong_rule_explanation":
        data["explanation"] = "住宿金额检查失败，财务须复核。"
    with pytest.raises(ValueError, match=code):
        validate_compact(normalized(data), req)


def test_nonexistent_id_and_invalid_json_rejected_before_any_semantic_acceptance():
    req = compact_request("HOTEL")
    data = supported_output(req)
    data["evidence_refs"][0] = "P4B-nonexistent"
    with pytest.raises(ValueError, match="UNSUPPORTED_EVIDENCE_REFERENCE"):
        validate_compact(normalized(data), req)
    from integrations.llm.deepseek import DeepSeekOutputError

    provider = CompactComplianceDeepSeekProvider(api_key="offline", model="offline")
    envelope = dict(
        model="offline", choices=[dict(finish_reason="stop", message=dict(content="{"))]
    )
    with pytest.raises(DeepSeekOutputError):
        provider._decode(json.dumps(envelope).encode(), req)


def test_server_summary_has_separate_sources_and_never_invents_semantic_checks():
    from domain.compliance_summary import compose_summary

    req = compact_request("HOTEL")
    req.rule_results[0].update(rule_id="HOTEL_LIMIT_MATRIX", actual="680.00", limit="800.00")
    result = validate_compact(normalized(supported_output(req)), req)
    contract = PositiveEvidenceContract(
        contract_version="p4b.positive.2",
        scope=req.facts["trusted"]["positive_scope"],
        entries=req.facts["positive"],
        required=req.facts["trusted"]["positive_required"],
        missing=[],
        invoice_requirement="ORIGINAL_AND_FINANCE_VERIFICATION",
    )
    raw = deepcopy(result.model_dump())
    text, bindings = compose_summary(
        result, contract, req.rule_results, "CHECK_PASSED", "CHECK_PASSED"
    )
    assert "模型八项检查均标记CLEAR" in text and "完全不存在" not in text
    assert "财务仍须单独批准" in text
    assert bindings["policy_evidence"] == result.evidence_refs
    assert bindings["rule_results"][0]["outcome"] == "PASS"
    assert any(f["path"] == "positive.duplicate_clear" for f in bindings["verified_facts"])
    assert result.model_dump() == raw
    result.checks.UNRESOLVED_QUESTIONS = "UNKNOWN"
    text, bindings = compose_summary(result, contract, req.rule_results, "UNKNOWN", "HUMAN_REVIEW")
    assert "八项检查均标记CLEAR" not in text
    assert "UNRESOLVED_QUESTIONS" in text
