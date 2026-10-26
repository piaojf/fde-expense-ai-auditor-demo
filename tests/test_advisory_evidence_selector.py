"""Offline selector identity, source integrity, semantic attribution and historical replay."""

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest
from advisory_v3_support import output_v3
from pydantic import ValidationError
from test_p4b_p2 import hotel_request

from domain.advisory_v2 import parse_advisory
from domain.advisory_v3 import AdvisoryOutputV3
from domain.compliance import ComplianceRequest
from integrations.llm.advisory import validate_advisory
from integrations.llm.advisory_v2 import AdvisoryDeepSeekProviderV2
from integrations.llm.advisory_v3 import AdvisoryDeepSeekProviderV3
from integrations.llm.evidence_selector import prepare_selector, resolve_keys, selector_manifest

CODES = ("GENERAL_FINANCE_CHECK", "VERIFIED_STAY", "POLICY_EFFECTIVE",
         "PRIOR_APPROVAL_VERIFIED", "AMOUNT_WITHIN_LIMIT")


def setup():
    request = hotel_request()
    manifest = prepare_selector(request)
    return request, manifest, output_v3(request, CODES)


def key(manifest, clause):
    return next(e["short_id"] for e in manifest["entries"] if e["clause_id"] == clause)


def test_valid_short_keys_map_exactly_and_keep_original_output():
    request, manifest, data = setup()
    before = deepcopy(data)
    assert resolve_keys(["E01"], manifest, "report") == [request.evidence[0].chunk_id]
    assert len(manifest["entries"]) == len(request.evidence) == 39
    valid, errors = validate_advisory(parse_advisory(data), request)
    assert errors == [] and len(valid) == 5
    assert valid[3]["policy_ref_ids"] == [next(
        e.chunk_id for e in request.evidence if e.clause_id == "第四十四条")]
    assert data == before
    assert manifest["entries"][0]["source_position"]
    assert manifest["entries"][0]["applicable_scope"]["grade"] == "D"


@pytest.mark.parametrize("bad", ["E99", "E00", "E1", "E001", "第三十九条", "E01 ", "e01"])
def test_unknown_and_clause_name_keys_are_never_inferred(bad):
    request, _, data = setup()
    data["policy_ref_keys"] = [bad]
    with pytest.raises((ValidationError, ValueError)):
        validate_advisory(parse_advisory(data), request)


def test_duplicate_keys_rejected():
    request, _, data = setup()
    data["issues"][0]["policy_ref_keys"] *= 2
    with pytest.raises(ValueError, match="UNSUPPORTED_EVIDENCE_KEY"):
        validate_advisory(parse_advisory(data), request)


@pytest.mark.parametrize("fault", ["version", "sha", "text", "position", "expired", "claim",
                                    "facts", "scope", "missing", "reorder"])
def test_changed_request_cannot_resolve_a_pinned_snapshot(fault):
    request, _, data = setup()
    if fault == "version":
        request.evidence[0].policy_version_id = "OLD"
    elif fault == "sha":
        request.evidence[0].document_sha256 = "a" * 64
    elif fault == "text":
        request.evidence[0].source_text += "tamper"
    elif fault == "position":
        request.evidence[0].source_position["body_index"] += 1
    elif fault == "expired":
        request.facts["candidate"]["incurred_on"] = "2027-01-01"
    elif fault == "claim":
        request.claim_id = "another-claim"
    elif fault == "facts":
        request.facts["candidate"]["notes"] = "changed"
    elif fault == "scope":
        request.facts["trusted"]["grade"] = "A"
    elif fault == "missing":
        request.evidence.pop()
    else:
        request.evidence.reverse()
    with pytest.raises(ValueError, match="EVIDENCE_SNAPSHOT_MISMATCH"):
        validate_advisory(parse_advisory(data), request)


@pytest.mark.parametrize("fault", ["version", "sha", "text", "expired", "scope", "missing"])
def test_invalid_initial_sources_are_rejected_before_mapping(fault):
    request = hotel_request()
    if fault == "version":
        request.evidence[0].policy_version_id = "OLD"
    elif fault == "sha":
        request.evidence[0].document_sha256 = "a" * 64
    elif fault == "text":
        request.evidence[0].source_text += "tamper"
    elif fault == "expired":
        request.facts["candidate"]["incurred_on"] = "2027-01-01"
        request.facts["trusted"]["policy_verification"]["incurred_on"] = "2027-01-01"
    elif fault == "scope":
        request.facts["trusted"]["grade"] = "A"
    else:
        request.evidence.pop()
    with pytest.raises(ValueError):
        prepare_selector(request)


