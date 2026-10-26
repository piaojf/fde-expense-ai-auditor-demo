"""Offline regression for short original notes and three-state rule explanations."""

import asyncio
import json
from copy import deepcopy

import pytest
from advisory_v2_support import AdvisoryWireV2, output_v2
from advisory_v3_support import AdvisoryWireV3, output_v3
from test_p4b_p2 import hotel_request

from domain.advisory_v2 import parse_advisory
from integrations.llm.advisory import validate_advisory
from integrations.llm.advisory_v2 import AdvisoryDeepSeekProviderV2
from integrations.llm.advisory_v3 import AdvisoryDeepSeekProviderV3
from integrations.llm.evidence_selector import prepare_selector


def judgment(version, request, codes):
    if version == 3:
        prepare_selector(request)
        return output_v3(request, codes)
    return output_v2(request, codes)


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("notes", ["无", "无备注", "正常交流"])
def test_short_exact_notes_reach_semantic_validation(version, notes):
    request = hotel_request()
    request.facts["candidate"]["notes"] = notes
    data = judgment(version, request, ("BUSINESS_PURPOSE_UNRESOLVED",))
    data.update(risk_level="MEDIUM", recommendation="REQUEST_EVIDENCE")
    before = deepcopy(data)
    valid, errors = validate_advisory(parse_advisory(data), request)
    assert not errors
    assert valid[0]["source_quote"] == notes
    assert valid[0]["resolution"] == "UNRESOLVED"
    assert data == before


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("quote", ["", "有"])
def test_empty_or_invented_quote_is_still_rejected_semantically(version, quote):
    request = hotel_request()
    request.facts["candidate"]["notes"] = "无"
    data = judgment(version, request, ("BUSINESS_PURPOSE_UNRESOLVED",))
    data.update(risk_level="MEDIUM", recommendation="REQUEST_EVIDENCE")
    data["issues"][0]["source_quote"] = quote
    _, errors = validate_advisory(parse_advisory(data), request)
    assert any(e["code"] == "UNSUPPORTED_SEMANTIC_OBSERVATION" for e in errors)


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("field,text", [
    ("source_quote", "无需财务批准。"), ("question", "无需财务批准。"),
    ("question", "无"), ("question", "规则失败。"),
])
def test_literal_none_quote_does_not_exempt_other_unsupported_claims(version, field, text):
    request = hotel_request()
    request.rule_results[0]["outcome"] = "UNKNOWN"
    request.facts["candidate"]["notes"] = text if field == "source_quote" else "无"
    data = judgment(version, request, ("BUSINESS_PURPOSE_UNRESOLVED",))
    data.update(risk_level="UNDETERMINED", recommendation="REQUEST_EVIDENCE")
    data["issues"][0][field] = text
    valid, errors = validate_advisory(parse_advisory(data), request)
    assert errors and not valid


