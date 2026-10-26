"""Offline attribution and consistency, including exact private historical failure."""

import hashlib
import importlib.util
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest
from advisory_v2_support import finding_v2, output_v2
from p4b_fix2_support import reference
from test_p4b_p2 import hotel_request

from domain.advisory import AdvisoryOutput
from domain.advisory_v2 import AdvisoryOutputV2, parse_advisory
from domain.compliance import ComplianceRequest
from integrations.llm.advisory import AdvisoryDeepSeekProvider, validate_advisory
from integrations.llm.advisory_v2 import AdvisoryDeepSeekProviderV2


def check(data, request):
    return validate_advisory(parse_advisory(data), request)


@pytest.mark.parametrize("codes", [
    ("GENERAL_FINANCE_CHECK",),
    ("GENERAL_FINANCE_CHECK", "VERIFIED_STAY", "POLICY_EFFECTIVE"),
    ("AMOUNT_WITHIN_LIMIT", "PRIOR_APPROVAL_VERIFIED"),
])
def test_low_recommend_approval_allows_supported_info(codes):
    request = hotel_request()
    data = output_v2(request, codes)
    before = deepcopy(data)
    valid, errors = check(data, request)
    assert errors == [] and len(valid) == len(codes)
    assert all(i["classification"] == "INFO" for i in valid)
    assert data == before and data["requires_human_review"] is True


def test_warning_is_unresolved_and_cannot_be_low_or_recommend_approval():
    request = hotel_request()
    data = output_v2(request, ("BUSINESS_PURPOSE_UNRESOLVED",))
    _, errors = check(data, request)
    assert errors[0]["code"] == "ADVISORY_RECOMMENDATION_CONFLICT"
    data.update(risk_level="MEDIUM", recommendation="REQUEST_EVIDENCE")
    valid, errors = check(data, request)
    assert not errors
    assert valid[0]["unverified_question"]
    assert valid[0]["resolution"] == "UNRESOLVED"


@pytest.mark.parametrize("fault", [
    "relabel_blocker", "relabel_warning", "invent_code", "resolved_warning", "wrong_policy",
    "wrong_fact", "missing_fact", "false_fact", "invent_rule", "wrong_rule", "free_info",
    "budget", "bypass", "top_level_prose", "missing_field", "invent_quote", "fake_missing",
])
def test_unverifiable_outputs_never_clear_or_silently_repair(fault):
    request = hotel_request()
    data = output_v2(request)
    if fault in {"relabel_blocker", "fake_missing"}:
        request.facts["positive"]["invoice_verified"]["status"] = "MISSING"
        data["issues"] = [finding_v2(request, "MISSING_REQUIRED_EVIDENCE",
                                    fact_key="invoice_verified",
                                    fact_refs=["positive.invoice_verified"])]
        if fault == "relabel_blocker":
            data["issues"][0]["classification"] = "INFO"
        else:
            request.facts["positive"]["invoice_verified"]["status"] = "VERIFIED"
    elif fault in {"relabel_warning", "resolved_warning", "budget", "bypass", "invent_quote"}:
        data["issues"] = [finding_v2(request, "BUSINESS_PURPOSE_UNRESOLVED")]
        data.update(risk_level="MEDIUM", recommendation="REQUEST_EVIDENCE")
        if fault == "relabel_warning":
            data["issues"][0]["classification"] = "INFO"
        elif fault == "resolved_warning":
            data["issues"][0]["resolution"] = "NOT_APPLICABLE"
        elif fault == "budget":
            data["issues"][0]["question"] = "本单预算内。"
        elif fault == "bypass":
            data["issues"][0]["question"] = "无需财务批准。"
        else:
            data["issues"][0]["source_quote"] = "原文中不存在的员工记录。"
    elif fault == "invent_code":
        data["issues"][0]["code"] = "BUDGET_CLEAR"
    elif fault == "wrong_policy":
        data["issues"][0]["policy_ref_ids"] = [reference(request, "第四十四条")]
    elif fault == "wrong_fact":
        data["issues"][0]["fact_refs"] = ["positive.not_a_fact"]
    elif fault in {"missing_fact", "false_fact"}:
        request.facts["positive"]["original_invoice"]["status"] = (
            "MISSING" if fault == "missing_fact" else "VERIFIED")
        if fault == "false_fact":
            request.facts["positive"]["original_invoice"]["value"] = False
    elif fault in {"invent_rule", "wrong_rule"}:
        data["issues"] = [finding_v2(request, "AMOUNT_WITHIN_LIMIT")]
        if fault == "invent_rule":
            data["issues"][0]["rule_refs"] = ["FAKE"]
        else:
            request.rule_results[0]["outcome"] = "FAIL"
    elif fault == "free_info":
        data["issues"][0]["question"] = "在预算内且可以自动批准。"
    elif fault == "top_level_prose":
        data["explanation"] = "全部合规。"
    else:
        del data["requires_human_review"]
    before = deepcopy(data)
    try:
        _, errors = check(data, request)
    except ValueError:
        pass
    else:
        assert errors
    assert data == before


