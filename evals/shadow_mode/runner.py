"""Offline Synthetic + Mock shadow replay. No live provider, ERP or public API entry."""

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import UUID, uuid4

from agent.shadow import build_shadow_graph
from agent.state import ClaimState
from domain.schemas import RunMode
from evals.dataset import DEFAULT_DATASET, ROOT, load_dataset
from evals.metrics import summarize
from evals.runner import (
    ProbeRetriever,
    catalog_for,
    governance,
    observe_state,
    select_provider,
    sha256,
)
from evals.shadow_mode.schema import ReferenceSource, ShadowResult, timestamp
from evals.shadow_mode.store import ShadowStore
from integrations.documents.mock import MockDocumentParser

DEFAULT_STORE = ROOT / "output/m5b-local/shadow-results.sqlite3"


def run_manifest(dataset_path, dataset, cases):
    names = (
        "agent/nodes/happy_path.py", "agent/shadow.py", "agent/routing/decision.py",
        "rules/hard_gates.py", "rules/travel_rules.py", "rag/retrieval/memory.py",
        "integrations/llm/mock.py", "integrations/documents/mock.py", "integrations/retry.py",
        "evals/dataset.py", "evals/runner.py", "evals/metrics/__init__.py",
        "evals/shadow_mode/runner.py", "evals/shadow_mode/schema.py", "evals/shadow_mode/store.py",
    )
    return {
        "dataset_version": f"{dataset.dataset_id}:{sha256(dataset_path)}",
        "policy_fixture_sha256": sha256(ROOT / dataset.policy_fixture),
        "prompt_version": sha256(ROOT / "integrations/llm/mock.py"),
        "case_ids": [c.case_id for c in cases],
        "mode": "shadow-synthetic-mock",
        "source_sha256": {name: sha256(ROOT / name) for name in names},
    }


async def execute_case(dataset, case, run_id, manifest, *, adapters=None, retry_policies=None):
    if adapters is None:
        provider = select_provider(case, None, None)
        catalog = catalog_for(dataset, case)
        retriever = ProbeRetriever(catalog, case)
    else:
        catalog, retriever, provider = adapters
    snapshot = case.claim.model_copy(deep=True)
    before = snapshot.model_dump_json()
    actual, error, valid, probe = {}, None, False, None
    effects = {"erp_write_count": 0, "claim_mutation_count": 0, "human_review_action_count": 0}
    elapsed = 0.0
    try:
        with TemporaryDirectory(prefix="fde-shadow-input-") as temporary:
            invoice_file = Path(temporary) / "invoices.json"
            invoice_file.write_text(json.dumps(case.invoices), encoding="utf-8")
            graph = build_shadow_graph(
                snapshot,
                erp_context={
                    "source": "synthetic-snapshot", "claim_id": snapshot.claim_id,
                    "employee_active": True, "duplicate_confirmed": False,
                    "employee": snapshot.employee.model_dump(mode="json"),
                },
                catalog=catalog, retriever=retriever, provider=provider,
                parser=MockDocumentParser(invoice_file),
                retry_policies=retry_policies,
            )
            if set(graph.get_graph().nodes) != {
                "__start__", "__end__", "load_claim", "fetch_erp_context", "parse_documents",
                "rule_check", "retrieve_policy", "llm_analysis", "risk_assessment",
                "decision_router",
            } or graph.checkpointer is not None:
                raise ValueError("SHADOW_TOPOLOGY_VIOLATION")
            start = perf_counter()
            try:
                state = ClaimState.model_validate(await graph.ainvoke(ClaimState(
                    claim_id=snapshot.claim_id, run_id=UUID(run_id), mode=RunMode.SHADOW,
                    status=snapshot.status,
                )))
                events = state.audit_context.get("events", [])
                effects["erp_write_count"] = sum(
                    e["event_type"] == "ERP_UPDATED" for e in events
                )
                effects["human_review_action_count"] = sum(
                    e["event_type"] in (
                        "HUMAN_REVIEW_REQUIRED", "HUMAN_REVIEW_SUBMITTED", "HUMAN_APPROVED",
                        "HUMAN_REJECTED", "WORKFLOW_INTERRUPTED", "WORKFLOW_RESUMED",
                    ) for e in events
                ) + int(state.human_review is not None)
                effects["claim_mutation_count"] = int(
                    before != snapshot.model_dump_json() or state.status != snapshot.status
                    or state.revision != 0 or state.erp_status.value != "NOT_STARTED"
                )
                actual = observe_state(state, erp_write_count=effects["erp_write_count"])
                valid = True
                probe = getattr(retriever, "probe_result", None)
            finally:
                elapsed = round((perf_counter() - start) * 1000, 3)
    except Exception as exc:
        # Never persist raw exception messages, prompts or credentials.
        error = type(exc).__name__
    row = {
        "evaluation_run_id": run_id, "case_id": case.case_id,
        "category": case.category, "risk_category": case.risk_category,
        "expected": {
            "decision": case.expected_decision.value, "risk": case.expected_risk.value,
            "requires_human": case.requires_human,
            "policy_evidence": case.expected_policy_evidence, "hard_block": case.hard_block,
        },
        "label_reason": case.label_reason, "policy_basis": case.policy_basis,
        "applicable_policy": case.applicable_policy.model_dump(),
        "llm_fault": case.llm_fault, "retrieval_fault": case.retrieval_fault,
        "actual": actual, "execution_error": error, "decision_schema_valid": valid,
        "llm_attempts": provider.attempts, "latency_ms": elapsed, "probe_result": probe,
    }
    dependency_errors = actual.get("errors", [])
    reasons = [error] if error else [f'{e["node"]}:{e["message"]}' for e in dependency_errors]
    analysis = actual.get("llm_analysis") or {}
    return ShadowResult(
        shadow_run_id=run_id, case_id=case.case_id,
        dataset_version=manifest["dataset_version"],
        policy_version=actual.get("authoritative_version_id") or None,
        policy_fixture_sha256=manifest["policy_fixture_sha256"],
        model_provider=analysis.get("provider", "deepseek" if adapters else "mock"),
        model_name=analysis.get("model", "NO_MODEL_RESPONSE" if adapters else "m1-structured-mock"),
        prompt_version=manifest["prompt_version"],
        reference_source=ReferenceSource.SYNTHETIC_RULE_LABEL,
        reference_decision=case.expected_decision,
        agent_decision=actual.get("decision"), agent_risk=actual.get("risk"),
        requires_human=actual.get("requires_human"),
        decision_match=case.expected_decision.value == actual.get("decision"),
        policy_match=not error and set(case.expected_policy_evidence)
        == set(actual.get("policy_evidence", [])),
        failure_reason=";".join(reasons) or None,
        execution_status="FAILED" if error else "DEPENDENCY_FAILED" if reasons else "COMPLETED",
        latency_ms=elapsed,
        token_usage={"billable_tokens": 0, "paid_api_calls": 0, "source": "mock-or-injected"},
        timestamp=timestamp(), evaluation_row=row, side_effects=effects,
    )


