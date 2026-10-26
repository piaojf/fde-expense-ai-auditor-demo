"""Failure recovery gates using the unchanged HTTP contract and real SQLite saver."""

import asyncio
from contextlib import contextmanager
from datetime import datetime
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent.state import ClaimState
from apps.api.config import Settings
from apps.api.main import create_app
from apps.api.repository import MockClaimRepository
from domain.schemas import LLMAnalysis, RiskLevel
from integrations.erp.mock import MockERP
from integrations.faults import FailureMode
from integrations.llm.mock import MockLLMProvider
from integrations.retry import DependencyFailure, RetryPolicy, call_with_retry
from rag.retrieval.memory import InMemoryPolicyRetriever, PolicyCatalog

C6, C4 = "CLM-2026-00006", "CLM-2026-00004"


async def no_wait(seconds):
    pass


@contextmanager
def api(repo, **adapters):
    app = create_app(Settings(app_env="test", _env_file=None), repo, sleeper=no_wait, **adapters)
    with TestClient(app) as client:
        yield client


def audit(client, claim=C6):
    response = client.post(f"/claims/{claim}/audit", json={"idempotency_key": "m3-audit"})
    assert response.status_code == 202
    return response.json()


def resume_body(op, key="m3-resume"):
    return dict(run_id=op["run_id"], expected_revision=op["revision"], idempotency_key=key)


def kinds(repo, claim=C6):
    return [e.event_type for e in repo.events(claim)]


async def snapshot(service, op, claim=C6):
    context = service.repository.run_context(claim, op["run_id"])
    async with service.graph_session() as graph:
        return await graph.aget_state({"configurable": {
            "thread_id": context["thread_id"], "checkpoint_id": context["checkpoint_id"],
        }})


@pytest.mark.parametrize("mode", [FailureMode.TIMEOUT_ONCE, FailureMode.TEMPORARY_ERROR,
                                  FailureMode.SUCCESS_THEN_RESPONSE_LOST])
def test_erp_transient_or_unknown_outcome_success_once(mode):
    repo = MockClaimRepository()
    erp = MockERP(repo, mode)
    with api(repo, erp=erp) as client:
        op = audit(client)
        assert op["status"] == "COMPLETED"
        result = op["decision_result"]
        assert result["decision"] == "APPROVE" and result["risk_level"] == "LOW"
        assert result["hard_gate_passed"] and result["rule_results"][0]["outcome"] == "PASS"
        assert result["rule_results"][0]["actual"] == "650.00"
        assert result["rule_results"][0]["limit"] == "700.00"
        assert [p["version"] for p in result["policy_evidence"]] == ["2026"]
        assert erp.faults.calls == 2 and repo.erp_write_count(C6) == 1
        assert client.get(f"/claims/{C6}").json()["erp_status"] == "SUCCEEDED"
        assert client.get(f"/claims/{C6}").json()["employee"]["name"] == "王强"
        assert {"DEPENDENCY_CALL_FAILED", "RETRY_SCHEDULED", "RETRY_ATTEMPTED",
                "DEPENDENCY_RECOVERED", "ERP_UPDATED", "WORKFLOW_COMPLETED"} <= set(kinds(repo))
        assert ("IDEMPOTENT_REPLAY" in kinds(repo)) == (
            mode == FailureMode.SUCCESS_THEN_RESPONSE_LOST
        )
        effects = repo.erp_effects(C6)
        assert len(effects) == 1
        assert effects[0]["idempotency_key"] == f"{C6}:{op['run_id']}:write_decision"
        assert effects[0]["operation"] == "write_decision"
        assert datetime.fromisoformat(effects[0]["timestamp"]).tzinfo is not None
    repo.close()


