"""Hotel Demo boundaries and authored Mock responses through real isolated workflows."""

import os
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from advisory_support import BASE, manual, prepare
from advisory_v3_support import AdvisoryWireV3
from p4b_fix2_support import login
from test_p4a_postgres import hotel_portal as hotel_portal
from test_p4b_fix2_postgres import COMMON
from test_policies_postgres import policy_pg as policy_pg

from apps.api.compliance import HybridCatalog
from apps.api.compliance_api import install_compliance_api
from apps.api.config import Settings
from scripts.finance_workbench_demo import configure_demo_advisory
from tests.test_enterprise import action

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="Disposable PostgreSQL required"),
]


@pytest.fixture
def demo(hotel_portal):
    client, app, repo, store, users, pid = hotel_portal
    provider = AdvisoryWireV3(configure_demo_advisory)
    provider.model = "mock-authored-advisory-v3"
    install_compliance_api(
        app.state.enterprise_app,
        Settings(app_env="test", enterprise_enabled=True, p4a_enabled=True, p4b_enabled=True,
                 p4b_advisory_enabled=True, p4b_advisory_protocol="p4b.advisory.3", _env_file=None),
        repo, app.state.audit_service, provider, business_clock=lambda: date(2027, 1, 5),
    )
    login(client, "admin")
    source = client.get(BASE + "/policies/" + pid).json()
    response = client.post(BASE + "/policies/" + pid + "/confirm-scope", json=dict(
        extraction_digest=source["rules"]["extraction_digest"], confirmed=True,
        reason="合成原文核对", idempotency_key=str(uuid4()),
    ))
    assert response.status_code == 200, response.text
    return hotel_portal, provider


def booking(env, grade_end="2027-02-01", city_end="2027-02-01"):
    client, _, _, _, users, _ = env
    login(client, "admin")
    common = dict(effective_from="2026-01-01", effective_to="2027-02-01",
                  source="合成边界测试材料", version="1", reason="独立核验")
    records = [
        ("EMPLOYEE", users["employee"]["id"], {"grade": "D"}, grade_end),
        ("CITY", "city-1", {"name": "深圳", "tier": "I", "level": "PREFECTURE"}, city_end),
        ("STAY", "stay-1", dict(employee_id=users["employee"]["id"], city_key="city-1",
                               address="合成深圳住宿地址", check_in="2026-11-02",
                               check_out="2026-11-03", evidence_reference="合成住宿订单"),
         "2027-02-01"),
    ]
    for kind, key, value, end in records:
        body = {**common, "kind": kind, "key": key, "value": value, "effective_to": end}
        response = client.post("/api/v1.5/p4a/v1/facts", json=body)
        assert response.status_code == 200, response.text


@pytest.mark.parametrize("grade_end,city_end,expected", [
    ("2026-11-03", "2027-02-01", "PASS"),
    ("2027-02-01", "2026-11-03", "PASS"),
    ("2026-11-03", "2026-11-03", "PASS"),
    ("2026-11-04", "2026-11-04", "PASS"),
    ("2026-11-02", "2027-02-01", "UNKNOWN"),
    ("2027-02-01", "2026-11-02", "UNKNOWN"),
])
def test_checkout_boundary_and_actual_expiry(demo, grade_end, city_end, expected):
    env, _ = demo
    booking(env, grade_end, city_end)
    detail = prepare(env)
    report = detail["advisory_analysis"]
    assert report["deterministic_status"] == expected
    client, app, repo = env[:3]
    assert repo.erp_write_count(detail["claim_id"]) == 0
    login(client, "finance")
    if expected == "PASS":
        assert detail["ai_result"]["rule_results"][0]["outcome"] == "PASS"
        assert report["ai_advisory_status"] == "SUCCEEDED", report["validation_errors"]
        assert manual(client, detail).status_code == 200
        current = client.get("/api/v1.5/claims/" + detail["claim_id"]).json()
        key = str(uuid4())
        assert action(client, current, "APPROVE", key=key).status_code == 200
        assert action(client, current, "APPROVE", key=key).status_code == 200
        assert repo.erp_write_count(detail["claim_id"]) == 1
    else:
        assert action(client, detail, "APPROVE").status_code == 409
        assert repo.erp_write_count(detail["claim_id"]) == 0


@pytest.mark.parametrize("case", ["normal", "overlimit", "missing", "ai_failure"])
def test_demo_scenarios_have_independent_advice_and_keep_gates(demo, case):
    env, _ = demo
    booking(env)
    detail = prepare(
        env, amount="1200.00" if case == "overlimit" else "680.00",
        notes="SYNTHETIC_AI_FAILURE 独立失败样本" if case == "ai_failure" else "正常公务住宿一晚。",
        facts={k: v for k, v in COMMON.items() if k != "invoice_verified"}
        if case == "missing" else COMMON,
    )
    report = detail["advisory_analysis"]
    assert env[2].erp_write_count(detail["claim_id"]) == 0
    if case == "ai_failure":
        assert report["ai_advisory_status"] == "PARTIAL"
        assert any(e["code"] == "UNSUPPORTED_EVIDENCE_REQUIREMENT"
                   for e in report["validation_errors"])
        assert report["original_output"] is not None
    else:
        assert report["ai_advisory_status"] == "SUCCEEDED", report["validation_errors"]
        assert not report["validation_errors"]
        if case == "overlimit":
            assert report["risk_level"] == "HIGH"
            assert report["recommendation"] == "RECOMMEND_REJECTION"
            assert any(i["code"] == "RULE_FAILURE" for i in report["original_output"]["issues"])
        elif case == "missing":
            assert report["recommendation"] == "REQUEST_EVIDENCE"
            assert report["missing_evidence"] == ["invoice_verified"]
        else:
            assert report["risk_level"] == "LOW"
            assert report["recommendation"] == "RECOMMEND_APPROVAL"
    login(env[0], "finance")
    assert action(env[0], detail, "APPROVE").status_code == 409
    assert env[2].erp_write_count(detail["claim_id"]) == 0


@pytest.mark.parametrize("trip_end,expected", [("2026-11-02", "D"), ("2026-11-03", "UNKNOWN")])
def test_nonhotel_keeps_inclusive_last_day(trip_end, expected):
    record = dict(effective_from="2026-01-01", effective_to="2026-11-03",
                  kind="CITY", value={"grade": "D", "tier": "I", "name": "深圳"})
    facts = SimpleNamespace(resolve=lambda *args: record, list=lambda *args: [record],
                            read=lambda cid: None)
    catalog = SimpleNamespace(legacy=SimpleNamespace(facts=facts))
    state = SimpleNamespace(claim_id="synthetic-allowance",
                            employee=SimpleNamespace(legal_entity="DEMO-CN",
                                                     employee_id="employee", department="研发部"),
                            expense=SimpleNamespace(incurred_on=date(2026, 11, 2)))
    trusted = HybridCatalog.trusted(catalog, state, dict(city="深圳", incurred_on="2026-11-02",
                                                       trip_start="2026-11-02", trip_end=trip_end))
    assert trusted["grade"] == expected
    assert trusted["city_tier"] == ("I" if expected == "D" else "UNKNOWN")
