"""Actual graph/finance/ERP/audit under fixed hotel facts and offline wire fixtures."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from p4b_fix2_support import CompactWireFixture, create, login
from test_p4a_postgres import add_booking
from test_p4a_postgres import hotel_portal as hotel_portal
from test_p4b_fix2_postgres import COMMON
from test_p4b_fix3 import supported_output
from test_p4b_p2 import POSITIVE
from test_p4b_postgres import hybrid as hybrid
from test_policies_postgres import policy_pg as policy_pg

from tests.test_enterprise import action

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="Disposable PostgreSQL required"),
]
BASE = "/api/v1.5/p4b/v1"
SCENARIOS = [("positive-" + str(i), text) for i, text in enumerate(POSITIVE[:8])]
SCENARIOS += [
    ("overlimit", "住宿金额不超标。"),
    ("missing", "金额符合规则，但仍需财务审批。"),
    ("approval", "金额检查通过等于整单通过。"),
    ("complex", "规则通过并非不能自动批准。"),
    ("contradiction", "住宿金额超标。"),
]


@pytest.mark.parametrize("mode,text", SCENARIOS)
def test_hotel_negation_graph_and_finance(hybrid, mode, text):
    env, _ = hybrid
    c, app, repo = env[:3]

    def configure(data, request):
        data.update(supported_output(request))
        data["explanation"] = text

    provider = CompactWireFixture(configure)
    app.state.audit_service.catalog.provider = provider
    add_booking(env)
    facts = {k: v for k, v in COMMON.items() if mode != "missing" or k != "prior_approval"}
    amount = "850.00" if mode == "overlimit" else "680.00"
    detail = create(c, dict(kind="HOTEL", amount=amount, notes="正常公务住宿一晚。", facts=facts))
    login(c, "finance")
    response = c.post(
        BASE + f"/claims/{detail['claim_id']}/reaudit",
        json=dict(expected_revision=detail["revision"], idempotency_key=str(uuid4())),
    )
    assert response.status_code == 202
    current = c.get("/api/v1.5/claims/" + detail["claim_id"]).json()
    report = current["compliance_analysis"]
    assert current["stage"] == "FINANCE_REVIEW"
    assert report["rule_results"][0]["actual"] == amount
    assert report["rule_results"][0]["limit"] == "800.00"
    assert report["trusted_facts"]["grade"] == "D"
    assert report["trusted_facts"]["city_tier"] == "I"
    validation = report["semantic_validation"]
    assert validation["original_analysis"]["explanation"] == text
    assert repo.erp_write_count(detail["claim_id"]) == 0
    login(c)
    assert action(c, current, "APPROVE").status_code == 403
    login(c, "finance")
    calls = provider.http_attempts
    key = str(uuid4())
    approval = action(c, current, "APPROVE", key=key)
    positive = mode.startswith("positive-")
    if positive:
        assert report["rule_results"][0]["outcome"] == "PASS"
        assert report["compliance_status"] == report["recommended_action"] == "CHECK_PASSED"
        assert report["positive_evidence"]["missing"] == []
        assert validation["status"] == "VALIDATED"
        assert report["semantic_assessment"] == validation["original_analysis"]
        assert approval.status_code == 200
        assert action(c, current, "APPROVE", key=key).status_code == 200
        assert action(c, approval.json(), "APPROVE", key=str(uuid4())).status_code == 409
        assert repo.erp_write_count(detail["claim_id"]) == 1
    else:
        assert report["compliance_status"] != "CHECK_PASSED"
        assert validation["status"] == "REJECTED"
        assert report["semantic_assessment"] is None
        assert report["summary_bindings"] == {}
        assert approval.status_code == 409
        assert repo.erp_write_count(detail["claim_id"]) == 0
    assert provider.http_attempts == calls
    assert c.get("/api/v1.5/claims/" + detail["claim_id"]).json()["compliance_analysis"] == report
    out = Path("output/p4b/p2-fix")
    if out.is_dir():
        (out / (mode + "-hotel.json")).write_text(
            json.dumps(
                dict(
                    mode=mode,
                    rule=report["rule_results"],
                    validation=validation,
                    status=report["compliance_status"],
                    finance_http=approval.status_code,
                    erp_writes=repo.erp_write_count(detail["claim_id"]),
                    real_llm_calls=0,
                ),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