@pytest.mark.parametrize("lost_response", [False, True])
def test_durable_recovery_restarts_at_failed_node_and_duplicates_are_idempotent(tmp_path,
                                                                            lost_response):
    store = str(tmp_path / "store.sqlite3")
    repo = MockClaimRepository(store_path=store)
    erp = MockERP(repo, FailureMode.TIMEOUT_ALWAYS)
    if lost_response:
        # Commit succeeded, but all acknowledgements (including replay) were lost.
        class LostEveryResponse(MockERP):
            async def write_decision(self, result, **kwargs):
                await super().write_decision(result, **kwargs)
                raise TimeoutError("Lost every response")
        erp = LostEveryResponse(repo)
    with api(repo, erp=erp) as client:
        op = audit(client)
        assert op["status"] == "PENDING_ERP" and erp.faults.calls == 3
        context = repo.run_context(C6, op["run_id"])
        assert context["recovery_status"] == "RECOVERY_PENDING"
        assert context["recoverable"] and context["recovery_required"]
        checkpoint = asyncio.run(snapshot(client.app.state.audit_service, op))
        assert checkpoint.next == ("erp_recovery",)
        assert any(task.interrupts for task in checkpoint.tasks)
        state = ClaimState.model_validate(checkpoint.values)
        assert state.audit_context["erp_unresolved"] and state.erp_status == "FAILED"
        assert state.audit_context["erp_attempts"] == 3
        before = [e.model_dump(mode="json") for e in repo.events(C6)]
        assert "RETRY_EXHAUSTED" in kinds(repo) and "RECOVERY_REQUIRED" in kinds(repo)
        assert "WORKFLOW_COMPLETED" not in kinds(repo)
        assert repo.erp_write_count(C6) == int(lost_response)
    repo.close()
    repo = MockClaimRepository(store_path=store)
    erp, provider, retriever = MockERP(repo), MockLLMProvider(), InMemoryPolicyRetriever()
    with api(repo, erp=erp, provider=provider, retriever=retriever) as client:
        assert repo.run_context(C6, op["run_id"])["checkpoint_id"] == context["checkpoint_id"]
        stale = client.post(f"/claims/{C6}/resume", json={**resume_body(op),
                                                       "expected_revision": op["revision"] + 1})
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "REVISION_CONFLICT"
        final = client.post(f"/claims/{C6}/resume", json=resume_body(op))
        assert final.status_code == 202 and final.json()["status"] == "COMPLETED"
        assert final.json()["revision"] > op["revision"]
        assert provider.faults.calls == retriever.faults.calls == 0
        assert repo.erp_write_count(C6) == 1 and erp.faults.calls == 1
        assert not repo.run_context(C6, op["run_id"])["recoverable"]
        assert "WORKFLOW_RECOVERY_RESUMED" in kinds(repo)
        assert ("IDEMPOTENT_REPLAY" in kinds(repo)) == lost_response
        assert [e.model_dump(mode="json") for e in repo.events(C6)][:len(before)] == before
        for kind in ("CLAIM_LOADED", "ERP_CONTEXT_FETCHED", "DOCUMENTS_PARSED",
                     "RULES_CHECKED", "POLICY_RETRIEVED", "LLM_ANALYSIS_COMPLETED",
                     "RISK_ASSESSED", "DECISION_MADE"):
            assert kinds(repo).count(kind) == 1
        assert client.post(f"/claims/{C6}/resume", json=resume_body(op)).json() == final.json()
        assert client.post(f"/claims/{C6}/resume",
                           json=resume_body(final.json(), "new-key")).status_code == 409
        assert repo.erp_write_count(C6) == 1
    repo.close()


def test_exhaustion_again_preserves_recovery_budget_and_revision():
    repo = MockClaimRepository()
    erp = MockERP(repo, FailureMode.TIMEOUT_ALWAYS)
    with api(repo, erp=erp) as client:
        op = audit(client)
        again = client.post(f"/claims/{C6}/resume", json=resume_body(op)).json()
        assert again["status"] == "PENDING_ERP" and again["revision"] > op["revision"]
        assert erp.faults.calls == 6
        assert client.post(f"/claims/{C6}/resume", json=resume_body(op)).json() == again
        erp.faults.mode = FailureMode.NONE
        final = client.post(f"/claims/{C6}/resume", json=resume_body(again, "second")).json()
        assert final["status"] == "COMPLETED" and repo.erp_write_count(C6) == 1
        assert kinds(repo).count("LLM_ANALYSIS_COMPLETED") == 1
    repo.close()


