"""Current-data advice without mandatory human attestations; isolated offline flows."""

import json
from datetime import date
from uuid import uuid4

import pytest
from advisory_v4_support import AdvisoryWireV4, output_v4
from p4b_fix2_support import invoice, login
from test_employee_hotel import BASE, declaration
from test_employee_hotel import employee_portal as employee_portal
from test_p4a_postgres import hotel_portal as hotel_portal
from test_policies_postgres import policy_pg as policy_pg

from apps.api.compliance_api import install_compliance_api
from apps.api.config import Settings
from domain.advisory_v4 import AdvisoryOutputV4
from integrations.llm.advisory_v4 import current_paths, validate_advisory_v4


@pytest.fixture
def current_data(employee_portal):
    c, app, repo, _, _, pid = employee_portal
    provider = AdvisoryWireV4()
    install_compliance_api(
        app.state.enterprise_app,
        Settings(_env_file=None, app_env="test", enterprise_enabled=True, p4a_enabled=True,
                 p4b_enabled=True, p4b_advisory_enabled=True,
                 p4b_advisory_protocol="p4b.advisory.4"),
        repo, app.state.audit_service, provider, business_clock=lambda: date(2027, 1, 5),
    )
    # Published P4A hotel matrices have their own complete-matrix confirmation;
    # they must not depend on the generic P4B scope-attestation endpoint.
    return employee_portal, provider


def submit(env, amount="680.00"):
    c = env[0]
    inv = invoice(c, dict(amount=amount))
    body = declaration()
    body.update(amount=amount, check_out="2026-11-03", seller="Synthetic Shenzhen Hotel",
                notes="无", invoice=inv)
    r = c.post(BASE + "/claims", json=body)
    assert r.status_code == 202, r.text
    login(c, "finance")
    return c.get("/api/v1.5/claims/" + r.json()["claim_id"]).json()


def decide(c, detail, action):
    return c.post("/api/v1.5/claims/" + detail["claim_id"] + "/finance-action", json=dict(
        action=action, reason="财务根据现有材料独立决定，无单独证明前置表单",
        expected_revision=detail["revision"], idempotency_key=str(uuid4()),
        advisory_report_digest=detail["advisory_analysis"]["report_digest"],
    ))


@pytest.mark.parametrize("action,stage,writes", [
    ("APPROVE", "APPROVED", 1), ("REJECT", "REJECTED", 0), ("RETURN", "RETURNED", 0),
])
def test_missing_attestations_do_not_block_advice_or_explicit_finance(current_data, action,
                                                                   stage, writes):
    env, provider = current_data
    c, _, repo = env[:3]
    detail = submit(env)
    report = detail["advisory_analysis"]
    # Matching legacy city master data must retain the published row provenance.
    city_fact = report["input_facts"]["trusted"]["city_fact"]
    assert city_fact["policy_version_id"] == report["policy_version_id"]
    assert city_fact["original_cells"]
    assert report["ai_advisory_status"] == "SUCCEEDED", (
        report["validation_errors"], report["provider_calls"])
    assert report["recommendation"] == "RECOMMEND_APPROVAL"
    assert report["analysis_basis"] == "CURRENT_DATA_WITH_RAG"
    assert report["manual_verification_required"] is False
    assert report["declared_amount_check"]["decision"] == "PASS"
    assert report["declared_amount_check"]["total_limit"] == "800.00"
    assert report["positive_evidence"]["entries"]["stay"]["status"] == "MISSING"
    assert report["positive_evidence"]["entries"]["invoice_verified"]["status"] == "MISSING"
    assert report["missing_evidence"] == []
    data = json.loads(provider.bodies[-1]["messages"][1]["content"])
    assert "positive_required" not in json.dumps(data)
    assert "invoice_verified" not in data["facts"]
    assert data["facts"]["invoice"]["total_amount"] == "680.00"
    assert "city_tier" not in data["facts"]["account"]
    assert data["facts"]["candidate"]["city"] == report["original_output"][
        "city_classification"]["city"]
    assert repo.erp_write_count(detail["claim_id"]) == 0
    r = decide(c, detail, action)
    assert r.status_code == 200, r.text
    assert r.json()["stage"] == stage
    assert repo.erp_write_count(detail["claim_id"]) == writes
    assert r.json()["advisory_analysis"]["original_output"] == report["original_output"]
    assert len(provider.requests) == 1


