"""FIX3 report preservation, independent positive hotel, finance and ERP isolation."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from p4b_fix2_support import CompactWireFixture, create, login, reference
from test_p4a_postgres import add_booking
from test_p4a_postgres import hotel_portal as hotel_portal
from test_p4b_fix2_postgres import COMMON
from test_p4b_fix3 import supported_output
from test_p4b_postgres import hybrid as hybrid
from test_policies_postgres import policy_pg as policy_pg

from tests.test_enterprise import action

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="Disposable PostgreSQL required"),
]
BASE = "/api/v1.5/p4b/v1"


@pytest.mark.parametrize("mode", ["positive", "wrong_citation", "wrong_meaning", "invalid_json"])
def test_fix3_hotel_summary_retains_raw_validation_finance_and_erp(hybrid, mode):
    env, _ = hybrid
    c, app, repo = env[:3]
    provider = CompactWireFixture()
    app.state.audit_service.catalog.provider = provider
    add_booking(env)
    case = dict(kind="HOTEL", amount="680.00", notes="正常公务住宿一晚。", facts=COMMON)
    detail = create(c, case)

    def configure(data, request):
        data.update(supported_output(request))
        if mode == "wrong_citation":
            data["evidence_refs"].remove(reference(request, "第四十一条"))
        elif mode == "wrong_meaning":
            data["explanation"] = "第十五条规定金额通过即整单通过，无需财务批准。"

    provider.configure = configure
    if mode == "invalid_json":
        provider.mode = "invalid_json"
    login(c, "finance")
    assert (
        c.post(
            BASE + f"/claims/{detail['claim_id']}/reaudit",
            json=dict(expected_revision=detail["revision"], idempotency_key=str(uuid4())),
        ).status_code
        == 202
    )
    current = c.get("/api/v1.5/claims/" + detail["claim_id"]).json()
    report = current["compliance_analysis"]
    assert repo.erp_write_count(detail["claim_id"]) == 0
    assert current["stage"] == "FINANCE_REVIEW"
    login(c)
    assert action(c, current, "APPROVE").status_code == 403
    login(c, "finance")
    calls = provider.http_attempts
    key = str(uuid4())
    approval = action(c, current, "APPROVE", key=key)
    if mode == "positive":
        assert report["compliance_status"] == report["recommended_action"] == "CHECK_PASSED"
        assert report["analysis_status"] == "SUCCEEDED"
        assert report["semantic_validation"]["status"] == "VALIDATED"
        assert report["semantic_validation"]["original_analysis"] == report["semantic_assessment"]
        assert report["explanation"].startswith("服务端摘要：")
        assert report["explanation"] != report["semantic_assessment"]["explanation"]
        assert report["rule_results"][0]["outcome"] == "PASS"
        assert report["rule_results"][0]["actual"] == "680.00"
        assert report["rule_results"][0]["limit"] == "800.00"
        assert report["trusted_facts"]["grade"] == "D"
        assert report["trusted_facts"]["city_tier"] == "I"
        assert report["positive_evidence"]["missing"] == []
        assert (
            report["summary_bindings"]["policy_evidence"]
            == report["semantic_assessment"]["evidence_refs"]
        )
        assert approval.status_code == 200
        assert action(c, current, "APPROVE", key=key).status_code == 200
        assert action(c, approval.json(), "APPROVE", key=str(uuid4())).status_code == 409
        assert repo.erp_write_count(detail["claim_id"]) == 1
    else:
        assert report["analysis_status"] == "FAILED"
        assert report["recommended_action"] == "HUMAN_REVIEW"
        assert report["semantic_assessment"] is None and report["summary_bindings"] == {}
        assert approval.status_code == 409 and repo.erp_write_count(detail["claim_id"]) == 0
        if mode != "invalid_json":
            audit = report["semantic_validation"]
            assert audit["status"] == "REJECTED"
            assert audit["original_analysis"]["recommended_action"] == "CHECK_PASSED"
            assert audit["error"]["field"] == "explanation"
            expected = (
                "CITATION_ATTRIBUTION_FAILURE"
                if mode == "wrong_citation"
                else "SEMANTIC_EXPLANATION_CONTRADICTION"
            )
            assert audit["error"]["code"] == expected
            persisted = c.get("/api/v1.5/claims/" + detail["claim_id"]).json()[
                "compliance_analysis"
            ]
            assert persisted == report
    assert provider.http_attempts == calls  # Approval never calls model.
    out = Path("output/p4b/fix3")
    if out.is_dir():
        (out / (mode + "-hotel.json")).write_text(
            json.dumps(
                dict(
                    mode=mode,
                    status=report["compliance_status"],
                    analysis=report["analysis_status"],
                    finance_http=approval.status_code,
                    erp_writes=repo.erp_write_count(detail["claim_id"]),
                    real_llm_calls=0,
                    rule=report["rule_results"],
                    validation=report["semantic_validation"].get("status"),
                ),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
