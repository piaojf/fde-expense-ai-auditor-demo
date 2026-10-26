"""Independent non-authoritative DTO/attribution/transport probes."""

import asyncio
import json
import os
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from advisory_support import issue, positive
from test_p4b_p2 import hotel_request

from apps.api.config import Settings
from apps.api.main import create_app
from domain.advisory import AdvisoryOutput, ManualAdvisoryReview
from domain.compliance import ComplianceRequest
from integrations.llm.advisory import AdvisoryDeepSeekProvider, validate_advisory
from integrations.llm.compliance_v2 import CompactComplianceDeepSeekProvider, validate_compact
from integrations.llm.deepseek import DeepSeekOutputError


def test_default_mode_off_and_requires_authority():
    assert not Settings(_env_file=None).p4b_advisory_enabled
    with pytest.raises(ValueError, match="requires P4B"):
        create_app(Settings(app_env="test", p4b_advisory_enabled=True, _env_file=None))


@pytest.mark.parametrize("field,value", [
    ("requires_human_review", False), ("recommendation", "APPROVE"),
    ("confidence", 1.0), ("reason", "自动批准"), ("risk_level", "SAFE"),
])
def test_dto_never_accepts_approval_authority_or_unbound_text(field, value):
    data = positive(hotel_request())
    data[field] = value
    with pytest.raises(ValueError):
        AdvisoryOutput.model_validate(data, strict=True)


@pytest.mark.parametrize("fault", ["wrong_ref", "wrong_fact", "unverified", "approval", "amount",
                                    "duplicate", "bad_basis", "bad_need", "conflicting_advice"])
def test_invalid_advisory_is_not_model_success(fault):
    req = hotel_request()
    data = positive(req)
    item = issue(req, "DUPLICATE_SPLIT_CATEGORY", "DUPLICATE", "第四十一条",
                 "无拆分重复。", facts=["positive.duplicate_clear"])
    data.update(recommendation="UNDETERMINED", risk_level="MEDIUM", issues=[item])
    if fault == "wrong_ref":
        item["policy_ref_ids"] = ["nonexistent"]
    elif fault == "wrong_fact":
        item["fact_refs"] = ["positive.no_such_fact"]
    elif fault == "unverified":
        req.facts["positive"]["duplicate_clear"]["status"] = "MISSING"
    elif fault == "approval":
        item["finance_check"] = "可以自动批准。"
    elif fault == "amount":
        item["explanation"] = "金额检查失败。"
        item["fact_refs"].append("rule_results")
    elif fault == "duplicate":
        item["fact_refs"] = ["candidate.notes"]
    elif fault == "bad_basis":
        item["basis"] = "HOTEL"
    elif fault == "bad_need":
        data["missing_evidence"] = ["business_related"]
    else:
        data["recommendation"] = "RECOMMEND_APPROVAL"
    valid, errors = validate_advisory(AdvisoryOutput.model_validate(data), req)
    assert errors
    if fault not in {"bad_need", "conflicting_advice"}:
        assert valid == []


def test_partial_keeps_only_valid_findings_without_reference_repair():
    req = hotel_request()
    invalid = issue(req, "DUPLICATE_SPLIT_CATEGORY", "DUPLICATE", "第十五条", "无拆分重复。")
    valid = issue(req, "BUSINESS_PURPOSE", "BUSINESS", "第二条", "业务目的需财务核查。")
    data = positive(req)
    data.update(recommendation="UNDETERMINED", risk_level="MEDIUM", issues=[invalid, valid])
    before = deepcopy(data)
    found, errors = validate_advisory(AdvisoryOutput.model_validate(data), req)
    assert found == [valid] and errors[0]["code"] == "CITATION_ATTRIBUTION_FAILURE"
    assert data == before


@pytest.mark.parametrize("changed", ["rule", "missing", "wrong_policy", "draft", "appendix"])
def test_source_and_rule_integrity_never_created_by_advice(changed):
    req = hotel_request()
    data = positive(req)
    if changed == "rule":
        req.rule_results[0]["outcome"] = "FAIL"
    elif changed == "missing":
        req.facts["positive"]["invoice_verified"]["status"] = "MISSING"
    elif changed == "wrong_policy":
        req.evidence[0].policy_version_id = "wrong"
    elif changed == "draft":
        req.facts["trusted"]["policy_verification"]["publication_and_authority_checked"] = False
    else:
        req.evidence[0].section = "附录"
    try:
        _, errors = validate_advisory(AdvisoryOutput.model_validate(data), req)
    except ValueError:
        pass
    else:
        assert errors


@pytest.mark.parametrize("mutation", ["duplicate_key", "wrong_model", "tool", "authority", "key"])
def test_real_decoder_rejects_bad_wire_and_never_calls_tools(mutation):
    req = hotel_request()
    data = positive(req)
    content = json.dumps(data, ensure_ascii=False)
    payload = dict(model="offline", choices=[dict(finish_reason="stop", message=dict(
        role="assistant", content=content))])
    if mutation == "duplicate_key":
        payload["choices"][0]["message"]["content"] = content[:-1] + ',"risk_level":"LOW"}'
    elif mutation == "wrong_model":
        payload["model"] = "other"
    elif mutation == "tool":
        payload["choices"][0]["message"]["tool_calls"] = [{"name": "approve"}]
    elif mutation == "authority":
        data["requires_human_review"] = False
        payload["choices"][0]["message"]["content"] = json.dumps(data)
    else:
        payload["choices"][0]["message"]["content"] = "synthetic-secret"
    def handler(request):
        return httpx.Response(200, json=payload)
    provider = AdvisoryDeepSeekProvider(api_key="synthetic-secret", model="offline",
                                        transport=httpx.MockTransport(handler))
    with pytest.raises(DeepSeekOutputError):
        asyncio.run(provider.analyze(req))
    assert "synthetic-secret" not in str(req._provider_evidence)


def test_historical_real_execution_remains_attribution_failure_without_repair():
    old = Path(os.environ['FDE_PRIVATE_COMPACT_REPLAY_ROOT'])
    req = ComplianceRequest.model_validate_json(
        (old / 'final-input-request.json').read_text(encoding='utf-8'))
    response = (old / 'real-response.json').read_bytes()
    provider = CompactComplianceDeepSeekProvider(api_key='offline', model='deepseek-flash')
    parsed = provider._decode(response, req)
    before = parsed.model_dump_json()
    with pytest.raises(ValueError) as exc:
        validate_compact(parsed, req)
    assert exc.value.details['code'] == 'CITATION_ATTRIBUTION_FAILURE'
    assert exc.value.details['field'] == 'explanation'
    assert exc.value.details['scope'] == 'DUPLICATE'
    assert parsed.model_dump_json() == before


def test_manual_review_is_not_exception_authorization():
    data = dict(report_digest='a' * 64, expected_revision=1, outcome='CLEAR',
                exception_required=True, unreliable_advice_acknowledged=True,
                source_reference='synthetic', basis='independent check',
                idempotency_key='synthetic')
    with pytest.raises(ValueError):
        ManualAdvisoryReview.model_validate(data)
