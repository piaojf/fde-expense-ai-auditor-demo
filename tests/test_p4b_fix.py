"""Independent offline P4B repair acceptance; no live model accuracy assertions."""

import asyncio
import json
from copy import deepcopy

import httpx
import pytest
from pydantic import ValidationError
from test_p4b_compliance import output, request

from domain.positive_evidence import EvidenceItem, build_contract
from integrations.llm.compliance import ComplianceDeepSeekProvider, validate_semantics
from integrations.llm.deepseek import DeepSeekOutputError


def contract_input():
    scope = dict(
        claim_version=1,
        candidate_digest="digest",
        role="FINANCE",
        actor_id="finance-1",
        source_reference="business-and-approval-001",
        at="2026-10-12T00:00:00+00:00",
        basis="Independent synthetic verification",
    )
    facts = dict(
        business_related=True,
        prior_approval=True,
        duplicate_clear=True,
        full_days=1,
        lunches=1,
        dinners=1,
    )
    master = dict(
        key="employee-1",
        id="hr-1",
        source="HR-001",
        actor="admin-1",
        recorded_at=scope["at"],
        effective_from="2026-11-01",
        effective_to="2027-01-01",
    )
    return dict(
        claim_id="synthetic",
        kind="ALLOWANCE",
        candidate=dict(
            incurred_on="2026-11-02",
            trip_start="2026-11-02",
            trip_end="2026-11-02",
            purpose="客户项目",
            notes="客户供餐",
        ),
        trusted=dict(
            grade="D",
            city_tier="I",
            employee_fact=master,
            city_fact=master,
            expense_mapping={
                "binding_checked": True,
                "storage_category": "OTHER",
                "expense_type": "ALLOWANCE",
            },
            policy_verification=dict(
                policy_version_id="V2",
                publication_and_authority_checked=True,
                document_sha256="a" * 64,
                effective_from="2026-11-01",
                effective_to="2027-01-01",
                scope_confirmation={"actor_id": "admin-1", "at": scope["at"]},
            ),
        ),
        verified=facts,
        record=dict(
            candidate_digest="digest",
            facts=facts,
            field_attestations={k: dict(scope) for k in facts},
        ),
        rules=[dict(policy_version_id="V2", outcome="PASS", actual="36.00", limit="36.00")],
        invoice=None,
        invoice_integrity=False,
        candidate_digest="digest",
        claim_version=1,
        started_at=scope["at"],
    )


def test_positive_evidence_has_provenance_mapping_and_real_missing_state():
    args = contract_input()
    contract = build_contract(**args)
    assert not contract.missing
    assert contract.entries["declared_purpose"].status == "DECLARED"
    assert contract.entries["business_related"].verified_by == "finance-1"
    assert contract.entries["grade"].source_reference == "HR-001"
    assert contract.entries["category_mapping"].value["storage_category"] == "OTHER"
    assert contract.invoice_requirement == "ALLOWANCE_BUSINESS_RECORDS"
    # Missing facts are never manufactured from employee text or existing flags.
    args["verified"].pop("lunches")
    changed = build_contract(**args)
    assert changed.entries["lunches"].status == "MISSING" and "lunches" in changed.missing


@pytest.mark.parametrize(
    "mode",
    [
        "no_source",
        "no_actor",
        "stale_digest",
        "wrong_version",
        "employee_actor",
        "historical_record",
        "false_fact",
    ],
)
def test_untrusted_attestations_do_not_create_verified_evidence(mode):
    args = contract_input()
    att = args["record"]["field_attestations"]["business_related"]
    if mode == "no_source":
        att.pop("source_reference")
    elif mode == "no_actor":
        att.pop("actor_id")
    elif mode == "stale_digest":
        att["candidate_digest"] = "old"
    elif mode == "wrong_version":
        att["claim_version"] = 0
    elif mode == "employee_actor":
        att["role"] = "EMPLOYEE"
    elif mode == "historical_record":
        args["record"].pop("field_attestations")
    else:
        args["verified"]["business_related"] = False
    result = build_contract(**args)
    assert result.entries["business_related"].status != "VERIFIED"
    assert "business_related" in result.missing


