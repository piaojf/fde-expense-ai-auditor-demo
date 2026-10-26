"""Evaluation denominators are explicit; no denominator means undefined, not success."""

import math
from collections import Counter
from statistics import mean, median


def ratio(numerator, denominator):
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def mismatches(row):
    expected, actual = row["expected"], row["actual"]
    fields = ["decision", "risk", "requires_human"]
    errors = [key for key in fields if expected[key] != actual.get(key)]
    if set(expected["policy_evidence"]) != set(actual.get("policy_evidence", [])):
        errors.append("policy_evidence")
    if row.get("execution_error"):
        errors.append("execution_error")
    if not row.get("decision_schema_valid", False):
        errors.append("decision_schema")
    return errors


def summarize(rows):
    n = len(rows)
    approved = [r for r in rows if r["actual"].get("decision") == "APPROVE"]
    should_approve = [r for r in rows if r["expected"]["decision"] == "APPROVE"]
    unsafe = [r for r in rows if r["expected"]["decision"] != "APPROVE"]
    wrong_approvals = sum(r["expected"]["decision"] != "APPROVE" for r in approved)

    def retrieval_ok(row):
        return set(row["expected"]["policy_evidence"]) == set(
            row["actual"].get("policy_evidence", [])
        ) and not row.get("execution_error")

    available = [
        r for r in rows if r["expected"]["policy_evidence"] and r["retrieval_fault"] == "NONE"
    ]
    fault_rows = [r for r in rows if r["retrieval_fault"] != "NONE"]
    attempts = [a for r in rows for a in r["llm_attempts"] if a["source"] != "budget_exhausted"]
    live = [a for a in attempts if a["source"] == "deepseek"]
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    tokens = {
        key: {
            "reported_sum": sum(a.get("token_usage", {}).get(key, 0) for a in live),
            "reported_calls": sum(key in a.get("token_usage", {}) for a in live),
            "missing_calls": sum(key not in a.get("token_usage", {}) for a in live),
        }
        for key in keys
    }
    latencies = sorted(r["latency_ms"] for r in rows)
    matrix = {
        decision: dict.fromkeys(["APPROVE", "REJECT", "HUMAN_REVIEW", "ERROR"], 0)
        for decision in ["APPROVE", "REJECT", "HUMAN_REVIEW"]
    }
    for row in rows:
        matrix[row["expected"]["decision"]][row["actual"].get("decision") or "ERROR"] += 1
    metrics = {
        "decision_accuracy": ratio(
            sum(r["expected"]["decision"] == r["actual"].get("decision") for r in rows), n
        ),
        "false_approval_unsafe_opportunity": ratio(wrong_approvals, len(unsafe)),
        "false_approval_contamination": ratio(wrong_approvals, len(approved)),
        "false_rejection_rate": ratio(
            sum(r["actual"].get("decision") == "REJECT" for r in should_approve),
            len(should_approve),
        ),
        "human_review_rate": ratio(
            sum(r["actual"].get("decision") == "HUMAN_REVIEW" for r in rows), n
        ),
        "automation_rate": ratio(
            sum(r["actual"].get("decision") in ("APPROVE", "REJECT") for r in rows), n
        ),
        "policy_retrieval_accuracy": ratio(sum(map(retrieval_ok, rows)), n),
        "policy_retrieval_available": ratio(sum(map(retrieval_ok, available)), len(available)),
        "policy_retrieval_fault_cases": ratio(sum(map(retrieval_ok, fault_rows)), len(fault_rows)),
        "hard_block_violations": sum(
            (r["expected"]["hard_block"] or r["actual"].get("hard_block", False))
            and r["actual"].get("decision") != "REJECT"
            for r in rows
        ),
        "schema_validation_rate": ratio(sum(a["schema_valid"] for a in attempts), len(attempts)),
        "decision_schema_rate": ratio(sum(r["decision_schema_valid"] for r in rows), n),
        "risk_accuracy": ratio(
            sum(r["expected"]["risk"] == r["actual"].get("risk") for r in rows), n
        ),
        "human_flag_accuracy": ratio(
            sum(r["expected"]["requires_human"] == r["actual"].get("requires_human") for r in rows),
            n,
        ),
        "latency_ms": {
            "mean": mean(latencies) if n else None,
            "p50": median(latencies) if n else None,
            "p95": latencies[math.ceil(0.95 * n) - 1] if n else None,
            "max": max(latencies) if n else None,
        },
        "token_usage": tokens,
        "paid_api_calls": len(live),
        "injected_or_mock_attempts": sum(a["source"] in ("mock", "injected") for a in attempts),
        "simulated_deepseek_http_attempts": sum(
            a["source"] == "simulated_deepseek_http" for a in attempts
        ),
        "confusion_matrix": matrix,
        "categories": dict(Counter(r["category"] for r in rows)),
        "error_cases": [
            {"case_id": r["case_id"], "mismatches": mismatches(r)} for r in rows if mismatches(r)
        ],
        "expected_dependency_failures": [r["case_id"] for r in rows if r["actual"].get("errors")],
    }
    metrics["risk_groups"] = {
        category: {
            "cases": len(group := [r for r in rows if r["risk_category"] == category]),
            "decision_correct": sum(
                r["expected"]["decision"] == r["actual"].get("decision") for r in group
            ),
            "false_approvals": sum(
                r["expected"]["decision"] != "APPROVE" and r["actual"].get("decision") == "APPROVE"
                for r in group
            ),
            "errors": sum(bool(mismatches(r)) for r in group),
        }
        for category in sorted({r["risk_category"] for r in rows})
    }
    return metrics
