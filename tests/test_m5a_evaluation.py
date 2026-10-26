"""Golden labels, real graph safety, explicit denominators and paid-call isolation."""

import asyncio
import copy
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from evals.dataset import DEFAULT_DATASET, GoldenDataset, load_dataset
from evals.metrics import ratio, summarize
from evals.report import render_report
from evals.runner import CallBudget, main, run_evaluation
from integrations.llm.deepseek import DeepSeekLLMProvider

DATASET = load_dataset()


def independently_labeled(case):
    """No imports from rule, risk, routing or retriever implementations under evaluation."""
    expense = case.claim.expense
    year = expense.incurred_on.year
    policy, cap = None, None
    if case.claim.employee.legal_entity == "DEMO-CN" and case.catalog_mode == "DEFAULT":
        if expense.city == "深圳" and year in (2024, 2025, 2026):
            if expense.category == "HOTEL":
                policy = f"POL-SZ-{year}"
                cap = {2024: Decimal("600"), 2025: Decimal("900"), 2026: Decimal("700")}[year]
            elif year == 2026 and expense.category in ("MEAL", "TRANSPORT"):
                policy = f"POL-{expense.category}-2026"
        elif expense.city == "北京" and year == 2026 and expense.category == "HOTEL":
            policy, cap = "POL-BEIJING-2026", Decimal("800")
    docs = {d["document_id"]: d for d in case.invoices}
    matches = bool(case.claim.documents)
    invoice_seen, outside = False, False
    for ref in case.claim.documents:
        doc = docs.get(ref.document_id)
        if not doc or doc["claim_id"] != case.case_id:
            matches = False
        elif doc.get("kind", "INVOICE") == "INVOICE":
            invoice_seen = True
            # Compare normalized money, because fixture input may be integer decimal text.
            inv = dict(doc["expense"])
            inp = expense.model_dump(mode="json")
            inv["amount"] = f"{Decimal(inv['amount']):.2f}"
            matches &= inv == inp
            trip = doc.get("trip_context")
            outside |= bool(trip and trip["stay_to"] > trip["approved_to"])
        else:
            matches &= bool(doc.get("cancelled_on") and doc.get("flight_reference"))
    blocked = (
        not matches
        or not invoice_seen
        or expense.incurred_on > date(2026, 10, 8)
        or (cap is not None and expense.amount > cap * expense.nights)
    )
    evidence = [policy] if policy and case.retrieval_fault == "NONE" else []
    uncertain = (
        cap is None
        or outside
        or case.retrieval_fault != "NONE"
        or case.llm_fault in ("INVALID_OUTPUT", "INVALID_RESPONSE", "TIMEOUT_ALWAYS")
    )
    decision, risk = (
        ("REJECT", "HIGH")
        if blocked
        else (("HUMAN_REVIEW", "MEDIUM") if uncertain else ("APPROVE", "LOW"))
    )
    return decision, risk, decision == "HUMAN_REVIEW", evidence, policy, blocked


@pytest.mark.parametrize("case", DATASET.cases, ids=lambda c: c.case_id)
def test_golden_labels_follow_independent_business_facts(case):
    decision, risk, human, evidence, policy, blocked = independently_labeled(case)
    assert (case.expected_decision, case.expected_risk, case.requires_human) == (
        decision,
        risk,
        human,
    )
    assert case.expected_policy_evidence == evidence
    assert case.applicable_policy.policy_version_id == policy
    assert case.hard_block == blocked
    assert case.policy_basis and case.label_reason
    if case.category in ("special_stay", "missing_proof"):
        assert case.label_source == "HUMAN_REVIEW_REQUIRED"


@pytest.mark.parametrize("mutation", ["duplicate", "risk", "human", "policy", "source", "block"])
def test_dataset_rejects_ambiguous_or_inconsistent_labels(mutation):
    raw = json.loads(DEFAULT_DATASET.read_text(encoding="utf-8"))
    row = raw["cases"][0]
    if mutation == "duplicate":
        raw["cases"].append(copy.deepcopy(row))
    elif mutation == "risk":
        row["expected_risk"] = "HIGH"
    elif mutation == "human":
        row["requires_human"] = True
    elif mutation == "policy":
        row["expected_policy_evidence"] = ["POL-SZ-2025"]
    elif mutation == "source":
        raw["data_source"] = "Real Enterprise Data"
    elif mutation == "block":
        row["hard_block"] = True
    with pytest.raises(ValidationError):
        GoldenDataset.model_validate(raw)


@pytest.fixture(scope="module")
def actual_run():
    return asyncio.run(run_evaluation())