def test_human_decision_survives_erp_recovery_without_duplicate_review():
    repo = MockClaimRepository()
    erp = MockERP(repo, FailureMode.TIMEOUT_ALWAYS)
    with api(repo, erp=erp) as client:
        op = audit(client, C4)
        reviewed = client.post(f"/claims/{C4}/human-review", json={
            **resume_body(op), "idempotency_key": "review", "decision": "APPROVE",
            "reviewer_id": "finance", "comment": "Verified exception",
        }).json()
        pending = client.post(f"/claims/{C4}/resume", json=resume_body(reviewed)).json()
        assert pending["status"] == "PENDING_ERP"
        assert pending["decision_result"]["decision"] == "APPROVE"
        erp.faults.mode = FailureMode.NONE
        final = client.post(f"/claims/{C4}/resume", json=resume_body(pending, "recover")).json()
        assert final["status"] == "COMPLETED" and repo.erp_write_count(C4) == 1
        assert kinds(repo, C4).count("HUMAN_APPROVED") == 1
        assert kinds(repo, C4).count("LLM_ANALYSIS_COMPLETED") == 1
    repo.close()


@pytest.mark.parametrize("dependency_name", ["llm", "retriever"])
@pytest.mark.parametrize("mode,attempts,success", [
    (FailureMode.TIMEOUT_ONCE, 2, True), (FailureMode.TEMPORARY_ERROR, 2, True),
    (FailureMode.TIMEOUT_ALWAYS, 3, False),
])
def test_analysis_dependencies_retry_or_degrade_to_real_human_interrupt(dependency_name, mode,
                                                                     attempts, success):
    repo = MockClaimRepository()
    adapter = MockLLMProvider(mode) if dependency_name == "llm" else InMemoryPolicyRetriever(
        failure_mode=mode
    )
    kw = {"provider" if dependency_name == "llm" else "retriever": adapter}
    with api(repo, **kw) as client:
        op = audit(client)
        assert adapter.faults.calls == attempts
        assert op["status"] == ("COMPLETED" if success else "PENDING_HUMAN")
        assert repo.erp_write_count(C6) == int(success)
        if not success:
            assert op["decision_result"]["decision"] == "HUMAN_REVIEW"
            snap = asyncio.run(snapshot(client.app.state.audit_service, op))
            assert snap.next == ("human_review",)
            if dependency_name == "retriever":
                assert snap.values["audit_context"]["retrieval_outcome"] == (
                    "RETRIEVAL_SYSTEM_FAILURE"
                )
            # Existing authority gate remains fail-closed for infrastructure errors.
            assert client.post(f"/claims/{C6}/human-review", json={
                **resume_body(op), "decision": "APPROVE", "reviewer_id": "finance",
                "comment": "Should not override missing reliable analysis",
            }).status_code == 409
        failed = [e for e in repo.events(C6) if e.event_type == "DEPENDENCY_CALL_FAILED"]
        assert failed and all(e.payload["dependency"] == dependency_name for e in failed)
    repo.close()


@pytest.mark.parametrize("mode", [FailureMode.INVALID_OUTPUT, FailureMode.INVALID_RESPONSE])
def test_invalid_llm_schema_does_not_retry_or_auto_approve(mode):
    repo = MockClaimRepository()
    provider = MockLLMProvider(mode)
    with api(repo, provider=provider) as client:
        op = audit(client)
        assert op["status"] == "PENDING_HUMAN"
        assert op["decision_result"]["decision"] == "HUMAN_REVIEW"
        assert provider.faults.calls == 1 and repo.erp_write_count(C6) == 0
        failed = next(e for e in repo.events(C6) if e.event_type == "DEPENDENCY_CALL_FAILED")
        assert failed.payload["error_type"] == "ValidationError"
        assert failed.payload["retryable"] is False
        assert "DEPENDENCY_NON_RETRYABLE" in kinds(repo) and "RETRY_SCHEDULED" not in kinds(repo)
    repo.close()