@pytest.mark.parametrize("wire", [AdvisoryWireV2, AdvisoryWireV3])
def test_one_character_quote_survives_native_wire_decoder(wire):
    request = hotel_request()
    request.facts["candidate"]["notes"] = "无"

    # output_v3 only reads the pinned manifest; do not create another invocation.
    def configure_bound(data, bound_request):
        factory = output_v3 if wire == AdvisoryWireV3 else output_v2
        data.update(factory(bound_request, ("BUSINESS_PURPOSE_UNRESOLVED",)),
                    risk_level="MEDIUM", recommendation="REQUEST_EVIDENCE")

    provider = wire(configure_bound)
    parsed = asyncio.run(provider.analyze(request))
    valid, errors = validate_advisory(
        parse_advisory(parsed.model_dump(exclude={"provider", "model"})), request)
    assert not errors and valid[0]["source_quote"] == "无"
    assert len(provider.bodies) == 1


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("outcome,offered", [
    ("PASS", {"AMOUNT_WITHIN_LIMIT"}), ("FAIL", {"RULE_FAILURE"}), ("UNKNOWN", set()),
])
def test_prompt_catalog_and_wire_schema_match_server_rule_outcome(version, outcome, offered):
    request = hotel_request()
    request.rule_results[0]["outcome"] = outcome
    provider = AdvisoryDeepSeekProviderV2 if version == 2 else AdvisoryDeepSeekProviderV3
    body = provider(api_key="offline", model="offline")._body(request)
    prompt = body["messages"][0]["content"]
    catalog, schema = prompt.split("目录：", 1)[1].split("；schema：", 1)
    catalog, schema = json.loads(catalog), json.loads(schema)
    rule_codes = {"AMOUNT_WITHIN_LIMIT", "RULE_FAILURE"}
    assert rule_codes & catalog.keys() == offered
    finding = schema["$defs"][f"AdvisoryFindingV{version}"]["properties"]
    assert rule_codes & set(finding["code"]["enum"]) == offered
    assert "minLength" not in finding["source_quote"]["anyOf"][0]
    assert "UNKNOWN=待核实，绝不是失败或超标" in prompt
    assert len(json.dumps(body, ensure_ascii=False).encode()) <= 32768


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("code", ["RULE_FAILURE", "AMOUNT_WITHIN_LIMIT"])
def test_unknown_cannot_be_relabelled_as_failure_or_pass(version, code):
    request = hotel_request()
    request.rule_results[0]["outcome"] = "UNKNOWN"
    data = judgment(version, request, (code,))
    data.update(risk_level="UNDETERMINED", recommendation="REQUEST_EVIDENCE")
    before = deepcopy(data)
    valid, errors = validate_advisory(parse_advisory(data), request)
    assert not valid
    assert any(e["code"] == "RULE_EXPLANATION_CONTRADICTION" for e in errors)
    assert data == before


@pytest.mark.parametrize("version", [2, 3])
def test_unknown_cannot_be_called_failed_in_model_question_even_with_rule_fact_ref(version):
    request = hotel_request()
    request.rule_results[0]["outcome"] = "UNKNOWN"
    request.facts["candidate"]["notes"] = "无"
    data = judgment(version, request, ("BUSINESS_PURPOSE_UNRESOLVED",))
    data.update(risk_level="UNDETERMINED", recommendation="REQUEST_EVIDENCE")
    data["issues"][0].update(question="规则失败，请补充说明。",
                            fact_refs=["candidate.notes", "rule_results"])
    valid, errors = validate_advisory(parse_advisory(data), request)
    assert not valid
    assert any(e["code"] == "RULE_EXPLANATION_CONTRADICTION" for e in errors)


@pytest.mark.parametrize("version", [2, 3])
def test_unknown_can_request_the_actual_missing_fact_without_rule_failure(version):
    request = hotel_request()
    request.rule_results[0]["outcome"] = "UNKNOWN"
    request.facts["positive"]["stay"]["status"] = "MISSING"
    data = judgment(version, request, ("MISSING_REQUIRED_EVIDENCE",))
    data["issues"][0].update(fact_key="stay", fact_refs=["positive.stay"])
    valid, errors = validate_advisory(parse_advisory(data), request)
    assert not errors
    assert valid[0]["code"] == "MISSING_REQUIRED_EVIDENCE"
    assert data["recommendation"] == "REQUEST_EVIDENCE"
    assert "规则失败" not in valid[0]["explanation"]


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("outcome,code", [
    ("PASS", "AMOUNT_WITHIN_LIMIT"), ("FAIL", "RULE_FAILURE"),
])
def test_actual_pass_and_failure_explanations_remain_supported(version, outcome, code):
    request = hotel_request()
    request.rule_results[0]["outcome"] = outcome
    data = judgment(version, request, (code,))
    if outcome == "FAIL":
        data.update(risk_level="HIGH", recommendation="RECOMMEND_REJECTION")
    valid, errors = validate_advisory(parse_advisory(data), request)
    assert not errors
    assert valid[0]["code"] == code
