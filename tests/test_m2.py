import asyncio
import json
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent.graph import build_m2_graph
from agent.nodes.human_review import (
    human_decision_result,
    validate_resume_payload,
)
from agent.state import ClaimState
from apps.api.config import Settings
from apps.api.main import create_app
from apps.api.repository import MockClaimRepository
from apps.api.workflow import AuditService
from domain.schemas import AuditRequest, DecisionResult, HumanReview, LLMAnalysis
from integrations.documents.mock import MockDocumentParser
from integrations.erp.mock import MockERP
from rag.retrieval.memory import InMemoryPolicyRetriever, PolicyCatalog

C4, C5 = "CLM-2026-00004", "CLM-2026-00005"
PRE_EVENTS = [
    "CLAIM_LOADED",
    "ERP_CONTEXT_FETCHED",
    "DOCUMENTS_PARSED",
    "HARD_GATE_CHECKED",
    "RULES_CHECKED",
    "POLICY_RETRIEVED",
    "LLM_ANALYSIS_COMPLETED",
    "RISK_ASSESSED",
    "DECISION_MADE",
    "HUMAN_REVIEW_REQUIRED",
    "WORKFLOW_INTERRUPTED",
]


def audit(client, claim=C4, key="audit"):
    response = client.post(f"/claims/{claim}/audit", json={"idempotency_key": key})
    assert response.status_code == 202
    return response.json()


def review_body(operation, action="APPROVE", key="review"):
    return dict(
        run_id=operation["run_id"],
        expected_revision=operation["revision"],
        decision=action,
        reviewer_id="finance_reviewer_01",
        comment="航班取消材料有效，额外住宿具有合理业务原因，批准报销。"
        if action == "APPROVE"
        else "所提交证明无法确认该住宿与航班取消直接相关，拒绝此次异常费用。",
        idempotency_key=key,
    )


def resume_body(operation, key="resume"):
    return dict(
        run_id=operation["run_id"], expected_revision=operation["revision"], idempotency_key=key
    )


async def inspect(service, operation):
    async with service.graph_session() as graph:
        return await graph.aget_state({"configurable": {"thread_id": operation["run_id"]}})


@pytest.mark.parametrize("claim", [C4, C5])
def test_medium_true_interrupt_payload_state_and_no_erp(client, claim):
    operation = audit(client, claim)
    result = operation["decision_result"]
    assert operation["status"] == "PENDING_HUMAN" and operation["revision"] == 1
    assert result["hard_gate_passed"] and result["requires_human"]
    assert result["decision"] == "HUMAN_REVIEW" and result["risk_level"] == "MEDIUM"
    assert result["rule_results"][0]["outcome"] == "PASS"
    assert result["rule_results"][0]["severity"] == "BLOCK"
    assert result["rule_results"][0]["actual"] == "680.00"
    assert result["rule_results"][0]["limit"] == "700.00"
    assert [p["version"] for p in result["policy_evidence"]] == ["2026"]
    service = client.app.state.audit_service
    snapshot = asyncio.run(inspect(service, operation))
    assert snapshot.next == ("human_review",)
    assert any(t.interrupts for t in snapshot.tasks)
    payload = snapshot.tasks[0].interrupts[0].value
    assert payload["claim_id"] == claim and payload["thread_id"] == operation["run_id"]
    assert payload["actions"] == ["APPROVE", "REJECT"]
    state = ClaimState.model_validate(snapshot.values)
    assert state.audit_context["business_analysis"]["ambiguity"]
    assert state.llm_analysis.anomalies == [
        "STAY_OUTSIDE_APPROVED_TRIP_DATES",
        "FLIGHT_CANCELLATION_EXCEPTION",
    ]
    assert state.audit_context["trip_context"]["approved_to"] == "2026-09-22"
    assert state.audit_context["trip_context"]["stay_to"] == "2026-09-23"
    repo = client.app.state.repository
    assert repo.erp_status(claim) is None and repo.erp_write_count(claim) == 0
    assert [e.event_type for e in repo.events(claim)] == PRE_EVENTS
    assert all(g.outcome == "PASS" for g in state.hard_gate_results)


