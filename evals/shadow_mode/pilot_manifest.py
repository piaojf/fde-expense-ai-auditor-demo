"""Frozen M5B-3 sample/budget preflight. Pure offline; no provider or Settings import."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from evals.dataset import DEFAULT_DATASET, load_dataset

SELECTION = {
    "compliant": ("compliant-02", "compliant-03"),
    "over_limit": ("over_limit-01", "over_limit-02"),
    "policy_version_conflict": ("policy_version_conflict-01", "policy_version_conflict-04"),
    "region_mismatch": ("region_mismatch-01", "region_mismatch-05"),
    "expense_type_mismatch": ("expense_type_mismatch-01", "expense_type_mismatch-02"),
    "special_stay": ("special_stay-01", "special_stay-02"),
    "invoice_anomaly": ("invoice_anomaly-01", "invoice_anomaly-02"),
    "missing_proof": ("missing_proof-01", "missing_proof-02"),
    "hard_block": ("hard_block-01", "hard_block-03"),
    "prompt_injection": ("prompt_injection-01", "prompt_injection-02"),
    "no_valid_policy": ("no_valid_policy-01", "no_valid_policy-02"),
    "dependency_failure": ("dependency_failure-01", "dependency_failure-03"),
}
SMOKE = ("compliant-03", "hard_block-01", "special_stay-01", "prompt_injection-02")
INJECTIONS = {
    "dependency_failure-01": "RETRIEVER_TIMEOUT_BEFORE_HTTP",
    "dependency_failure-03": "INVALID_LLM_OUTPUT_BEFORE_HTTP",
}


def manifest(dataset_path=DEFAULT_DATASET):
    path = Path(dataset_path)
    dataset = load_dataset(path)
    version = f"{dataset.dataset_id}:{hashlib.sha256(path.read_bytes()).hexdigest()}"
    by_id = {c.case_id: c for c in dataset.cases}
    rows = []
    for category, ids in SELECTION.items():
        for case_id in ids:
            case = by_id[case_id]
            if case.category != category or case.catalog_mode != "DEFAULT":
                raise ValueError("Frozen sample category/catalog conflict")
            injection = INJECTIONS.get(case_id)
            if injection is None and (case.llm_fault != "NONE" or case.retrieval_fault != "NONE"):
                raise ValueError("Unmarked artificial dependency fault")
            rows.append({
                "case_id": case_id, "category": category,
                "reference_source": "SYNTHETIC_RULE_LABEL", "dataset_version": version,
                "label_source": case.label_source,
                "expected_decision": case.expected_decision.value,
                "expected_risk": case.expected_risk.value,
                "expected_policy": case.applicable_policy.model_dump(),
                "llm_fault": case.llm_fault, "retrieval_fault": case.retrieval_fault,
                "probe": case.probe, "artificial_fault_injection": injection,
                "real_inference_planned": injection is None,
                "smoke": case_id in SMOKE,
            })
    ids = [r["case_id"] for r in rows]
    if len(ids) != 24 or len(set(ids)) != 24 or not set(SMOKE) <= set(ids):
        raise ValueError("Frozen sample identity conflict")
    body = {
        "pilot_id": "m5b-3-fixed-v1", "dataset_version": version,
        "label_method": dataset.label_method, "selection_method":
        "Two fixed cases per category; boundary/historical/date/region coverage; "
        "selected before any live predictions. No outcome-based resampling.",
        "reference_source": "SYNTHETIC_RULE_LABEL",
        "accuracy_notice": "Synthetic Cases + Real DeepSeek; Not Expert-Validated; "
        "Not Production Accuracy",
        "selected_case_count": len(rows),
        "category_counts": dict(Counter(r["category"] for r in rows)),
        "expected_decision_counts": dict(Counter(r["expected_decision"] for r in rows)),
        "real_inference_planned_count": 22, "injected_case_count": 2,
        "smoke_case_ids": list(SMOKE),
        "execution_order": [*SMOKE, *[i for i in ids if i not in SMOKE]],
        "budget": {
            "nominal_deepseek_http_attempts": 22,
            "maximum_deepseek_http_attempts_including_retries": 30,
            "retry_headroom": 8, "maximum_selected_cases": 24,
            "smoke_reused_in_batch": True,
            "injected_cases_deepseek_http_attempts": 0,
            "approval": "REQUIRED_IN_SUBSEQUENT_USER_MESSAGE",
        },
        "cases": rows,
    }
    body["manifest_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return body


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    body = manifest()
    # Exclusive creation preserves the original selection artifact.
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(body, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print("PAID_API_APPROVAL_REQUIRED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