@pytest.mark.parametrize("kind", ["HOTEL", "ENTERTAINMENT"])
def test_transaction_invoice_original_and_finance_verification_are_both_required(kind):
    args = contract_input()
    args["kind"] = kind
    result = build_contract(**args)
    assert "original_invoice" in result.missing and "invoice_verified" in result.missing
    args["invoice"] = {"attachment": {"sha256": "a" * 64}}
    assert build_contract(**args).entries["original_invoice"].status == "CONFLICTED"
    args["invoice_integrity"] = True
    assert build_contract(**args).entries["original_invoice"].status == "VERIFIED"
    assert "invoice_verified" in build_contract(**args).missing


def test_verified_evidence_dto_cannot_be_created_without_provenance():
    with pytest.raises(ValidationError, match="VERIFIED_REQUIRES_PROVENANCE"):
        EvidenceItem(evidence_id="fake", status="VERIFIED", value=True)


@pytest.mark.parametrize("status", ["DECLARED", "MISSING", "CONFLICTED"])
def test_model_cannot_cite_nonverified_positive_entry_as_compliant(status):
    req = request()
    req.facts["positive"] = {"business_related": {"status": status, "value": True}}
    data = output(req)
    data["semantic_findings"][0]["factual_basis"] = ["positive.business_related"]
    with pytest.raises(ValueError, match=r"UNVERIFIED_POSITIVE_REFERENCE:semantic_findings.0"):
        validate_semantics(data, req)


@pytest.mark.parametrize("mode", ["evidence", "rule"])
def test_version_mismatch_is_rejected_at_semantic_boundary(mode):
    req = request()
    if mode == "evidence":
        req.evidence[0] = req.evidence[0].model_copy(update={"policy_version_id": "OTHER"})
    else:
        req.rule_results[0]["policy_version_id"] = "OTHER"
    with pytest.raises(ValueError, match="POLICY_VERSION_REFERENCE_MISMATCH"):
        validate_semantics(output(req), req)


def wire(content):
    return dict(
        model="test-model",
        choices=[dict(finish_reason="stop", message=dict(role="assistant", content=content))],
    )


@pytest.mark.parametrize(
    "mutation,loc",
    [
        ("risk", ["suggested_risk"]),
        ("extra", ["approved"]),
        ("json", ["content"]),
    ],
)
def test_safe_field_diagnostics_do_not_contain_raw_model_text(mutation, loc):
    req = request()
    data = output(req)
    if mutation == "risk":
        data["suggested_risk"] = "synthetic-secret-invalid"
    if mutation == "extra":
        data["approved"] = "synthetic-secret-invalid"
    content = "{" if mutation == "json" else json.dumps(data)
    provider = ComplianceDeepSeekProvider(api_key="synthetic-key", model="test-model")
    with pytest.raises(DeepSeekOutputError):
        provider._decode(json.dumps(wire(content)), req)
    errors = req._provider_evidence["validation_errors"]
    assert errors[0]["loc"] == loc
    assert "synthetic-secret-invalid" not in json.dumps(errors)
    if mutation == "json":
        assert errors[0]["offset"] == 1


def test_single_local_fence_repair_preserves_analysis_and_makes_one_mock_http_attempt():
    req = request()
    data = output(req)
    seen = []

    def handler(r):
        seen.append(r)
        return httpx.Response(200, json=wire("```json\n" + json.dumps(data) + "\n```"))

    provider = ComplianceDeepSeekProvider(
        api_key="synthetic-key", model="test-model", transport=httpx.MockTransport(handler)
    )
    parsed = asyncio.run(provider.analyze(req))
    assert parsed.model_dump() == data
    assert len(seen) == 1 and req._provider_evidence["local_format_repairs"] == 1
    assert validate_semantics(parsed, req) == parsed


