"""Shadow topology, durable replay and formal side-effect failure isolation."""

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from agent.nodes.happy_path import build_happy_path
from agent.shadow import build_shadow_graph
from agent.state import ClaimState
from apps.api.repository import MockClaimRepository
from apps.api.workflow import AuditService
from domain.schemas import ClaimStatus, DecisionResult, ERPStatus, RunMode
from evals.dataset import DEFAULT_DATASET, load_dataset
from evals.metrics import summarize
from evals.runner import ProbeRetriever, catalog_for, run_evaluation, select_provider
from evals.shadow_mode import runner
from evals.shadow_mode.schema import ReferenceSource, ShadowResult
from evals.shadow_mode.store import ShadowStore
from integrations.documents.mock import MockDocumentParser
from integrations.erp.mock import MockERP


@pytest.fixture
def dataset():
    return load_dataset()


def run(tmp_path, **kwargs):
    return asyncio.run(runner.run_shadow(store_path=tmp_path / "shadow.sqlite3", **kwargs))


def graph_for(tmp_path, dataset, case):
    invoice_file = tmp_path / "invoices.json"
    invoice_file.write_text(json.dumps(case.invoices), encoding="utf-8")
    catalog = catalog_for(dataset, case)
    return build_shadow_graph(
        case.claim, erp_context={"employee_active": True,
                                 "employee": case.claim.employee.model_dump()},
        catalog=catalog, retriever=ProbeRetriever(catalog, case),
        provider=select_provider(case, None, None), parser=MockDocumentParser(invoice_file),
    )


def test_shadow_topology_has_no_business_actions(tmp_path, dataset):
    graph = graph_for(tmp_path, dataset, dataset.cases[0])
    assert set(graph.get_graph().nodes) == {
        "__start__", "__end__", "load_claim", "fetch_erp_context", "parse_documents",
        "rule_check", "retrieve_policy", "llm_analysis", "risk_assessment", "decision_router",
    }
    assert [(e.source, e.target) for e in graph.get_graph().edges
            if e.source == "decision_router"] == [("decision_router", "__end__")]
    assert graph.checkpointer is None


def test_live_factory_cannot_select_shadow_boundary():
    with pytest.raises(TypeError):
        build_happy_path(object(), shadow_boundary=True)
    with pytest.raises(TypeError):
        build_shadow_graph(object(), repository=object())


@pytest.mark.parametrize("input_mode", [RunMode.SHADOW, RunMode.LIVE])
def test_compiled_boundary_does_not_depend_on_mode_flag(tmp_path, dataset, input_mode):
    case = dataset.cases[0]
    graph = graph_for(tmp_path, dataset, case)
    state = ClaimState.model_validate(asyncio.run(graph.ainvoke(ClaimState(
        claim_id=case.case_id, run_id=uuid4(), mode=input_mode, revision=42,
    ))))
    assert state.decision.value == "APPROVE"
    assert state.revision == 42
    assert state.status == case.claim.status
    assert state.erp_status == ERPStatus.NOT_STARTED
    assert state.human_review is None