def test_missing_required_evidence_and_blocker_recommendation_conflict():
    request = hotel_request()
    request.facts["positive"]["invoice_verified"]["status"] = "MISSING"
    data = output_v2(request, ())
    data["issues"] = [finding_v2(request, "MISSING_REQUIRED_EVIDENCE",
                                fact_key="invoice_verified",
                                fact_refs=["positive.invoice_verified"])]
    data.update(risk_level="HIGH", recommendation="REQUEST_EVIDENCE")
    valid, errors = check(data, request)
    assert not errors and valid[0]["classification"] == "BLOCKER"
    data.update(risk_level="LOW", recommendation="RECOMMEND_APPROVAL")
    assert any(e["code"] == "ADVISORY_RECOMMENDATION_CONFLICT" for e in check(data, request)[1])


def test_rule_failure_cannot_be_covered_by_low_info():
    request = hotel_request()
    request.rule_results[0].update(outcome="FAIL", actual="1200.00")
    from test_p4b_compliance import chunks

    from rag.retrieval.compliance_context import minimal_evidence
    request.evidence = minimal_evidence(chunks(), kind="HOTEL", grade="D", city_tier="I",
                                       amount="1200", department="华南销售部", city="深圳")
    data = output_v2(request, ("RULE_FAILURE",))
    data.update(risk_level="HIGH", recommendation="RECOMMEND_REJECTION")
    assert not check(data, request)[1]
    data.update(risk_level="LOW", recommendation="RECOMMEND_APPROVAL")
    assert check(data, request)[1]


def test_private_real_failure_replays_before_and_after_without_modification():
    old = Path(os.environ['FDE_PRIVATE_ADVISORY_REPLAY_ROOT'])
    raw = (old/'advisory-live-1-authorized/real-response.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (
        '062238d1dbb759c76ce447888c9544c0e9993bc6640965f6205583d36c5ce221')
    request = ComplianceRequest.model_validate_json(
        (old/'advisory-live-1-preflight/input-request.json').read_text(encoding='utf-8'))
    parsed = AdvisoryDeepSeekProvider(
        api_key='offline', model='deepseek-flash')._decode(raw, request)
    dto = AdvisoryOutput.model_validate(parsed.model_dump(exclude={'provider', 'model'}))
    original = dto.model_dump_json()
    spec = importlib.util.spec_from_file_location(
        'baseline_advisory', 'output/p4b/contract-review/baseline_advisory.py')
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    historical = json.loads(
        (old/'advisory-live-1-authorized/live-validation.json').read_text(encoding='utf-8'))
    assert baseline.validate_advisory(dto, request)[1] == historical['errors']
    valid, errors = validate_advisory(dto, request)
    assert valid == []
    assert [(e['code'], e['field']) for e in errors] == [
        ('CITATION_ATTRIBUTION_FAILURE', 'issues.0.explanation'),
        ('CITATION_ATTRIBUTION_FAILURE', 'issues.1.explanation'),
        ('UNSUPPORTED_BUDGET_CLAIM', 'issues.2.explanation'),
        ('ADVISORY_RECOMMENDATION_CONFLICT', 'recommendation'),
    ]
    assert dto.model_dump_json() == original
    assert (old/'advisory-live-1-authorized/real-response.json').read_bytes() == raw


def test_v2_prompt_is_bounded_and_separates_three_evidence_kinds():
    request = hotel_request()
    body = AdvisoryDeepSeekProviderV2(api_key='offline', model='offline')._body(request)
    assert len(json.dumps(body, ensure_ascii=False).encode()) <= 32768
    prompt = body['messages'][0]['content']
    assert 'policy_ref_ids' in prompt and 'fact_refs' in prompt and 'rule_refs' in prompt
    assert 'tools' not in body
    assert AdvisoryOutputV2.model_json_schema()['additionalProperties'] is False