@pytest.mark.parametrize(
    ("claim", "action", "erp"), [(C4, "APPROVE", "APPROVED"), (C5, "REJECT", "REJECTED")]
)
def test_restart_review_resume_original_checkpoint_and_replays(tmp_path, claim, action, erp):
    settings = Settings(
        app_env="development", mock_store_path=str(tmp_path / "claims.db"), _env_file=None
    )
    with TestClient(create_app(settings)) as client:
        operation = audit(client, claim)
        repo = client.app.state.repository
        original = repo.run_context(claim, operation["run_id"])
        checkpoint = original["checkpoint_id"]
        original_events = [e.model_dump(mode="json") for e in repo.events(claim)]
    with TestClient(create_app(settings)) as client:
        repo = client.app.state.repository
        service = client.app.state.audit_service
        assert client.get(f"/claims/{claim}").json()["status"] == "PENDING_HUMAN"
        snapshot = asyncio.run(inspect(service, operation))
        assert snapshot.config["configurable"]["checkpoint_id"] == checkpoint
        assert any(t.interrupts for t in snapshot.tasks)
        body = review_body(operation, action)
        submitted = client.post(f"/claims/{claim}/human-review", json=body)
        assert submitted.status_code == 200
        assert submitted.json()["status"] == "PENDING_HUMAN"
        assert submitted.json()["revision"] == 2
        assert repo.erp_write_count(claim) == 0
        assert client.post(f"/claims/{claim}/human-review", json=body).json() == submitted.json()
        assert repo.run_context(claim, operation["run_id"])["checkpoint_id"] == checkpoint
        # A second process/application lifecycle also preserves the submitted review.
    with TestClient(create_app(settings)) as client:
        repo = client.app.state.repository
        service = client.app.state.audit_service
        resume = resume_body(submitted.json())
        completed = client.post(f"/claims/{claim}/resume", json=resume)
        assert completed.status_code == 202
        final = completed.json()
        assert final["run_id"] == operation["run_id"] and final["revision"] == 3
        assert final["status"] == "COMPLETED" and final["decision_result"]["decision"] == action
        assert repo.erp_status(claim) == erp and repo.erp_write_count(claim) == 1
        snapshot = asyncio.run(inspect(service, operation))
        state = ClaimState.model_validate(snapshot.values)
        assert not snapshot.next and not any(t.interrupts for t in snapshot.tasks)
        assert state.human_review.decision == action
        assert state.audit_context["resume_context"]["from_checkpoint_id"] == checkpoint
        assert state.audit_context["resume_context"]["thread_id"] == operation["run_id"]
        events = repo.events(claim)
        assert [e.model_dump(mode="json") for e in events[:11]] == original_events
        assert [e.event_type for e in events] == PRE_EVENTS + [
            "HUMAN_REVIEW_SUBMITTED",
            "WORKFLOW_RESUMED",
            "HUMAN_APPROVED" if action == "APPROVE" else "HUMAN_REJECTED",
            "ERP_UPDATED",
            "WORKFLOW_COMPLETED",
        ]
        for event in events[11:14]:
            assert event.payload["reviewer"] == "finance_reviewer_01"
            assert event.payload["action"] == action and event.payload["reason"]
        assert client.post(f"/claims/{claim}/resume", json=resume).json() == final
        assert client.post(f"/claims/{claim}/human-review", json=body).json() == submitted.json()
        assert len(repo.events(claim)) == 16 and repo.erp_write_count(claim) == 1
        assert (
            client.post(f"/claims/{claim}/resume", json=resume_body(final, "new")).status_code
            == 409
        )
    with TestClient(create_app(settings)) as client:
        assert client.post(f"/claims/{claim}/resume", json=resume).json() == final
        assert client.app.state.repository.erp_write_count(claim) == 1
        assert client.get(f"/claims/{claim}/audit-log").json()["total"] == 16


def test_resume_before_review_and_second_review_are_rejected(client):
    operation = audit(client)
    assert client.post(f"/claims/{C4}/resume", json=resume_body(operation)).status_code == 409
    submitted = client.post(f"/claims/{C4}/human-review", json=review_body(operation)).json()
    second = review_body(submitted, "REJECT", "second-review")
    assert client.post(f"/claims/{C4}/human-review", json=second).status_code == 409
    assert client.app.state.repository.erp_write_count(C4) == 0
    assert client.get(f"/claims/{C4}/audit-log").json()["total"] == 12