def test_golden_replay_and_formal_store_traps(tmp_path, dataset, monkeypatch):
    # Establish independent formal business data, then prohibit every business entry.
    formal = MockClaimRepository(store_path=str(tmp_path / "formal.sqlite3"))
    before = formal._db.iterdump()
    before = list(before)
    original_inputs = dataset.model_dump_json()
    calls = {"erp_write_count": 0, "claim_mutation_count": 0, "human_review_action_count": 0}

    def trap(key):
        def forbidden(*args, **kwargs):
            calls[key] += 1
            raise AssertionError("Formal side effect reached")
        return forbidden

    for name in ("write_erp",):
        monkeypatch.setattr(MockClaimRepository, name, trap("erp_write_count"))
    for name in ("save_operation", "append_events", "__init__"):
        monkeypatch.setattr(MockClaimRepository, name, trap("claim_mutation_count"))
    monkeypatch.setattr(MockERP, "__init__", trap("erp_write_count"))
    monkeypatch.setattr(MockERP, "write_decision", trap("erp_write_count"))
    for name in ("audit", "persist", "human_review", "resume"):
        monkeypatch.setattr(AuditService, name, trap("human_review_action_count"))
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "DO_NOT_SAVE_SENTINEL")
    try:
        result = run(tmp_path)
        assert result["shadow_status"] == "PASS"
        assert result["persisted_case_count"] == result["selected_case_count"] == 72
        assert result["side_effects"] == calls == dict.fromkeys(calls, 0)
        assert list(formal._db.iterdump()) == before
        assert dataset.model_dump_json() == original_inputs
        assert result["governance_certification"] == "PASS"
        assert all(result["governance"].values())
        assert "DO_NOT_SAVE_SENTINEL" not in json.dumps(result)
        store = ShadowStore(tmp_path / "shadow.sqlite3")
        try:
            assert store.db.execute("SELECT count(*) FROM shadow_results").fetchone()[0] == 72
            for row in result["cases"]:
                stored = store.get(result["shadow_run_id"], row["case_id"])
                assert stored.model_dump(mode="json") == row
                assert row["reference_source"] == "SYNTHETIC_RULE_LABEL"
                assert row["decision_match"] and row["policy_match"]
                assert row["evaluation_row"]["actual"]["mock_erp_writes"] == 0
                assert row["evaluation_row"]["actual"]["status"] == "NEW"
        finally:
            store.close()
        assert result["metrics"] == summarize([r["evaluation_row"] for r in result["cases"]])
        assert result["metrics"]["decision_accuracy"] == {
            "numerator": 72, "denominator": 72, "value": 1.0,
        }
        assert result["metrics"]["paid_api_calls"] == 0
    finally:
        formal.close()


def test_shadow_matches_m5a_semantics_without_erp_writes(tmp_path):
    shadow = run(tmp_path)
    baseline = asyncio.run(run_evaluation())
    keys = ("decision", "risk", "requires_human", "policy_evidence", "rule_results",
            "hard_gate_results", "hard_block", "llm_analysis")
    for observed, reference in zip(shadow["cases"], baseline["cases"], strict=True):
        assert observed["case_id"] == reference["case_id"]
        assert {k: observed["evaluation_row"]["actual"][k] for k in keys} == {
            k: reference["actual"][k] for k in keys
        }
    for key in ("decision_accuracy", "false_approval_unsafe_opportunity",
                "false_rejection_rate", "human_review_rate", "automation_rate",
                "policy_retrieval_accuracy", "hard_block_violations"):
        assert shadow["metrics"][key] == baseline["metrics"][key]


@pytest.mark.parametrize("case_id,node,attempts", [
    ("dependency_failure-01", "retrieve_policy", 1),
    ("dependency_failure-03", "llm_analysis", 1),
    ("dependency_failure-04", "llm_analysis", 1),
    ("dependency_failure-05", "llm_analysis", 3),
])
def test_dependency_failures_are_visible_without_formal_actions(tmp_path, case_id, node, attempts):
    report = run(tmp_path, case_ids=[case_id])
    assert report["shadow_status"] == "PASS"  # matches the expected negative-case safe route
    row = report["cases"][0]
    assert row["execution_status"] == "DEPENDENCY_FAILED"
    assert row["agent_decision"] == "HUMAN_REVIEW"
    assert row["requires_human"] is True
    assert node in row["failure_reason"]
    assert len(row["evaluation_row"]["llm_attempts"]) == attempts
    assert not any(row["side_effects"].values())
    assert report["governance_certification"] == "NOT_CERTIFIED"


def test_snapshot_of_completed_claim_retains_reference_and_revision(tmp_path, dataset):
    case = dataset.cases[0].model_copy(deep=True)
    case.claim.status = ClaimStatus.COMPLETED
    case.claim.active_run_id = uuid4()
    case.claim.decision_result = DecisionResult(
        claim_id=case.case_id, run_id=case.claim.active_run_id, decision="REJECT",
        risk_level="HIGH", hard_gate_passed=True, rule_results=[], policy_evidence=[],
        reason="Existing reference fixture; preserve even when agent disagrees",
        confidence=1.0, requires_human=False, decided_at=case.claim.updated_at,
    )
    before = case.claim.model_dump_json()
    graph = graph_for(tmp_path, dataset, case)
    # Caller changes after factory construction cannot change its detached inputs.
    case.claim.expense.amount = case.claim.expense.amount * 2
    state = ClaimState.model_validate(asyncio.run(graph.ainvoke(ClaimState(
        claim_id=case.case_id, run_id=uuid4(), mode=RunMode.SHADOW, revision=73,
    ))))
    assert state.status == ClaimStatus.COMPLETED
    assert state.revision == 73
    assert state.decision.value == "APPROVE"
    assert state.expense.amount < case.claim.expense.amount
    case.claim.expense.amount /= 2
    assert case.claim.model_dump_json() == before