def test_full_graph_golden_gates_and_traceability(actual_run):
    assert actual_run["case_count"] >= 60
    assert actual_run["evaluation_status"] == "PASS"
    assert all(actual_run["governance"].values())
    assert actual_run["metrics"]["hard_block_violations"] == 0
    assert actual_run["metrics"]["paid_api_calls"] == 0
    assert actual_run["metrics"]["error_cases"] == []
    for row in actual_run["cases"]:
        kinds = {e["event_type"] for e in row["actual"]["audit_events"]}
        assert {"HARD_GATE_CHECKED", "RULES_CHECKED", "DECISION_MADE"} <= kinds
        assert row["expected"]["decision"] == row["actual"]["decision"]
        if row["actual"]["decision"] != "APPROVE":
            assert row["actual"]["mock_erp_writes"] == 0
    by_id = {r["case_id"]: r for r in actual_run["cases"]}
    assert by_id["hard_block-02"]["actual"]["llm_analysis"]["suggested_risk"] == "LOW"
    assert by_id["hard_block-02"]["actual"]["decision"] == "REJECT"
    assert by_id["prompt_injection-06"]["actual"]["decision"] == "REJECT"
    assert (
        "SYSTEM:" in by_id["prompt_injection-06"]["actual"]["policy_evidence_details"][0]["quote"]
    )


def test_repeat_semantic_results_without_ambient_network_or_demo_state(actual_run, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline runner must never construct a network client/settings")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    monkeypatch.setattr("apps.api.config.Settings", forbidden)
    demo = Path(".m1-runtime/mock.sqlite3")
    before = demo.read_bytes() if demo.exists() else None
    second = asyncio.run(run_evaluation())
    assert second["run_id"] != actual_run["run_id"]
    assert second["evaluation_status"] == "PASS"
    assert [
        (
            r["expected"],
            r["actual"]["decision"],
            r["actual"]["risk"],
            r["actual"]["policy_evidence"],
        )
        for r in second["cases"]
    ] == [
        (
            r["expected"],
            r["actual"]["decision"],
            r["actual"]["risk"],
            r["actual"]["policy_evidence"],
        )
        for r in actual_run["cases"]
    ]
    assert (demo.read_bytes() if demo.exists() else None) == before


def metric_row(expected, actual, *, block=False, evidence=None):
    return {
        "case_id": f"{expected}-{actual}",
        "category": "test",
        "risk_category": "test",
        "expected": {
            "decision": expected,
            "risk": "LOW",
            "requires_human": False,
            "policy_evidence": ["p"],
            "hard_block": block,
        },
        "actual": {
            "decision": actual,
            "risk": "LOW",
            "requires_human": False,
            "policy_evidence": evidence if evidence is not None else ["p"],
        },
        "llm_attempts": [],
        "retrieval_fault": "NONE",
        "latency_ms": 10,
        "decision_schema_valid": True,
    }


def test_metric_denominators_with_actual_errors_not_all_pass_fixture():
    rows = [
        metric_row("REJECT", "APPROVE", block=True),
        metric_row("HUMAN_REVIEW", "HUMAN_REVIEW"),
        metric_row("REJECT", "REJECT"),
        metric_row("APPROVE", "APPROVE"),
        metric_row("APPROVE", "REJECT"),
        metric_row("APPROVE", None, evidence=[]),
    ]
    m = summarize(rows)
    assert m["decision_accuracy"] == ratio(3, 6)
    assert m["false_approval_unsafe_opportunity"] == ratio(1, 3)
    assert m["false_approval_contamination"] == ratio(1, 2)
    assert m["false_rejection_rate"] == ratio(1, 3)
    assert m["human_review_rate"] == ratio(1, 6)
    assert m["automation_rate"] == ratio(4, 6)
    assert m["hard_block_violations"] == 1
    assert m["policy_retrieval_accuracy"] == ratio(5, 6)
    assert m["confusion_matrix"]["APPROVE"]["ERROR"] == 1
    assert len(m["error_cases"]) == 3
    assert summarize([])["decision_accuracy"]["value"] is None
    assert summarize([metric_row("REJECT", "REJECT")])["false_approval_contamination"] == ratio(
        0, 0
    )


def test_tokens_include_all_live_attempts_and_preserve_missing_coverage():
    row = metric_row("APPROVE", "APPROVE")
    row["llm_attempts"] = [
        {
            "source": "deepseek",
            "schema_valid": True,
            "token_usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        },
        {"source": "deepseek", "schema_valid": False},
        {"source": "injected", "schema_valid": False},
    ]
    m = summarize([row])
    assert m["schema_validation_rate"] == ratio(1, 3)
    assert m["paid_api_calls"] == 2
    assert m["token_usage"]["total_tokens"] == {
        "reported_sum": 150,
        "reported_calls": 1,
        "missing_calls": 1,
    }


@pytest.mark.parametrize("budget", [None, 0, -1, 101, True])
def test_invalid_live_budget_before_provider_or_network(budget, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid budget must fail before provider configuration")

    monkeypatch.setattr("apps.api.config.Settings", forbidden)
    with pytest.raises(ValueError):
        asyncio.run(run_evaluation(live_deepseek=True, max_calls=budget))
    with pytest.raises(ValueError):
        CallBudget(budget)


def test_real_adapter_with_simulated_http_enforces_global_budget_and_no_fallback():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "summary": "Synthetic transport validation only",
                                    "anomalies": [],
                                    "suggested_risk": "LOW",
                                    "confidence": 1.0,
                                    "provider": "deepseek",
                                    "model": "deepseek-flash",
                                }
                            ),
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            },
        )

    provider = DeepSeekLLMProvider(
        api_key="synthetic-test-key", transport=httpx.MockTransport(handler)
    )
    r = asyncio.run(
        run_evaluation(
            live_deepseek=True,
            live_provider=provider,
            max_calls=2,
            case_ids=["compliant-01", "compliant-02", "compliant-03"],
        )
    )
    assert len(calls) == r["live_calls_reserved"] == 2
    assert r["metrics"]["paid_api_calls"] == 0
    assert r["metrics"]["simulated_deepseek_http_attempts"] == 2
    assert r["live_budget_exhausted"] and r["evaluation_status"] == "FAIL"
    assert r["cases"][-1]["actual"]["decision"] == "HUMAN_REVIEW"
    assert r["cases"][-1]["actual"]["mock_erp_writes"] == 0
    assert r["metrics"]["token_usage"]["total_tokens"]["reported_sum"] == 0
    assert (
        sum(
            a["token_usage"]["total_tokens"]
            for row in r["cases"]
            for a in row["llm_attempts"]
            if a["source"] == "simulated_deepseek_http"
        )
        == 60
    )
    assert "synthetic-test-key" not in json.dumps(r)