@pytest.mark.parametrize("operation", ["human-review", "resume"])
def test_wrong_run_revision_and_payload_validation(client, operation):
    initial = audit(client)
    body = review_body(initial) if operation == "human-review" else resume_body(initial)
    wrong = {**body, "expected_revision": 99}
    response = client.post(f"/claims/{C4}/{operation}", json=wrong)
    assert response.status_code == 409 and response.json()["error"]["code"] == "REVISION_CONFLICT"
    assert (
        client.post(f"/claims/{C4}/{operation}", json={**body, "run_id": str(uuid4())}).status_code
        == 404
    )
    assert (
        client.post(
            f"/claims/{C4}/{operation}", json={**body, "injected_state": {"decision": "APPROVE"}}
        ).status_code
        == 422
    )


def test_operation_scoped_keys_and_conflicting_review_are_rejected(client):
    operation = audit(client, key="shared-key")
    body = review_body(operation, key="shared-key")
    submitted = client.post(f"/claims/{C4}/human-review", json=body)
    assert submitted.status_code == 200
    conflict = client.post(f"/claims/{C4}/human-review", json={**body, "decision": "REJECT"})
    assert (
        conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    )
    resumed = client.post(f"/claims/{C4}/resume", json=resume_body(submitted.json(), "shared-key"))
    assert resumed.status_code == 202
    assert (
        client.post(f"/claims/{C4}/audit", json={"idempotency_key": "shared-key"}).json()
        == operation
    )


@pytest.mark.parametrize("claim", ["CLM-2026-001", "CLM-2026-00001"])
def test_human_review_non_interrupted_claim_is_rejected(client, claim):
    operation = audit(client, claim)
    before = client.get(f"/claims/{claim}").json()
    response = client.post(f"/claims/{claim}/human-review", json=review_body(operation))
    assert response.status_code == 409
    assert client.get(f"/claims/{claim}").json() == before
    if claim == "CLM-2026-001":
        assert operation["decision_result"]["decision"] == "REJECT"
        assert operation["decision_result"]["rule_results"][0]["outcome"] == "FAIL"
        assert client.app.state.repository.erp_write_count(claim) == 0


@pytest.mark.parametrize("decision", ["APPROVE", "HUMAN_REVIEW"])
def test_original_780_block_contract_gate_is_a_regression(client, decision):
    operation = audit(client, "CLM-2026-001")
    result = operation["decision_result"]
    result["rule_results"][0]["actual"] = "780.00"
    result.update(
        decision=decision,
        risk_level="LOW" if decision == "APPROVE" else "MEDIUM",
        requires_human=decision == "HUMAN_REVIEW",
        decided_at=None if decision == "HUMAN_REVIEW" else datetime.now(UTC),
    )
    with pytest.raises(ValidationError, match="BLOCK / FAIL cannot be overridden"):
        DecisionResult.model_validate(result)


@pytest.mark.parametrize("mutation", ["block", "hard_gate", "unknown", "policy"])
def test_human_approve_rechecks_deterministic_constraints(client, mutation):
    operation = audit(client)
    snapshot = asyncio.run(inspect(client.app.state.audit_service, operation))
    state = ClaimState.model_validate(snapshot.values)
    if mutation == "block":
        state.rule_results[0].outcome = "FAIL"
    elif mutation == "hard_gate":
        state.hard_gate_results[0].outcome = "FAIL"
    elif mutation == "unknown":
        state.rule_results[0].outcome = "UNKNOWN"
    else:
        state.policy_evidence = []
    review = HumanReview(
        decision="APPROVE",
        reviewer_id="finance_reviewer_01",
        comment="Approve",
        reviewed_at=datetime.now(UTC),
    )
    with pytest.raises(ValueError):
        human_decision_result(state, review, PolicyCatalog())


@pytest.mark.parametrize("field", ["run_id", "claim_id", "checkpoint_id", "revision", "review"])
def test_resume_envelope_must_match_persisted_checkpoint_and_review(client, field):
    operation = audit(client)
    client.post(f"/claims/{C4}/human-review", json=review_body(operation))
    repo = client.app.state.repository
    context = repo.run_context(C4, operation["run_id"])
    state = ClaimState.model_validate(
        asyncio.run(inspect(client.app.state.audit_service, operation)).values
    )
    payload = dict(
        claim_id=C4,
        run_id=operation["run_id"],
        checkpoint_id=context["checkpoint_id"],
        revision=context["revision"],
        review=context["human_review"],
    )
    wrong = {
        "run_id": str(uuid4()),
        "claim_id": C5,
        "checkpoint_id": "wrong",
        "revision": 99,
        "review": {**payload["review"], "decision": "REJECT"},
    }
    with pytest.raises(ValueError):
        validate_resume_payload(state, {**payload, field: wrong[field]}, context)