def test_duplicate_run_requires_explicit_resume_and_completed_rows_stay_immutable(tmp_path):
    run_id = str(uuid4())
    first = run(tmp_path, run_id=run_id, case_ids=["compliant-01"])
    with pytest.raises(ValueError, match="Duplicate"):
        run(tmp_path, run_id=run_id, case_ids=["compliant-01"])
    resumed = run(tmp_path, run_id=run_id, case_ids=["compliant-01"], resume=True)
    assert first["cases"] == resumed["cases"]
    assert first["started_at"] == resumed["started_at"]
    second = run(tmp_path, case_ids=["compliant-01"])
    assert second["shadow_run_id"] != run_id
    assert first["cases"][0]["timestamp"] != second["cases"][0]["timestamp"]
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            store.save(ShadowResult.model_validate(first["cases"][0]))
        assert store.db.execute("SELECT count(*) FROM shadow_results").fetchone()[0] == 2
    finally:
        store.close()


@pytest.mark.parametrize("changed", ["selection", "dataset", "source"])
def test_resume_rejects_input_or_code_changes(tmp_path, monkeypatch, changed):
    run_id = str(uuid4())
    run(tmp_path, run_id=run_id, case_ids=["compliant-01"])
    kwargs = {"run_id": run_id, "resume": True, "case_ids": ["compliant-01"]}
    if changed == "selection":
        kwargs["case_ids"] = ["compliant-02"]
    elif changed == "dataset":
        path = tmp_path / "changed.json"
        path.write_bytes(DEFAULT_DATASET.read_bytes() + b"\n")
        kwargs["dataset_path"] = path
    else:
        monkeypatch.setattr(runner, "sha256", lambda p: "changed-source-hash")
    with pytest.raises(ValueError, match="manifest conflict"):
        run(tmp_path, **kwargs)


@pytest.mark.parametrize("phase", ["init", "save", "finish"])
def test_evaluation_store_failure_returns_shadow_fail_only(tmp_path, monkeypatch, phase):
    target = {"init": "__init__", "save": "save", "finish": "finish"}[phase]

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("DO_NOT_SAVE_RAW_SECRET")

    monkeypatch.setattr(ShadowStore, target, unavailable)
    report = run(tmp_path, case_ids=["compliant-01"])
    assert report["shadow_status"] == "FAIL"
    assert report["evaluation_store_failure"] == "OperationalError"
    assert "DO_NOT_SAVE_RAW_SECRET" not in json.dumps(report)
    assert not any(report["side_effects"].values())
    assert report["persisted_case_count"] == (1 if phase == "finish" else 0)


def test_store_failure_mid_run_then_resume_preserves_completed_results(tmp_path, monkeypatch):
    run_id = str(uuid4())
    original = ShadowStore.save
    count = 0

    def fail_second(self, result):
        nonlocal count
        count += 1
        if count == 2:
            raise sqlite3.OperationalError("disk full")
        original(self, result)

    monkeypatch.setattr(ShadowStore, "save", fail_second)
    cases = ["compliant-01", "compliant-02"]
    failed = run(tmp_path, run_id=run_id, case_ids=cases)
    assert failed["shadow_status"] == "FAIL"
    assert failed["persisted_case_count"] == 1
    monkeypatch.setattr(ShadowStore, "save", original)
    resumed = run(tmp_path, run_id=run_id, case_ids=cases, resume=True)
    assert resumed["shadow_status"] == "PASS"
    assert resumed["cases"][0] == failed["cases"][0]
    assert resumed["persisted_case_count"] == 2


def test_abrupt_process_restart_resumes_only_unfinished_cases(tmp_path):
    run_id = str(uuid4())
    store_path = tmp_path / "shadow.sqlite3"
    script = '''
import asyncio, os, sys
from evals.shadow_mode.store import ShadowStore
from evals.shadow_mode.runner import run_shadow
original = ShadowStore.save
def crash(self, result):
    original(self, result)
    os._exit(23)
ShadowStore.save = crash
asyncio.run(run_shadow(store_path=sys.argv[1], run_id=sys.argv[2],
                      case_ids=["compliant-01", "compliant-02"]))
'''
    child = subprocess.run([sys.executable, "-c", script, str(store_path), run_id],
                           capture_output=True, timeout=30, env=os.environ.copy())
    assert child.returncode == 23, child.stderr.decode()
    store = ShadowStore(store_path)
    try:
        original = store.get(run_id, "compliant-01").model_dump(mode="json")
        assert store.get(run_id, "compliant-02") is None
    finally:
        store.close()
    resumed = run(tmp_path, run_id=run_id, resume=True,
                  case_ids=["compliant-01", "compliant-02"])
    assert resumed["shadow_status"] == "PASS"
    assert resumed["cases"][0] == original
    assert not any(resumed["side_effects"].values())