def test_real_but_irrelevant_hotel_clause_cannot_prove_approval():
    request, manifest, data = setup()
    data["issues"][3]["policy_ref_keys"] = [key(manifest, "第十五条")]
    valid, errors = validate_advisory(parse_advisory(data), request)
    assert not any(i["code"] == "PRIOR_APPROVAL_VERIFIED" for i in valid)
    assert any(e["code"] == "CITATION_ATTRIBUTION_FAILURE" for e in errors)
    assert any(e["code"] == "ADVISORY_APPROVAL_BASIS_INCOMPLETE" for e in errors)
    assert data["recommendation"] == "RECOMMEND_APPROVAL"  # original remains untrusted


def test_cross_request_and_same_input_new_invocation_are_isolated():
    a, ma, data = setup()
    b = a.model_copy(deep=True)
    mb = prepare_selector(b)
    assert ma["entries"] == mb["entries"]
    assert ma["evidence_snapshot_id"] != mb["evidence_snapshot_id"]
    with pytest.raises(ValueError, match="EVIDENCE_SNAPSHOT_MISMATCH"):
        validate_advisory(parse_advisory(data), b)
    b = a.model_copy(deep=True)
    b.claim_id = "B"
    b.evidence.reverse()
    mb = prepare_selector(b)
    assert resolve_keys(["E01"], ma, "report") != resolve_keys(["E01"], mb, "report")
    with pytest.raises(ValueError, match="EVIDENCE_SNAPSHOT_MISMATCH"):
        validate_advisory(parse_advisory(data), b)


def test_unpinned_request_is_never_reconstructed_from_model_response():
    _, _, data = setup()
    with pytest.raises(ValueError, match="NOT_PINNED"):
        validate_advisory(parse_advisory(data), hotel_request())


def test_manifest_is_a_detached_copy_and_immutable_fingerprint_is_verified():
    request, manifest, _ = setup()
    manifest["entries"][0]["original_chunk_id"] = "forged"
    assert selector_manifest(request)["entries"][0]["original_chunk_id"] != "forged"
    request._evidence_selector = json.dumps(manifest)
    with pytest.raises(ValueError, match="EVIDENCE_SNAPSHOT_MISMATCH"):
        selector_manifest(request)


def test_v3_input_preserves_all_original_text_tables_approval_and_version():
    request = hotel_request()
    v2 = AdvisoryDeepSeekProviderV2(api_key="offline")._body(request)
    v3 = AdvisoryDeepSeekProviderV3(api_key="offline")._body(request)
    data = json.loads(v3["messages"][1]["content"])
    assert data["schema_version"] == "p4b.input.3"
    assert [e["source_text"] for e in data["evidence"]] == [e.source_text for e in request.evidence]
    assert data["policy"]["document_sha256"] == request.evidence[0].document_sha256
    assert data["evidence"][0]["short_id"] == "E01"
    assert "第四十四条" in {e["clause_id"] for e in data["evidence"]}
    assert "TABLE-5-ROW-4" in {e["clause_id"] for e in data["evidence"]}
    assert all("chunk_id" not in e for e in data["evidence"])
    assert len(json.dumps(v3, ensure_ascii=False, separators=(",", ":")).encode()) <= 32768
    assert AdvisoryDeepSeekProviderV2(api_key="offline")._body(request) == v2


