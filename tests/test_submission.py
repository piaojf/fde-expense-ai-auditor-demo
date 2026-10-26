import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.config import Settings
from apps.api.main import create_app
from apps.api.repository import MockClaimRepository
from domain.submission import CreateClaimRequest


def payload(**updates):
    return (
        dict(
            idempotency_key="ui-p1-test",
            applicant_name="新申请人",
            department="华南销售部",
            position="客户经理",
            category="HOTEL",
            amount="600.00",
            incurred_on="2026-09-20",
            purpose="客户拜访",
            city="深圳",
            trip_start="2026-09-20",
            trip_end="2026-09-21",
            nights=1,
            seller="深圳测试酒店",
            notes="",
            **updates,
        )
        if not updates
        else {**payload(), **updates}
    )


def submit(client, **updates):
    response = client.post("/api/v1.1/claims", json=payload(**updates))
    assert response.status_code == 202, response.text
    return response.json()


def test_dynamic_approve_persist_replay_restart(tmp_path):
    path = str(tmp_path / "business.sqlite3")
    repo = MockClaimRepository(store_path=path)
    settings = Settings(app_env="test", _env_file=None)
    with TestClient(create_app(settings, repository=repo)) as c:
        body = submit(c)
        claim = body["claim"]
        cid = claim["claim_id"]
        assert (
            len(cid) > 35
            and cid
            not in json.loads(Path("apps/api/mock_data/claims.json").read_text(encoding="utf-8"))[
                0
            ].values()
        )
        assert claim["expense"]["amount"] == "600.00"
        assert claim["decision_result"]["decision"] == "APPROVE"
        assert claim["erp_status"] == "SUCCEEDED"
        assert body["trip_days"] == 2 and body["submission"]["nights"] == 1
        assert body["llm_analysis"]["provider"] == "mock"
        assert len(body["erp_effects"]) == 1
        assert submit(c)["claim"]["claim_id"] == cid
        assert repo.erp_write_count(cid) == 1
        events = repo.events(cid)
        assert sum(e.event_type == "CLAIM_CREATED" for e in events) == 1
        assert {
            "RULES_CHECKED",
            "POLICY_RETRIEVED",
            "LLM_ANALYSIS_COMPLETED",
            "RISK_ASSESSED",
            "DECISION_MADE",
        } <= {e.event_type for e in events}
        assert c.post("/api/v1.1/claims", json=payload(amount="601.00")).status_code == 409
    repo.close()
    repo = MockClaimRepository(store_path=path)
    with TestClient(create_app(settings, repository=repo)) as c:
        assert submit(c)["claim"]["claim_id"] == cid
        assert repo.erp_write_count(cid) == 1
        assert c.get(f"/api/v1.1/claims/{cid}").json()["submission"]["seller"] == "深圳测试酒店"
    repo.close()


def test_dynamic_hard_block(client):
    body = submit(client, amount="780.00")
    cid = body["claim"]["claim_id"]
    dr = body["claim"]["decision_result"]
    assert dr["decision"] == "REJECT" and dr["risk_level"] == "HIGH"
    assert dr["rule_results"][0]["severity"] == "BLOCK"
    assert body["erp_effects"] == []
    op = body["operation"]
    r = client.post(
        f"/claims/{cid}/human-review",
        json={
            "run_id": op["run_id"],
            "expected_revision": op["revision"],
            "decision": "APPROVE",
            "reviewer_id": "FIN",
            "comment": "override",
            "idempotency_key": "bad-override",
        },
    )
    assert r.status_code == 409