@pytest.mark.parametrize("fault", ["wrong_value", "out_of_interval"])
def test_policy_effective_info_needs_matching_verified_version_and_date(fault):
    request = hotel_request()
    data = output_v2(request, ("POLICY_EFFECTIVE",))
    if fault == "wrong_value":
        request.facts["positive"]["policy_version"]["value"] = "UNRELATED_VERSION"
    else:
        request.facts["candidate"]["incurred_on"] = "2027-02-01"
        request.facts["trusted"]["policy_verification"]["incurred_on"] = "2027-02-01"
    assert check(data, request)[1]


@pytest.mark.parametrize("kind", ["ALLOWANCE", "ENTERTAINMENT"])
def test_other_expense_kinds_keep_valid_zero_values_and_reject_hotel_only_codes(kind):
    from test_p4b_fix2 import compact_request
    request = compact_request(kind)
    request.rule_results[0]["rule_id"] = "SYNTHETIC_" + kind
    if kind == "ALLOWANCE":
        request.facts["positive"]["lunches"]["value"] = 0
    data = output_v2(request, ())
    assert not check(data, request)[1]
    # Each expense kind must retain its own original policy authority.
    if kind == "ALLOWANCE":
        data["issues"] = [finding_v2(request, "GENERAL_FINANCE_CHECK")]
        assert check(data, request)[1]


@pytest.mark.parametrize("fault", ["missing_id", "duplicate_id", "bad_outcome"])
def test_malformed_server_rule_records_fail_closed(fault):
    request = hotel_request()
    data = output_v2(request, ())
    if fault == "missing_id":
        del request.rule_results[0]["rule_id"]
    elif fault == "duplicate_id":
        request.rule_results.append(dict(request.rule_results[0]))
    else:
        request.rule_results[0]["outcome"] = "INFERRED_PASS"
    with pytest.raises(ValueError, match="UNSUPPORTED_RULE_REFERENCE"):
        check(data, request)


@pytest.mark.parametrize("code", ["AMOUNT_WITHIN_LIMIT", "RULE_FAILURE"])
def test_invoice_obligation_clause_is_not_an_amount_limit_basis(code):
    request = hotel_request()
    if code == "RULE_FAILURE":
        request.rule_results[0]["outcome"] = "FAIL"
    data = output_v2(request, (code,))
    data["issues"][0]["policy_ref_ids"] = [reference(request, "第十八条")]
    data.update(risk_level="HIGH", recommendation="UNDETERMINED")
    assert any(e["code"] == "CITATION_ATTRIBUTION_FAILURE" for e in check(data, request)[1])


@pytest.mark.parametrize("codes", [(), ("GENERAL_FINANCE_CHECK",),
                                   ("BUSINESS_PURPOSE_UNRESOLVED",)])
@pytest.mark.parametrize("risk", ["LOW", "HIGH"])
def test_recommend_rejection_needs_its_own_verified_structured_blocker(codes, risk):
    request = hotel_request()
    data = output_v2(request, codes)
    data.update(risk_level=risk, recommendation="RECOMMEND_REJECTION")
    assert any(e["code"] == "ADVISORY_REJECTION_BASIS_INCOMPLETE"
               for e in check(data, request)[1])


def test_unrelated_pass_rule_cannot_prove_an_amount_check():
    request = hotel_request()
    request.rule_results[0]["rule_id"] = "UNRELATED_PASS_RULE"
    data = output_v2(request, ("AMOUNT_WITHIN_LIMIT",))
    assert any(e["code"] == "RULE_EXPLANATION_CONTRADICTION" for e in check(data, request)[1])


@pytest.mark.parametrize("classification", ["INFO", "WARNING", "BLOCKER"])
def test_notes_cannot_create_a_verified_exception_blocker(classification):
    request = hotel_request()
    data = output_v2(request, ("BUSINESS_PURPOSE_UNRESOLVED",))
    data.update(risk_level="HIGH", recommendation="RECOMMEND_REJECTION")
    data["issues"][0].update(code="EXCEPTION_UNAUTHORIZED", classification=classification)
    with pytest.raises(ValueError):
        parse_advisory(data)
    assert data["issues"][0]["code"] == "EXCEPTION_UNAUTHORIZED"
