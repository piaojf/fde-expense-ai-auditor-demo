"""Actual isolated graph/API/DB repair checks with a universal optimistic wire fixture.

The transport never looks at text or golden ID. Negative cases must remain blocked
by actual evidence/rules, even with an overly optimistic synthetic response. This
proves engineering behavior, not semantic recognition by a real model.
"""

import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from test_p4a_matrix import fields
from test_p4a_postgres import add_booking
from test_p4a_postgres import hotel_portal as hotel_portal
from test_p4b_postgres import hybrid as hybrid
from test_p4b_postgres import nonhotel, verify_and_reaudit
from test_policies_postgres import policy_pg as policy_pg

from integrations.llm.compliance import SEMANTIC_TOPICS, ComplianceDeepSeekProvider
from tests.test_enterprise import action, login

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="Disposable PostgreSQL required"),
]
BASE = "/api/v1.5/p4b/v1"
CASES = json.loads(Path("evals/datasets/p4b/fix_golden_cases.json").read_text(encoding="utf-8"))[
    "cases"
]


class OptimisticWireFixture:
    """Same eight-topic fixture for every input; no lookup/classifier/model answers."""

    model = "test-model"

    def __init__(self):
        self.requests = []
        self.http_attempts = 0
        self.malformed = False

    async def analyze(self, request):
        self.requests.append(request)
        references = {
            "BUSINESS_PURPOSE": "business_related",
            "POLICY_APPLICABILITY": "policy_version",
            "AUTHORIZATION": "prior_approval",
            "DUPLICATE_SPLIT_CATEGORY": "duplicate_clear",
            "EVIDENCE_SUFFICIENCY": "business_related",
            "UNRESOLVED_QUESTIONS": "business_related",
            "FACT_POLICY_CONFLICT": "rule_results",
            "EXCEPTION_REASON": "business_related",
        }
        entries = request.facts["positive"]
        response = dict(
            semantic_findings=[
                dict(
                    finding_id=topic,
                    issue_type=topic,
                    conclusion="COMPLIANT",
                    evidence_refs=[request.evidence[0].chunk_id],
                    factual_basis=["positive." + references[topic]]
                    if entries[references[topic]]["status"] == "VERIFIED"
                    else ["trusted.employee_fact"],
                    uncertainty="",
                    explanation="Universal optimistic synthetic wire fixture",
                )
                for topic in sorted(SEMANTIC_TOPICS)
            ],
            missing_evidence=[],
            conflicts=[],
            exceptions=[],
            suggested_risk="LOW",
            recommended_action="CHECK_PASSED",
            explanation="Offline contract test only",
            provider="deepseek",
            model=self.model,
        )

        def handler(_):
            self.http_attempts += 1
            return httpx.Response(
                200,
                json=dict(
                    model=self.model,
                    choices=[
                        dict(
                            finish_reason="stop",
                            message=dict(
                                role="assistant",
                                content='{"approved":true}'
                                if self.malformed
                                else json.dumps(response),
                            ),
                        )
                    ],
                ),
            )

        provider = ComplianceDeepSeekProvider(
            api_key="synthetic-test-key",
            model=self.model,
            max_tokens=4096,
            transport=httpx.MockTransport(handler),
        )
        return await provider.analyze(request)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_independent_golden_graph_boundaries_and_positive_erp(hybrid, monkeypatch, case):
    env, _ = hybrid
    c, app, repo = env[:3]
    cat = app.state.audit_service.catalog
    provider = OptimisticWireFixture()
    cat.provider = provider
    add_booking(env)
    mutation = case.get("boundary_mutation")
    if mutation == "illegal_structure":
        provider.malformed = True
    if mutation == "no_evidence":
        monkeypatch.setattr("apps.api.compliance.rank_chunks", lambda *a, **k: [])
    if mutation == "version_conflict":
        original = cat.retriever.read

        def wrong_version(**kwargs):
            source = original(**kwargs)
            source["chunks"] = [
                ch.model_copy(update={"policy_version_id": "CONFLICT"}) for ch in source["chunks"]
            ]
            return source

        monkeypatch.setattr(cat.retriever, "read", wrong_version)
    if case["kind"] == "HOTEL":
        login(c)
        declaration = fields(amount=case["amount"]).model_dump(mode="json")
        declaration["notes"] = case["notes"]
        result = c.post(
            "/api/v1.5/p4a/v1/claims",
            json=dict(fields=declaration, stay_id="stay-1", idempotency_key=str(uuid4())),
        )
        assert result.status_code == 202, result.text
        detail = c.get("/api/v1.5/claims/" + result.json()["claim_id"]).json()
    else:
        detail = nonhotel(env, kind=case["kind"], amount=case["amount"], notes=case["notes"])
    if case.get("verify"):
        detail = verify_and_reaudit(env, detail, **case["verify"])
    report = detail["compliance_analysis"]
    assert report["rule_results"][0]["outcome"] == case["expected_rule"]
    if "expected_status" in case:
        assert report["compliance_status"] == case["expected_status"], report
    if "allowed_actions" in case:
        assert report["recommended_action"] in case["allowed_actions"]
    for missing in case["required_missing"]:
        assert report["positive_evidence"]["entries"][missing]["status"] != "VERIFIED"
        assert missing in report["positive_evidence"]["missing"]
    cid = detail["claim_id"]
    assert detail["stage"] == "FINANCE_REVIEW" and repo.erp_write_count(cid) == 0
    login(c, "finance")
    before = provider.http_attempts
    key = str(uuid4())
    approved = action(c, detail, "APPROVE", key=key)
    if case.get("expected_erp_count_after_finance"):
        assert report["missing_evidence"] == report["conflicts"] == report["exceptions"] == []
        assert report["positive_evidence"]["missing"] == []
        assert approved.status_code == 200, approved.text
        assert approved.json()["stage"] == "APPROVED"
        assert approved.json()["claim"]["erp_status"] == "SUCCEEDED"
        assert action(c, detail, "APPROVE", key=key).status_code == 200
        assert action(c, approved.json(), "APPROVE", key=str(uuid4())).status_code == 409
        assert repo.erp_write_count(cid) == 1
        for item in report["positive_evidence"]["entries"].values():
            if item["status"] == "VERIFIED":
                assert item["source_reference"] and item["verified_by"] and item["verified_at"]
    else:
        assert approved.status_code == 409, approved.text
        assert repo.erp_write_count(cid) == 0
    assert provider.http_attempts == before  # Finance and ERP never call the provider.
    if mutation in {"no_evidence", "version_conflict"}:
        assert not provider.requests and provider.http_attempts == 0
    if mutation == "illegal_structure":
        assert provider.http_attempts == 1  # Non-retryable structure error.
        assert report["provider_calls"][0]["validation_errors"][0]["loc"]


def test_partial_verification_preserves_individual_sources_and_invalidates_old_approval(hybrid):
    env, _ = hybrid
    c, app, repo = env[:3]
    add_booking(env)
    detail = verify_and_reaudit(env, nonhotel(env), full_days=1, lunches=1, dinners=1)
    cat = app.state.audit_service.catalog
    _, first = cat.store.read("FACTS", detail["claim_id"])
    result = c.post(
        BASE + f"/claims/{detail['claim_id']}/verify",
        json=dict(
            facts={"lunches": 0},
            expected_revision=1,
            source_reference="updated-meal-record-002",
            basis="Independent corrected meal count",
            confirmed=True,
            idempotency_key=str(uuid4()),
        ),
    )
    assert result.status_code == 200, result.text
    _, second = cat.store.read("FACTS", detail["claim_id"])
    assert (
        first["field_attestations"]["business_related"]
        == second["field_attestations"]["business_related"]
    )
    assert second["field_attestations"]["lunches"]["source_reference"] == "updated-meal-record-002"
    assert action(c, detail, "APPROVE").status_code == 409
    assert repo.erp_write_count(detail["claim_id"]) == 0