async def run_shadow(dataset_path=DEFAULT_DATASET, *, store_path=DEFAULT_STORE, run_id=None,
                     case_ids=None, resume=False):
    dataset = load_dataset(dataset_path)
    if resume and run_id is None:
        raise ValueError("Resume requires the original Run ID")
    run_id = str(UUID(str(run_id))) if run_id is not None else str(uuid4())
    requested = set(case_ids) if case_ids is not None else {c.case_id for c in dataset.cases}
    if not requested or requested - {c.case_id for c in dataset.cases}:
        raise ValueError("Empty or unknown case selection")
    cases = [c for c in dataset.cases if c.case_id in requested]
    manifest = run_manifest(dataset_path, dataset, cases)
    results, failure = [], None
    store = None
    started = timestamp().isoformat()
    try:
        store = ShadowStore(store_path)
        with store.exclusive():
            started = store.begin(run_id, manifest, resume=resume)
            for case in cases:
                result = store.get(run_id, case.case_id)
                if result is None:
                    result = await execute_case(dataset, case, run_id, manifest)
                    store.save(result)
                results.append(result)
            report = shadow_summary(run_id, manifest, results, len(cases), started)
            store.finish(run_id, {k: v for k, v in report.items() if k != "cases"})
            return report
    except (sqlite3.Error, OSError) as exc:
        failure = type(exc).__name__
    finally:
        if store is not None:
            store.close()
    return shadow_summary(run_id, manifest, results, len(cases), started, failure)


def shadow_summary(run_id, manifest, results, selected, started, store_failure=None):
    rows = [result.metric_row() for result in results]
    metrics, gates = summarize(rows), governance(rows)
    effects = {key: sum(r.side_effects[key] for r in results) for key in (
        "erp_write_count", "claim_mutation_count", "human_review_action_count",
    )}
    passed = (not store_failure and len(rows) == selected and not any(effects.values())
              and not metrics["error_cases"] and not metrics["hard_block_violations"]
              and all(value is not False for value in gates.values()))
    return {
        "shadow_status": "PASS" if passed else "FAIL", "shadow_run_id": run_id,
        "mode": "shadow-synthetic-mock", "data_source": "Synthetic",
        "reference_source": "SYNTHETIC_RULE_LABEL", "production_accuracy": "NOT_CERTIFIED",
        "started_at": started, "completed_at": timestamp().isoformat(), "manifest": manifest,
        "selected_case_count": selected, "persisted_case_count": len(rows),
        "evaluation_store_failure": store_failure, "side_effects": effects,
        "metrics": metrics, "governance": gates,
        "governance_certification": "PASS" if all(gates.values()) else "NOT_CERTIFIED",
        "cases": [r.model_dump(mode="json") for r in results],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--run-id")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.output and args.output.exists():
        parser.error("Output exists; use a fresh report path")
    try:
        report = asyncio.run(run_shadow(
            args.dataset, store_path=args.store, run_id=args.run_id,
            case_ids=args.case_ids, resume=args.resume,
        ))
    except (ValueError, TypeError):
        parser.error("Invalid/conflicting shadow input; business system untouched")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
    print(json.dumps({k: report[k] for k in (
        "shadow_status", "shadow_run_id", "persisted_case_count", "side_effects",
        "evaluation_store_failure",
    )}))
    return 0 if report["shadow_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