def test_declared_overlimit_stays_blocked_without_a_stay_attestation(current_data):
    env, _ = current_data
    detail = submit(env, "1200.00")
    report = detail["advisory_analysis"]
    assert report["ai_advisory_status"] == "SUCCEEDED", report["validation_errors"]
    assert report["declared_amount_check"]["decision"] == "FAIL"
    assert report["deterministic_status"] == "FAIL"
    assert decide(env[0], detail, "APPROVE").status_code == 409
    assert env[2].erp_write_count(detail["claim_id"]) == 0
    assert decide(env[0], detail, "RETURN").status_code == 200


def test_foreign_citations_and_fabricated_verified_facts_are_rejected(current_data):
    env, provider = current_data
    submit(env)
    request = provider.requests[-1]
    data = output_v4(request)
    data["policy_ref_keys"] = ["E999"]
    with pytest.raises(ValueError, match="UNSUPPORTED_EVIDENCE_KEY"):
        validate_advisory_v4(AdvisoryOutputV4.model_validate(data), request)
    data = output_v4(request)
    data["summary"] = "住宿事实已核实，发票已验真。"
    _, errors = validate_advisory_v4(AdvisoryOutputV4.model_validate(data), request)
    assert errors[0]["code"] == "UNSUPPORTED_VERIFIED_ASSERTION"


def test_city_classification_must_match_the_policy_row_and_city(current_data):
    env, provider = current_data
    submit(env)
    request = provider.requests[-1]
    data = output_v4(request)
    data["city_classification"]["tier"] = "IV"
    _, errors = validate_advisory_v4(AdvisoryOutputV4.model_validate(data), request)
    assert any(e["code"] == "CITY_CLASSIFICATION_CONFLICT" for e in errors)
    data = output_v4(request)
    data["city_classification"]["policy_ref_keys"] = ["E999"]
    with pytest.raises(ValueError, match="UNSUPPORTED_EVIDENCE_KEY"):
        validate_advisory_v4(AdvisoryOutputV4.model_validate(data), request)


def test_model_cannot_recommend_approval_when_declared_amount_fails(current_data):
    env, provider = current_data
    submit(env, "1200.00")
    request = provider.requests[-1]
    data = output_v4(request)
    data.update(risk_level="LOW", recommendation="RECOMMEND_APPROVAL")
    _, errors = validate_advisory_v4(AdvisoryOutputV4.model_validate(data), request)
    assert any(e["code"] == "RULE_EXPLANATION_CONTRADICTION" for e in errors)


def test_all_available_fact_references_fit_the_contract_without_fabrication(current_data):
    env, provider = current_data
    submit(env)
    request = provider.requests[-1]
    data = output_v4(request)
    data["fact_refs"] = sorted(current_paths(request))
    assert len(data["fact_refs"]) > 20
    _, errors = validate_advisory_v4(AdvisoryOutputV4.model_validate(data, strict=True), request)
    assert errors == []


def test_reaudit_after_employee_resubmission_preserves_history(current_data):
    env, provider = current_data
    c, _, repo = env[:3]
    detail = submit(env)
    returned = decide(c, detail, "RETURN")
    assert returned.status_code == 200, returned.text
    login(c)
    body = declaration()
    body.update(amount="680.00", check_out="2026-11-03", seller="Synthetic Shenzhen Hotel",
                expected_revision=returned.json()["revision"],
                invoice=invoice(c, dict(amount="680.00")))
    cid = detail["claim_id"]
    response = c.post(BASE + f"/claims/{cid}/resubmit", json=body)
    assert response.status_code == 202, response.text
    login(c, "finance")
    detail = c.get(f"/api/v1.5/claims/{cid}").json()
    original = repo._db.execute("SELECT key,body FROM p4b_records WHERE kind='ADVISORY' "
                                "AND key LIKE ?", (cid + ":%",)).fetchall()
    request = dict(expected_revision=detail["revision"], idempotency_key=str(uuid4()))
    response = c.post(f"/api/v1.5/p4b/v1/claims/{cid}/reaudit", json=request)
    assert response.status_code == 202, response.text
    current = c.get(f"/api/v1.5/claims/{cid}").json()
    assert current["version"] == detail["version"] + 1
    assert current["advisory_analysis"]["ai_advisory_status"] == "SUCCEEDED", current
    calls = len(provider.requests)
    assert c.post(f"/api/v1.5/p4b/v1/claims/{cid}/reaudit", json=request).status_code == 202
    assert len(provider.requests) == calls  # Same request cannot spend another model call.
    for key, body in original:
        assert repo._db.execute("SELECT body FROM p4b_records WHERE kind='ADVISORY' AND key=?",
                                (key,)).fetchone()[0] == body
    assert repo.erp_write_count(cid) == 0