def test_no_policy_is_distinct_from_system_failure():
    repo = MockClaimRepository()
    catalog = PolicyCatalog([])
    with api(repo, catalog=catalog, retriever=InMemoryPolicyRetriever(catalog)) as client:
        op = audit(client)
        snap = asyncio.run(snapshot(client.app.state.audit_service, op))
        assert snap.values["audit_context"]["retrieval_outcome"] == "NO_POLICY_FOUND"
        assert "DEPENDENCY_CALL_FAILED" not in kinds(repo) and repo.erp_write_count(C6) == 0
    repo.close()


@pytest.mark.parametrize("code,retryable", [(429, True), (502, True), (503, True),
                                          (400, False), (401, False), (403, False), (404, False)])
def test_http_error_classification_has_bounded_attempts(code, retryable):
    error = httpx.HTTPStatusError("secret raw body", request=httpx.Request("POST", "http://mock"),
                                  response=httpx.Response(code))
    calls, sleeps = [], []

    async def call():
        calls.append(1)
        raise error

    async def sleep(delay):
        sleeps.append(delay)

    state = ClaimState(claim_id=C6, run_id=uuid4())
    with pytest.raises(DependencyFailure) as caught:
        asyncio.run(call_with_retry(state, "erp", "write_erp", call,
                                   RetryPolicy(backoff=0.25), sleeper=sleep))
    assert len(calls) == (3 if retryable else 1)
    assert sleeps == ([0.25, 0.5] if retryable else [])
    assert "secret raw body" not in str(caught.value.events)


@pytest.mark.parametrize("error", [ValueError("Invalid Request"), RuntimeError("Business Reject"),
                                   TypeError("Schema"), AssertionError("Programming error")])
def test_non_transient_errors_are_not_blindly_retried(error):
    assert not RetryPolicy().is_retryable(error)


@pytest.mark.parametrize("mode", [FailureMode.INVALID_RESPONSE])
def test_nonrecoverable_erp_failure_cannot_resume(mode):
    repo = MockClaimRepository()
    erp = MockERP(repo, mode)
    with api(repo, erp=erp) as client:
        op = audit(client)
        assert op["status"] == "FAILED" and erp.faults.calls == 1
        assert not repo.run_context(C6, op["run_id"])["recoverable"]
        assert client.post(f"/claims/{C6}/resume", json=resume_body(op)).status_code == 409
        assert repo.erp_write_count(C6) == 0
    repo.close()


def test_independent_retry_policies_and_sleeper():
    repo = MockClaimRepository()
    erp = MockERP(repo, FailureMode.TIMEOUT_ALWAYS)
    with api(repo, erp=erp, retry_policies={"erp": RetryPolicy(max_attempts=1)}) as client:
        assert audit(client)["status"] == "PENDING_ERP" and erp.faults.calls == 1
    repo.close()


@pytest.mark.parametrize("kwargs", [{"max_attempts": 0}, {"max_attempts": 101},
                                    {"backoff": -1}, {"backoff": float("inf")},
                                    {"max_backoff": -1}])
def test_invalid_retry_policy_is_rejected(kwargs):
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


def test_erp_ledger_returns_original_result_after_restart_and_detects_conflict(tmp_path):
    path = str(tmp_path / "store.sqlite3")
    repo = MockClaimRepository(store_path=path)
    result = repo.write_erp(C6, "APPROVED", "stable")
    repo.close()
    repo = MockClaimRepository(store_path=path)
    replay = repo.write_erp(C6, "APPROVED", "stable")
    assert replay == {**result, "idempotent_replay": True}
    assert repo.erp_write_count(C6) == 1
    for args in [(C6, "REJECTED", "stable"), (C4, "APPROVED", "stable")]:
        with pytest.raises(ValueError, match="idempotency conflict"):
            repo.write_erp(*args)
    assert repo.erp_write_count(C6) == 1
    repo.close()


