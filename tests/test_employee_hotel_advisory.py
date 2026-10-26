"""Actual employee declaration -> finance verification -> authored Mock advisory -> Mock ERP."""

from datetime import date
from uuid import uuid4

import pytest
from advisory_support import BASE as P4B
from advisory_support import manual
from advisory_v3_support import AdvisoryWireV3
from p4b_fix2_support import invoice, login
from test_employee_hotel import BASE, V1, declaration, verify
from test_employee_hotel import employee_portal as employee_portal
from test_p4a_postgres import hotel_portal as hotel_portal
from test_p4b_fix2_postgres import COMMON
from test_policies_postgres import policy_pg as policy_pg

from apps.api.compliance_api import install_compliance_api
from apps.api.config import Settings
from scripts.finance_workbench_demo import configure_demo_advisory
from tests.test_enterprise import action


@pytest.fixture
def employee_advisory(employee_portal):
    c, app, repo, _, _, pid = employee_portal
    provider = AdvisoryWireV3(configure_demo_advisory)
    provider.model = "mock-authored-advisory-v3"
    install_compliance_api(
        app.state.enterprise_app,
        Settings(
            app_env="test",
            enterprise_enabled=True,
            p4a_enabled=True,
            p4b_enabled=True,
            p4b_advisory_enabled=True,
            p4b_advisory_protocol="p4b.advisory.3",
            _env_file=None,
        ),
        repo,
        app.state.audit_service,
        provider,
        business_clock=lambda: date(2027, 1, 5),
    )
    login(c, "admin")
    source = c.get(P4B + "/policies/" + pid).json()
    r = c.post(
        P4B + "/policies/" + pid + "/confirm-scope",
        json=dict(
            extraction_digest=source["rules"]["extraction_digest"],
            confirmed=True,
            reason="合成原文核对",
            idempotency_key=str(uuid4()),
        ),
    )
    assert r.status_code == 200, r.text
    return employee_portal


@pytest.mark.parametrize("amount,expected", [("1680.00", "PASS"), ("2500.00", "FAIL")])
def test_three_night_total_real_workflow_and_independent_finance_gate(
    employee_advisory, amount, expected
):
    c, app, repo, _, _, _ = employee_advisory
    inv = invoice(c, dict(amount=amount))
    body = {**declaration(), "amount": amount, "seller": "Synthetic Shenzhen Hotel", "invoice": inv}
    r = c.post(BASE + "/claims", json=body)
    assert r.status_code == 202, r.text
    cid = r.json()["claim_id"]
    login(c, "finance")
    detail = c.get("/api/v1.5/claims/" + cid).json()
    assert detail["advisory_analysis"]["deterministic_status"] == "UNKNOWN"
    assert detail["advisory_analysis"]["ai_advisory_status"] == "SUCCEEDED", (
        detail["advisory_analysis"]["validation_errors"]
    )
    assert action(c, detail, "APPROVE").status_code == 409
    proof = verify(c, cid, detail["revision"])
    assert c.post(BASE + f"/claims/{cid}/verify-stay", json=proof).status_code == 200
    r = c.post(
        P4B + f"/claims/{cid}/verify",
        json=dict(
            expected_revision=0,
            facts=COMMON,
            confirmed=True,
            source_reference="SYNTHETIC-INDEPENDENT-FINANCE",
            basis="独立核对业务、票据及授权",
            idempotency_key=str(uuid4()),
        ),
    )
    assert r.status_code == 200, r.text
    r = c.post(
        P4B + f"/claims/{cid}/reaudit",
        json=dict(expected_revision=detail["revision"], idempotency_key=str(uuid4())),
    )
    assert r.status_code == 202, r.text
    detail = c.get("/api/v1.5/claims/" + cid).json()
    report = detail["advisory_analysis"]
    assert report["deterministic_status"] == expected
    assert report["rule_results"][0]["limit"] == "2400.00"
    assert report["ai_advisory_status"] == "SUCCEEDED", report["validation_errors"]
    assert repo.erp_write_count(cid) == 0
    assert manual(c, detail).status_code == 200
    detail = c.get("/api/v1.5/claims/" + cid).json()
    key = str(uuid4())
    if expected == "PASS":
        assert action(c, detail, "APPROVE", key=key).status_code == 200
        assert action(c, detail, "APPROVE", key=key).status_code == 200
        assert repo.erp_write_count(cid) == 1
    else:
        assert action(c, detail, "APPROVE", key=key).status_code == 409
        assert repo.erp_write_count(cid) == 0
    assert c.get(V1 + "/claims/" + cid).json()["facts"]["employee_stay"]["status"] == "VERIFIED"