def test_live_2_failure_replays_unchanged_and_never_becomes_short_keys():
    root = Path(os.environ['FDE_PRIVATE_SELECTOR_REPLAY_ROOT'])
    path = root / 'advisory-live-2-authorized/real-response.json'
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (
        'ad476dea926343ea58b57ae1850b53e0e7eac9005a638c4498353394688e2aee')
    request = ComplianceRequest.model_validate_json(
        (root / 'advisory-live-2-preflight/input-request.json').read_bytes())
    output = AdvisoryDeepSeekProviderV2(
        api_key='offline', model='deepseek-flash')._decode(raw, request)
    dto = parse_advisory(output.model_dump(exclude={'provider', 'model'}))
    with pytest.raises(ValueError, match='UNSUPPORTED_EVIDENCE_REFERENCE:report'):
        validate_advisory(dto, request)
    manifest = prepare_selector(request)
    originals = json.loads(json.loads(raw)['choices'][0]['message']['content'])
    for item in [originals, *originals['issues']]:
        with pytest.raises(ValueError, match='UNSUPPORTED_EVIDENCE_KEY'):
            resolve_keys(item['policy_ref_ids'], manifest, 'historical')
    with pytest.raises(ValidationError):
        AdvisoryOutputV3.model_validate(originals, strict=True)
    assert path.read_bytes() == raw
    ledger = json.loads((root / 'advisory-live-2-authorized/closed-ledger-final.json').read_bytes())
    assert ledger['status'] == 'CLOSED' and ledger['real_llm_gate'] == 'FAIL'


def test_report_cannot_cite_an_unrelated_real_key():
    request, manifest, data = setup()
    data["policy_ref_keys"] = [key(manifest, "第五条")]
    valid, errors = validate_advisory(parse_advisory(data), request)
    assert len(valid) == 5
    assert any(e["code"] == "CITATION_ATTRIBUTION_FAILURE" and e["field"] == "report"
               for e in errors)


def test_initial_clause_label_cannot_impersonate_an_approval_clause():
    request = hotel_request()
    table = next(e for e in request.evidence if e.clause_id == "TABLE-5-ROW-4")
    table.clause_id = "第四十四条"
    with pytest.raises(ValueError, match="CHUNK_IDENTITY"):
        prepare_selector(request)


@pytest.mark.parametrize("failure", ["http", "timeout"])
def test_each_provider_attempt_keeps_its_own_snapshot_even_on_failure(failure):
    import asyncio

    import httpx
    from advisory_v3_support import output_v3

    request = hotel_request()
    count = 0

    def handler(wire):
        nonlocal count
        count += 1
        if count == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("synthetic offline timeout")
            return httpx.Response(503, json={"error": "synthetic"})
        data = output_v3(request)
        return httpx.Response(200, json=dict(model="offline", choices=[dict(
            finish_reason="stop", message=dict(role="assistant", content=json.dumps(data)))]))

    provider = AdvisoryDeepSeekProviderV3(
        api_key="offline-key", model="offline", transport=httpx.MockTransport(handler))
    with pytest.raises(Exception):
        asyncio.run(provider.analyze(request))
    first = request._evidence_selector
    output = asyncio.run(provider.analyze(request))
    assert len(request._evidence_selector_attempts) == 2
    assert request._evidence_selector_attempts[0] == first
    assert request._evidence_selector_attempts[1] == request._evidence_selector
    assert first != request._evidence_selector
    from integrations.llm.evidence_selector import digest
    for serialized in request._evidence_selector_attempts:
        snapshot = json.loads(serialized)
        fingerprint = snapshot.pop("evidence_snapshot_id")
        assert digest(snapshot) == fingerprint
    assert validate_advisory(parse_advisory(output.model_dump(
        exclude={"provider", "model"})), request)[1] == []


@pytest.mark.parametrize("classification", ["WARNING", "BLOCKER"])
def test_v3_retains_warning_blocker_and_recommendation_consistency(classification):
    request = hotel_request()
    if classification == "BLOCKER":
        request.facts["positive"]["invoice_verified"].update(status="MISSING", value=None)
    prepare_selector(request)
    code = ("BUSINESS_PURPOSE_UNRESOLVED" if classification == "WARNING"
            else "MISSING_REQUIRED_EVIDENCE")
    data = output_v3(request, (code,))
    data.update(risk_level="MEDIUM" if classification == "WARNING" else "HIGH",
                recommendation="REQUEST_EVIDENCE")
    if classification == "BLOCKER":
        data["issues"][0].update(
            fact_key="invoice_verified", fact_refs=["positive.invoice_verified"])
    valid, errors = validate_advisory(parse_advisory(data), request)
    assert errors == [] and valid[0]["classification"] == classification
    assert valid[0]["resolution"] == "UNRESOLVED"
    data.update(risk_level="LOW", recommendation="RECOMMEND_APPROVAL")
    assert validate_advisory(parse_advisory(data), request)[1]