def test_store_cannot_open_formal_database(tmp_path):
    path = tmp_path / "formal.sqlite3"
    formal = MockClaimRepository(store_path=str(path))
    formal.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="business database"):
        asyncio.run(runner.run_shadow(store_path=path, case_ids=["compliant-01"]))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert not Path(str(path) + ".lock").exists()


def test_store_excludes_concurrent_runner(tmp_path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    try:
        with store.exclusive():
            report = run(tmp_path, case_ids=["compliant-01"])
            assert report["shadow_status"] == "FAIL"
            assert report["evaluation_store_failure"] == "BlockingIOError"
    finally:
        store.close()


@pytest.mark.parametrize("kwargs", [
    {"case_ids": []}, {"case_ids": ["unknown"]}, {"run_id": "bad"}, {"resume": True},
    {"resume": True, "run_id": str(uuid4())},
])
def test_invalid_selection_or_resume_rejected_before_analysis(tmp_path, kwargs):
    with pytest.raises(ValueError):
        run(tmp_path, **kwargs)


def test_unexpected_graph_failure_is_recorded_and_does_not_stop_other_cases(tmp_path, monkeypatch):
    original = runner.build_shadow_graph
    calls = 0

    def fail_first(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("DO_NOT_SAVE_RAW_SECRET")
        return original(*args, **kwargs)

    monkeypatch.setattr(runner, "build_shadow_graph", fail_first)
    result = run(tmp_path, case_ids=["compliant-01", "compliant-02"])
    assert result["shadow_status"] == "FAIL"
    assert result["persisted_case_count"] == 2
    failed, completed = result["cases"]
    assert failed["execution_status"] == "FAILED"
    assert failed["failure_reason"] == "RuntimeError"
    assert failed["agent_decision"] is None and failed["decision_match"] is False
    assert completed["execution_status"] == "COMPLETED"
    assert result["metrics"]["decision_accuracy"]["value"] == 0.5
    assert "DO_NOT_SAVE_RAW_SECRET" not in json.dumps(result)


def test_cli_preserves_existing_report_and_rejects_paid_flags(tmp_path):
    output = tmp_path / "report.json"
    assert runner.main(["--store", str(tmp_path / "shadow.sqlite3"), "--output", str(output),
                        "--case", "compliant-01"]) == 0
    before = output.read_bytes()
    with pytest.raises(SystemExit):
        runner.main(["--output", str(output)])
    assert output.read_bytes() == before
    with pytest.raises(SystemExit):
        runner.main(["--live-deepseek"])


def test_reference_provenance_enum_does_not_invent_human_labels():
    assert {value.value for value in ReferenceSource} == {
        "SYNTHETIC_RULE_LABEL", "EXPERT_REVIEWED", "HISTORICAL_HUMAN_DECISION",
    }


def test_formal_approval_remains_usable_after_shadow_store_failure(tmp_path, monkeypatch, client):
    claim_id = "CLM-2026-00001"
    before = client.get(f"/claims/{claim_id}").json()
    service = client.app.state.audit_service
    before_events = service.repository.events(claim_id)

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("store unavailable")

    monkeypatch.setattr(ShadowStore, "save", unavailable)
    result = run(tmp_path, case_ids=["compliant-01"])
    assert result["shadow_status"] == "FAIL"
    assert client.get(f"/claims/{claim_id}").json() == before
    assert service.repository.events(claim_id) == before_events
    assert service.repository.erp_write_count(claim_id) == 0
    response = client.post(f"/claims/{claim_id}/audit", json={
        "idempotency_key": "formal-after-shadow-failure", "mode": "LIVE",
    })
    assert response.status_code == 202
    assert response.json()["status"] == "COMPLETED"
    assert response.json()["decision_result"]["decision"] == "APPROVE"
    assert service.repository.erp_write_count(claim_id) == 1