def test_dynamic_ambiguity_human_resume(client):
    body = submit(client, notes="航班取消，住宿延长需核实")
    cid = body["claim"]["claim_id"]
    op = body["operation"]
    assert body["claim"]["status"] == "PENDING_HUMAN"
    assert body["erp_effects"] == []
    review = client.post(
        f"/claims/{cid}/human-review",
        json={
            "run_id": op["run_id"],
            "expected_revision": op["revision"],
            "decision": "APPROVE",
            "reviewer_id": "FIN",
            "comment": "已核实行程说明",
            "idempotency_key": "review",
        },
    )
    assert review.status_code == 200, review.text
    request = {
        "run_id": op["run_id"],
        "expected_revision": review.json()["revision"],
        "idempotency_key": "resume",
    }
    assert client.post(f"/claims/{cid}/resume", json=request).status_code == 202
    assert client.post(f"/claims/{cid}/resume", json=request).status_code == 202
    result = client.get(f"/api/v1.1/claims/{cid}").json()
    assert result["claim"]["decision_result"]["decision"] == "APPROVE"
    assert result["claim"]["erp_status"] == "SUCCEEDED"
    assert len(result["erp_effects"]) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"amount": "0.00"},
        {"amount": "-1.00"},
        {"amount": 1.23},
        {"amount": "1.001"},
        {"amount": "NaN"},
        {"incurred_on": "2026-02-30"},
        {"incurred_on": "2099-01-01"},
        {"trip_end": "2026-09-19"},
        {"nights": None},
        {"nights": 0},
        {"nights": True},
        {"category": "OTHER", "nights": 1},
        {"category": "BAD"},
        {"city": "深圳市"},
        {"applicant_name": " "},
        {"purpose": ""},
        {"position": ""},
        {"seller": ""},
        {"decision": "APPROVE"},
        {"mode": "SHADOW"},
    ],
)
def test_invalid_submission_never_creates(client, change):
    before = client.get("/dashboard/metrics").json()
    r = client.post("/api/v1.1/claims", json=payload(**change))
    assert r.status_code == 422, r.text
    assert all("input" not in e for e in r.json()["error"]["details"]["issues"])
    assert client.get("/dashboard/metrics").json() == before


def test_nonhotel_no_fake_nights_and_unknown_policy(client):
    body = submit(client, category="MEAL", nights=None)
    assert body["submission"]["nights"] is None
    assert body["claim"]["decision_result"]["decision"] == "HUMAN_REVIEW"
    assert body["claim"]["decision_result"]["rule_results"][0]["outcome"] == "UNKNOWN"


def test_parallel_creation_reserves_one_row(tmp_path):
    path = str(tmp_path / "concurrency.sqlite3")
    repos = [MockClaimRepository(store_path=path) for _ in range(4)]
    request = CreateClaimRequest.model_validate(payload())
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = list(pool.map(lambda repo: repo.create_submission(request), repos))
        assert len({c.claim_id for c in claims}) == 1
        assert len(repos[0].events(claims[0].claim_id)) == 1
    finally:
        for repo in repos:
            repo.close()


def test_parallel_http_no_duplicate_workflow(client):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: submit(client), range(4)))
    assert len({r["claim"]["claim_id"] for r in results}) == 1
    assert all(len(r["erp_effects"]) == 1 for r in results)


def test_v1_root_contract_byte_equivalent(client):
    assert client.get("/openapi.json").json() == json.loads(
        Path("docs/openapi.json").read_text(encoding="utf-8")
    )
    schema = client.get("/api/v1.1/openapi.json").json()
    assert schema["info"]["version"] == "1.1.0"
    assert set(schema["paths"]) == {"/claims", "/claims/{claim_id}"}


def test_submission_survives_failure_before_audit_start(tmp_path):
    repo = MockClaimRepository(store_path=str(tmp_path / "failure.sqlite3"))
    app = create_app(Settings(app_env="test", _env_file=None), repository=repo)
    original = app.state.audit_service.audit

    async def fail(*args):
        raise RuntimeError("simulated worker failure before audit acceptance")

    app.state.audit_service.audit = fail
    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.post("/api/v1.1/claims", json=payload()).status_code == 500
        created = [claim for claim in repo.list() if repo.submission(claim.claim_id)]
        assert len(created) == 1 and created[0].status == "NEW"
        app.state.audit_service.audit = original
        assert submit(c)["claim"]["claim_id"] == created[0].claim_id
        assert repo.erp_write_count(created[0].claim_id) == 1
    repo.close()


def test_mutated_declaration_cannot_bypass_document_gate(client):
    repo = client.app.state.repository
    claim = repo.create_submission(CreateClaimRequest.model_validate(payload()))
    claim.documents[0].sha256 = "0" * 64
    with repo._db:
        repo._db.execute(
            "UPDATE claims SET body=? WHERE id=?", (claim.model_dump_json(), claim.claim_id)
        )
    body = submit(client)
    assert body["claim"]["decision_result"]["hard_gate_passed"] is False
    assert body["claim"]["decision_result"]["decision"] == "REJECT"
    assert body["erp_effects"] == []