def test_failure_audit_is_durable_sanitized_and_complete():
    repo = MockClaimRepository()
    with api(repo, erp=MockERP(repo, FailureMode.TIMEOUT_ALWAYS)) as client:
        audit(client)
        for event in repo.events(C6):
            if event.event_type in ("DEPENDENCY_CALL_FAILED", "RETRY_SCHEDULED",
                                    "RETRY_EXHAUSTED", "RECOVERY_REQUIRED"):
                assert {"dependency", "operation", "attempt", "error_type", "retryable",
                        "timestamp", "summary"} <= event.payload.keys()
        failed = [e for e in repo.events(C6) if e.event_type == "DEPENDENCY_CALL_FAILED"]
        assert [e.payload["attempt"] for e in failed] == [1, 2, 3]
    repo.close()


def test_llm_schema_contract_is_validated_even_for_model_construct():
    bad = LLMAnalysis.model_construct(summary="bad", suggested_risk=RiskLevel.LOW, confidence=2,
                                      provider="mock", model="bad")
    with pytest.raises(ValidationError):
        LLMAnalysis.model_validate(bad.model_dump())
    class BadProvider:
        async def analyze(self, request):
            return bad
    repo = MockClaimRepository()
    with api(repo, provider=BadProvider()) as client:
        assert audit(client)["status"] == "PENDING_HUMAN" and repo.erp_write_count(C6) == 0
    repo.close()


def test_nonretryable_error_during_recovery_is_terminal_not_another_interrupt():
    repo = MockClaimRepository()
    erp = MockERP(repo, FailureMode.TIMEOUT_ALWAYS)
    with api(repo, erp=erp) as client:
        op = audit(client)
        erp.faults.mode = FailureMode.INVALID_RESPONSE
        failed = client.post(f"/claims/{C6}/resume", json=resume_body(op)).json()
        assert failed["status"] == "FAILED" and erp.faults.calls == 4
        assert not repo.run_context(C6, op["run_id"])["recoverable"]
        assert client.post(f"/claims/{C6}/resume",
                           json=resume_body(failed, "second")).status_code == 409
        assert repo.erp_write_count(C6) == 0
    repo.close()


@pytest.mark.parametrize("dependency_name", ["llm", "retriever"])
def test_human_reject_can_resolve_degraded_analysis_without_approving(dependency_name):
    repo = MockClaimRepository()
    adapters = ({"provider": MockLLMProvider(FailureMode.TIMEOUT_ALWAYS)}
                if dependency_name == "llm" else
                {"retriever": InMemoryPolicyRetriever(failure_mode=FailureMode.TIMEOUT_ALWAYS)})
    with api(repo, **adapters) as client:
        op = audit(client)
        reviewed = client.post(f"/claims/{C6}/human-review", json={
            **resume_body(op), "idempotency_key": "reject-review", "decision": "REJECT",
            "reviewer_id": "finance", "comment": "Insufficient reliable analysis; reject",
        })
        assert reviewed.status_code == 200
        final = client.post(f"/claims/{C6}/resume", json=resume_body(reviewed.json())).json()
        assert final["status"] == "COMPLETED" and final["decision_result"]["decision"] == "REJECT"
        assert repo.erp_status(C6) == "REJECTED" and repo.erp_write_count(C6) == 1
        assert "DEPENDENCY_CALL_FAILED" in kinds(repo)
    repo.close()


def test_cancellation_propagates_without_retry():
    calls = []
    async def cancel():
        calls.append(1)
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(call_with_retry(ClaimState(claim_id=C6, run_id=uuid4()),
                                   "erp", "write_erp", cancel, RetryPolicy(), sleeper=no_wait))
    assert len(calls) == 1