class CountingERP(MockERP):
    fetches = 0
    writes = 0

    async def fetch_claim_context(self, claim):
        self.fetches += 1
        return await super().fetch_claim_context(claim)

    async def write_decision(self, result, **kwargs):
        self.writes += 1
        return await super().write_decision(result, **kwargs)


class CountingParser(MockDocumentParser):
    calls = 0

    def parse(self, document):
        self.calls += 1
        return super().parse(document)


class CountingRetriever(InMemoryPolicyRetriever):
    calls = 0

    async def retrieve(self, *args, **kwargs):
        self.calls += 1
        return await super().retrieve(*args, **kwargs)


class CountingLLM:
    calls = 0

    async def analyze(self, request):
        self.calls += 1
        assert self.calls == 1, "LLM must not rerun after human decision"
        return LLMAnalysis(
            summary="Optimistic model; deterministic ambiguity still requires human review",
            suggested_risk="LOW",
            confidence=1,
            provider="mock",
            model="test",
        )


@pytest.mark.parametrize("action", ["APPROVE", "REJECT"])
def test_no_upstream_adapter_reexecution_and_human_precedence(client, action):
    repo = client.app.state.repository
    erp, parser, retriever, llm = (
        CountingERP(repo),
        CountingParser(),
        CountingRetriever(),
        CountingLLM(),
    )
    client.app.state.audit_service.graph_factory = partial(
        build_m2_graph, repo, erp=erp, parser=parser, retriever=retriever, provider=llm
    )
    operation = audit(client)
    assert operation["decision_result"]["risk_level"] == "MEDIUM"
    submitted = client.post(
        f"/claims/{C4}/human-review", json=review_body(operation, action)
    ).json()
    result = client.post(f"/claims/{C4}/resume", json=resume_body(submitted)).json()
    assert result["decision_result"]["decision"] == action
    assert (erp.fetches, parser.calls, retriever.calls, llm.calls, erp.writes) == (1, 2, 1, 1, 1)


def test_780_actual_api_claim_stays_blocked(tmp_path):
    fixtures = Path("apps/api/mock_data")
    claims = json.loads((fixtures / "claims.json").read_text(encoding="utf-8"))
    docs = json.loads((fixtures / "documents.json").read_text(encoding="utf-8"))
    for claim in claims:
        if claim["claim_id"] == C4:
            claim["expense"]["amount"] = "780.00"
    for doc in docs:
        if doc["claim_id"] == C4 and "expense" in doc:
            doc["expense"]["amount"] = "780.00"
    claim_path, doc_path = tmp_path / "claims.json", tmp_path / "docs.json"
    claim_path.write_text(json.dumps(claims), encoding="utf-8")
    doc_path.write_text(json.dumps(docs), encoding="utf-8")
    repo = MockClaimRepository(claim_path)
    app = create_app(Settings(app_env="test", _env_file=None), repo)
    app.state.audit_service.graph_factory = partial(
        build_m2_graph, repo, parser=MockDocumentParser(doc_path)
    )
    with TestClient(app) as client:
        operation = audit(client)
        assert operation["status"] == "COMPLETED"
        result = operation["decision_result"]
        assert result["risk_level"] == "HIGH" and result["decision"] == "REJECT"
        assert result["rule_results"][0]["actual"] == "780.00"
        assert (
            client.post(f"/claims/{C4}/human-review", json=review_body(operation)).status_code
            == 409
        )
        assert not any(e.event_type == "WORKFLOW_INTERRUPTED" for e in repo.events(C4))
        assert repo.erp_write_count(C4) == 0
    repo.close()


def test_official_saver_connections_close_and_thread_checkpoint_persists(tmp_path):
    repo = MockClaimRepository(store_path=str(tmp_path / "store.db"))
    service = AuditService(repo)
    operation = asyncio.run(service.audit(C4, AuditRequest(idempotency_key="direct")))
    assert Path(repo.checkpoint_path).exists()
    assert asyncio.run(inspect(service, operation.model_dump(mode="json"))).next == (
        "human_review",
    )
    repo.close()
