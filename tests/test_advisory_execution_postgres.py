"""First ERP effects revalidate evidence; committed effects only reconcile."""

import json
import os
from uuid import uuid4

import pytest
from advisory_support import manual, prepare
from test_advisory_postgres import advisory as advisory
from test_p4a_postgres import hotel_portal as hotel_portal
from test_policies_postgres import policy_pg as policy_pg

from integrations.erp.mock import MockERP
from integrations.faults import FailureMode
from integrations.retry import RetryPolicy
from tests.test_enterprise import action

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="Disposable PostgreSQL required")]


def waiting(advisory, *, model_failure=False, committed=False):
    env, provider = advisory
    c, app, repo = env[:3]
    if model_failure:
        provider.mode = "invalid_json"
    erp = MockERP(repo, failure_mode=FailureMode.SUCCESS_THEN_RESPONSE_LOST
                  if committed else FailureMode.TIMEOUT_ALWAYS)
    app.state.audit_service.graph_factory.keywords.update(
        erp=erp, retry_policies={"erp": RetryPolicy(max_attempts=1)})
    detail = prepare(env)
    assert manual(c, detail).status_code == 200
    first = action(c, detail, "APPROVE")
    assert first.status_code == 200, first.text
    assert first.json()["stage"] == "ERP_SYNC_PENDING"
    assert repo.erp_write_count(detail["claim_id"]) == int(committed)
    erp.faults.mode = FailureMode.NONE
    return first.json(), erp


def retry(c, detail, key=None):
    return c.post("/api/v1.5/claims/" + detail["claim_id"] + "/retry", json=dict(
        expected_revision=detail["revision"], idempotency_key=key or str(uuid4())))


def mutate(env, detail, mutation, monkeypatch):
    _, app, repo, store, users, _ = env
    cid = detail["claim_id"]
    catalog = app.state.audit_service.catalog
    if mutation.startswith("invoice_"):
        attachment = repo.invoice_store.evidence(cid)["attachment"]
        path = repo.invoice_store.verified_path(attachment)
        if mutation == "invoice_missing":
            path.unlink()
        else:
            path.write_bytes(path.read_bytes() + b"\nSYNTHETIC CHANGED ORIGINAL\n")
    elif mutation == "facts":
        record = catalog.store.read("FACTS", cid)[1]
        changed = {**record, "facts": {**record["facts"], "duplicate_clear": False}}
        with store.lock, store.db:
            catalog.store.append("FACTS", cid, changed)
    elif mutation == "source_unavailable":
        def fail(*args, **kwargs):
            raise ConnectionError("SYNTHETIC_POLICY_SOURCE_UNAVAILABLE")
        monkeypatch.setattr(catalog.retriever, "read", fail)
    elif mutation == "trusted_grade":
        fact = catalog.legacy.facts.resolve(
            "DEMO-CN", "EMPLOYEE", users["employee"]["id"], "2026-11-02")
        changed = {**fact, "id": str(uuid4()), "value": {"grade": "A"}}
        # Simulated corrupt master row in disposable SQLite; normal API rejects overlap.
        with store.lock, store.db:
            store.db.execute("INSERT INTO p4a_facts VALUES(?,?,?,?,?,?,?)", (
                changed["id"], "DEMO-CN", "EMPLOYEE", users["employee"]["id"],
                fact["effective_from"], fact["effective_to"], json.dumps(changed)))
    else:
        assert mutation == "manual_review"
        key = cid + ":" + detail["claim"]["active_run_id"]
        record = catalog.store.read("MANUAL_ADVISORY", key)[1]
        with store.lock, store.db:
            catalog.store.append("MANUAL_ADVISORY", key, {**record, "outcome": "ESCALATE"})


@pytest.mark.parametrize("model_failure", [False, True])
@pytest.mark.parametrize("boundary", ["entry", "adapter"])
@pytest.mark.parametrize("mutation", [
    "invoice_missing", "invoice_bytes", "facts", "source_unavailable", "trusted_grade",
    "manual_review",
])
def test_uncommitted_recovery_revalidates_execution_evidence(
    advisory, monkeypatch, model_failure, boundary, mutation,
):
    env, provider = advisory
    c, _, repo = env[:3]
    detail, erp = waiting(advisory, model_failure=model_failure)
    cid, run = detail["claim_id"], detail["claim"]["active_run_id"]
    review = repo.run_context(cid, run)["human_review"]
    report = detail["advisory_analysis"]
    calls = len(provider.requests)
    original = erp.write_decision
    attempted = []

    async def interleaved(*args, **kwargs):
        attempted.append(True)
        mutate(env, detail, mutation, monkeypatch)
        return await original(*args, **kwargs)

    if boundary == "entry":
        mutate(env, detail, mutation, monkeypatch)
    else:
        monkeypatch.setattr(erp, "write_decision", interleaved)
    result = retry(c, detail)
    assert result.status_code == 409, result.text
    assert result.json()["error"]["code"] == "FINANCE_EVIDENCE_INVALID", result.text
    assert bool(attempted) == (boundary == "adapter")
    assert repo.erp_write_count(cid) == 0
    current = c.get("/api/v1.5/claims/" + cid).json()
    assert current["stage"] == "MANUAL_RECOVERY_REQUIRED"
    assert current["advisory_analysis"]["binding"] == report["binding"]
    assert current["advisory_analysis"]["original_output"] == report["original_output"]
    assert repo.run_context(cid, run)["human_review"] == review
    assert len(provider.requests) == calls
    # A second key cannot turn a stale approval into a new payment.
    assert retry(c, current).status_code == 409
    assert repo.erp_write_count(cid) == 0


@pytest.mark.parametrize("model_failure", [False, True])
def test_unchanged_uncommitted_recovery_writes_once(advisory, model_failure):
    env, provider = advisory
    c, _, repo = env[:3]
    detail, _ = waiting(advisory, model_failure=model_failure)
    calls = len(provider.requests)
    key = str(uuid4())
    result = retry(c, detail, key)
    assert result.status_code == 200, result.text
    assert result.json()["stage"] == "APPROVED"
    assert repo.erp_write_count(detail["claim_id"]) == 1
    assert retry(c, detail, key).status_code == 200
    assert repo.erp_write_count(detail["claim_id"]) == 1
    assert len(provider.requests) == calls


@pytest.mark.parametrize("mutation", ["invoice_missing", "facts", "source_unavailable"])
def test_committed_response_loss_only_reconciles_existing_effect(advisory, monkeypatch, mutation):
    env, _ = advisory
    c, _, repo = env[:3]
    detail, _ = waiting(advisory, committed=True)
    effects = repo.erp_effects(detail["claim_id"])
    mutate(env, detail, mutation, monkeypatch)
    result = retry(c, detail)
    assert result.status_code == 200, result.text
    assert result.json()["stage"] == "APPROVED"
    assert repo.erp_effects(detail["claim_id"]) == effects
    assert repo.erp_write_count(detail["claim_id"]) == 1