def test_simulated_online_timeout_is_one_attempt_without_paid_retry():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic timeout")

    provider = DeepSeekLLMProvider(api_key="synthetic-only", transport=httpx.MockTransport(handler))
    result = asyncio.run(
        run_evaluation(
            live_deepseek=True,
            max_calls=3,
            live_provider=provider,
            case_ids=["compliant-01", "dependency_failure-06"],
        )
    )
    assert len(calls) == 1
    assert result["cases"][0]["actual"]["decision"] == "HUMAN_REVIEW"
    # Injected transient failure still follows existing M3 retry semantics in live mode.
    assert result["cases"][1]["actual"]["decision"] == "APPROVE"
    assert len(result["cases"][1]["llm_attempts"]) == 2


def test_unknown_case_and_implicit_live_provider_are_rejected():
    with pytest.raises(ValueError):
        asyncio.run(run_evaluation(case_ids=["does-not-exist"]))
    with pytest.raises(ValueError):
        asyncio.run(run_evaluation(live_provider=object()))
    with pytest.raises(ValueError):
        asyncio.run(run_evaluation(max_calls=1))


def test_report_uses_measured_values_and_cli_preserves_previous_results(actual_run, tmp_path):
    report = render_report(actual_run, Path("actual.json"))
    assert actual_run["run_id"] in report
    assert "72/72 (100.00%)" in report
    assert "NOT_CERTIFIED" in report and "LIVE_DEEPSEEK_EVALUATION: NOT_RUN" in report
    output = tmp_path / "existing.json"
    output.write_text("preserve me", encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["--output", str(output)])
    assert output.read_text(encoding="utf-8") == "preserve me"


def test_subset_reports_unexercised_governance_without_false_certification():
    result = asyncio.run(run_evaluation(case_ids=["compliant-01"]))
    assert result["evaluation_status"] == "PASS"
    assert result["governance_certification"] == "NOT_CERTIFIED"
    assert set(result["governance"].values()) == {None}


def test_budget_refusals_are_not_llm_calls_in_schema_denominator():
    row = metric_row("HUMAN_REVIEW", "HUMAN_REVIEW")
    row["llm_attempts"] = [{"source": "budget_exhausted", "schema_valid": False}]
    result = summarize([row])
    assert result["schema_validation_rate"] == ratio(0, 0)
    assert result["paid_api_calls"] == 0