@pytest.mark.parametrize("content", ['{"provider":"a","provider":"b"}', "{} {}", "```json\n{\n```"])
def test_local_repair_does_not_accept_duplicate_keys_truncation_or_multiple_objects(content):
    req = request()
    provider = ComplianceDeepSeekProvider(api_key="synthetic-key", model="test-model")
    with pytest.raises(DeepSeekOutputError):
        provider._decode(json.dumps(wire(content)), req)


def test_positive_contract_reaches_dynamic_wire_schema_without_synthetic_field_names():
    req = request()
    req.facts["positive"] = {
        k: v.model_dump() for k, v in build_contract(**contract_input()).entries.items()
    }
    body = ComplianceDeepSeekProvider(api_key="synthetic-key")._body(req)
    prompt = body["messages"][0]["content"]
    schema = json.loads(prompt[prompt.index('{"$defs"') :])
    paths = schema["$defs"]["SemanticFinding"]["properties"]["factual_basis"]["items"]["enum"]
    assert "positive.prior_approval" in paths
    assert "positive.invented_approval" not in paths
    assert "ALLOWANCE/ENTERTAINMENT" in prompt and "年度报销特权" in prompt
    data = deepcopy(output(req))
    for finding in data["semantic_findings"]:
        finding["factual_basis"] = ["positive.business_related"]
    assert validate_semantics(data, req).recommended_action == "CHECK_PASSED"


@pytest.mark.parametrize("mode", ["invented_clause", "uncited_clause", "null_fact", "false_fact"])
def test_clause_text_and_unknown_fact_cannot_hide_behind_valid_ids(mode):
    req = request()
    data = output(req)
    finding = data["semantic_findings"][0]
    if mode == "invented_clause":
        finding["explanation"] = "依据第九十九条可以豁免所有限制"
    elif mode == "uncited_clause":
        e = next(e for e in req.evidence if e.clause_id != req.evidence[0].clause_id)
        finding["explanation"] = "依据" + e.clause_id
    else:
        req.facts["verified"]["business_related"] = None if mode == "null_fact" else False
    with pytest.raises(ValueError, match="UNSUPPORTED_POLICY_CLAUSE|UNKNOWN_FACT_IS_NOT_COMPLIANT"):
        validate_semantics(data, req)


def test_version_mismatch_preflight_consumes_zero_mock_http_attempts():
    req = request()
    req.evidence[0] = req.evidence[0].model_copy(update={"policy_version_id": "OTHER"})
    seen = []

    def handler(r):
        seen.append(r)
        return httpx.Response(500)

    provider = ComplianceDeepSeekProvider(
        api_key="synthetic-key", model="test-model", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ValueError, match="POLICY_VERSION_REFERENCE_MISMATCH"):
        asyncio.run(provider.analyze(req))
    assert seen == []


def test_failed_rule_is_verified_computation_and_does_not_become_policy_conflict():
    args = contract_input()
    args["rules"][0]["outcome"] = "FAIL"
    result = build_contract(**args)
    assert result.entries["rule_results"].status == "VERIFIED"
    assert result.entries["rule_results"].value[0]["outcome"] == "FAIL"


def test_unchanged_candidate_digest_can_survive_a_new_audit_revision():
    args = contract_input()
    args["claim_version"] = 2
    result = build_contract(**args)
    assert result.entries["business_related"].status == "VERIFIED"
    assert result.entries["business_related"].scope["attested_claim_version"] == 1
    assert result.scope["claim_version"] == 2


def test_wire_semantic_topic_enum_matches_decode_validator():
    req = request()
    data = output(req, recommended_action="HUMAN_REVIEW")
    data["semantic_findings"][0]["issue_type"] = "INVENTED_PRIVILEGE_AUTHORITY"
    with pytest.raises(
        ValueError, match="UNSUPPORTED_SEMANTIC_TOPIC:semantic_findings.0.issue_type"
    ):
        validate_semantics(data, req)
