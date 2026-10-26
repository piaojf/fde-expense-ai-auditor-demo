"""Read existing shadow artifacts and export analysis; no replay or paid provider path."""

import argparse
import csv
import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from uuid import UUID

from evals.comparison.comparator import (
    EFFECT_KEYS,
    SEVERITIES,
    Disagreement,
    compare_case,
    metric_row,
)
from evals.dataset import DEFAULT_DATASET, ROOT, load_dataset
from evals.metrics import ratio, summarize
from evals.runner import governance, sha256


def read_shadow_store(path, run_id):
    """Do not instantiate ShadowStore: its constructor writes schema and PRAGMAs."""
    run_id = str(UUID(run_id))
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != {"shadow_runs", "shadow_results"}:
            raise ValueError("Expected an existing Shadow Store")
        run = db.execute("SELECT manifest,summary FROM shadow_runs WHERE run_id=?", (run_id,))
        saved = run.fetchone()
        if saved is None:
            raise ValueError("Unknown shadow run")
        manifest, summary = json.loads(saved[0]), json.loads(saved[1])
        if summary.get("manifest", manifest) != manifest:
            raise ValueError("Shadow summary/manifest conflict")
        records = []
        for case_id, body in db.execute(
            "SELECT case_id,body FROM shadow_results WHERE run_id=? ORDER BY case_id", (run_id,)
        ):
            record = json.loads(body)
            if record.get("case_id") != case_id:
                raise ValueError("Stored case identity conflicts with body")
            records.append(record)
        return {
            "mode": manifest.get("mode"),
            "data_source": "Synthetic",
            "reference_source": "SYNTHETIC_RULE_LABEL",
            **summary,
            "shadow_run_id": run_id,
            "manifest": manifest,
            "cases": records,
        }


def compare_run(shadow, dataset_path=DEFAULT_DATASET):
    dataset = load_dataset(dataset_path)
    version = f"{dataset.dataset_id}:{sha256(dataset_path)}"
    policy_hash = sha256(ROOT / dataset.policy_fixture)
    run_id = str(UUID(shadow["shadow_run_id"]))
    manifest = shadow["manifest"]
    mode = manifest.get("mode")
    real_mode = mode in ("shadow-synthetic-real-deepseek", "shadow-synthetic-simulated-real")
    simulated = mode == "shadow-synthetic-simulated-real"
    prompt_file = "integrations/llm/deepseek.py" if real_mode else "integrations/llm/mock.py"
    if real_mode:
        from evals.shadow_mode.pilot_manifest import manifest as fixed_manifest

        fixed = fixed_manifest()
        if (manifest.get("pilot_selection_sha256") != fixed["manifest_sha256"]
                or manifest.get("case_ids") != fixed["execution_order"]
                or manifest.get("http_budget") != {"deepseek": 30, "bge-m3": 30}
                or (not simulated and manifest.get("paid_approval") is not True)):
            raise ValueError("Invalid real pilot provenance/approval")
    ids = manifest["case_ids"]
    by_id = {c.case_id: c for c in dataset.cases}
    if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)) or set(ids) - by_id.keys():
        raise ValueError("Invalid shadow case selection")
    if (
        manifest["dataset_version"] != version
        or manifest["policy_fixture_sha256"] != policy_hash
        or shadow.get("reference_source") != "SYNTHETIC_RULE_LABEL"
        or shadow.get("data_source") != "Synthetic"
        or shadow.get("mode") != mode
        or (not real_mode and mode != "shadow-synthetic-mock")
        or manifest.get("prompt_version") != sha256(ROOT / prompt_file)
        or shadow.get("selected_case_count", len(ids)) != len(ids)
    ):
        raise ValueError("Run reference provenance or dataset/policy version conflict")
    results = {}
    for result in shadow["cases"]:
        case_id = result["case_id"]
        if case_id not in ids or case_id in results:
            raise ValueError("Duplicate or unselected shadow record")
        results[case_id] = result
    comparisons = [
        compare_case(
            by_id[c],
            results.get(c),
            dataset_version=version,
            policy_sha256=policy_hash,
            run_id=run_id,
            prompt_version=manifest["prompt_version"],
            real_mode=real_mode, simulated=simulated,
        )
        for c in ids
    ]
    rows = [
        metric_row(by_id[c], results.get(c), comparison, run_id)
        for c, comparison in zip(ids, comparisons, strict=True)
    ]
    # Includes every selected label, even absent/invalid results. M5A formulas are untouched.
    metrics = summarize(rows)
    gates = governance(rows)
    n = len(comparisons)
    counts = Counter(d["category"] for c in comparisons for d in c["disagreements"])
    severity = Counter(c["severity"] for c in comparisons if c["disagreements"])
    disagreements = sum(bool(c["disagreements"]) for c in comparisons)
    unavailable_reference = sum(not c["reference_available"] for c in comparisons)
    unavailable_agent = sum(not c["agent_result_valid"] for c in comparisons)
    dependency_failures = sum(c["dependency_failure"] for c in comparisons)
    effects_known = all(c["side_effects"] is not None for c in comparisons)
    effects = {
        k: sum(c["side_effects"][k] for c in comparisons) if effects_known else None
        for k in EFFECT_KEYS
    }
    isolation_pass = effects_known and not any(effects.values())
    agreement = {
        name: ratio(sum(c[field] for c in comparisons), n)
        for name, field in (
            ("decision", "decision_match"),
            ("risk", "risk_match"),
            ("policy", "policy_match"),
            ("human_review", "human_review_match"),
        )
    }
    valid = [c for c in comparisons if c["agent_result_valid"]]
    business = sum(
        any(
            d["category"] not in ("EVALUATION_ERROR", "REFERENCE_UNAVAILABLE")
            for d in c["disagreements"]
        )
        for c in comparisons
    )
    passed = (
        not unavailable_reference
        and not unavailable_agent
        and not business
        and isolation_pass
        and not metrics["hard_block_violations"]
        and (
            all(gates.values())
            if set(ids) == by_id.keys()
            else all(v is not False for v in gates.values())
        )
        and all(
            not c["dependency_failure"] or c["expected_dependency_failure"] for c in comparisons
        )
        and shadow.get("shadow_status") == "PASS"
        and not shadow.get("evaluation_store_failure")
    )
    comparisons.sort(
        key=lambda c: (
            SEVERITIES.index(c["severity"]) if c["severity"] else len(SEVERITIES),
            c["case_id"],
        )
    )
    return {
        "comparison_status": "PASS" if passed else "FAIL",
        "shadow_run_id": run_id,
        "dataset_id": dataset.dataset_id,
        "dataset_version": version,
        "reference_source": "SYNTHETIC_RULE_LABEL",
        "agent_provider": "Simulated DeepSeek HTTP" if simulated else "DeepSeek"
        if real_mode else "Mock",
        "environment": "Synthetic + Simulated HTTP + Real PostgreSQL" if simulated else
        "Synthetic Cases + Real DeepSeek; Not Expert-Validated; Not Production Accuracy"
        if real_mode else "Synthetic + Mock; lexical RAG",
        "production_accuracy": "NOT_CERTIFIED",
        "source_shadow_status": shadow.get("shadow_status", "INCOMPLETE"),
        "source_store_failure": shadow.get("evaluation_store_failure"),
        "selected_case_count": n,
        "dataset_case_count": len(dataset.cases),
        "persisted_case_count": len(results),
        "agreement": agreement,
        "total_disagreements": disagreements,
        "business_disagreements": business,
        "disagreement_categories": {d.value: counts[d.value] for d in Disagreement},
        "disagreement_severities": {s: severity[s] for s in SEVERITIES},
        "coverage": {
            "valid_agent_output": ratio(len(valid), n),
            "successful_dependency_execution": ratio(len(valid) - dependency_failures, n),
            "unavailable_agent_results": unavailable_agent,
            "unavailable_reference_labels": unavailable_reference,
            "evaluation_failure_count": unavailable_agent + dependency_failures,
            "dependency_failures": dependency_failures,
            "expected_injected_dependency_failures": sum(
                c["expected_dependency_failure"] for c in comparisons
            ),
        },
        "metrics": metrics,
        "governance": gates,
        "governance_certification": "PASS" if all(gates.values()) else "NOT_CERTIFIED",
        "side_effect_isolation": "PASS" if isolation_pass else "NOT_CERTIFIED",
        "side_effects": effects,
        "comparison_source_sha256": {
            str(p.relative_to(ROOT)): sha256(p)
            for p in sorted((ROOT / "evals/comparison").glob("*.py"))
        },
        "source_manifest": manifest,
        "cases": comparisons,
    }


CSV_FIELDS = (
    "case_id",
    "reference_source",
    "reference_decision",
    "agent_decision",
    "reference_risk",
    "agent_risk",
    "reference_requires_human",
    "agent_requires_human",
    "reference_policy",
    "agent_policy",
    "reference_authoritative_policy",
    "agent_authoritative_policy",
    "decision_match",
    "risk_match",
    "policy_match",
    "human_review_match",
    "match_status",
    "execution_status",
    "dependency_diagnostics",
    "severity",
    "disagreements",
    "reason",
)


def write_outputs(result, output, report=None, csv_path=None):
    from evals.comparison.report import render_report

    paths = [Path(p) for p in (output, report, csv_path) if p is not None]
    if len({p.resolve() for p in paths}) != len(paths) or any(p.exists() for p in paths):
        raise ValueError("Outputs must be distinct new files")
    for p in paths:
        p.parent.mkdir(parents=True, exist_ok=True)
    with Path(output).open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    if report:
        with Path(report).open("x", encoding="utf-8") as handle:
            handle.write(render_report(result, output, csv_path))
    if csv_path:
        with Path(csv_path).open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
            writer.writeheader()
            for c in result["cases"]:
                writer.writerow(
                    {
                        k: json.dumps(c[k], ensure_ascii=False)
                        if isinstance(c[k], (list, dict))
                        else c[k]
                        for k in CSV_FIELDS
                    }
                )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path)
    source.add_argument("--store", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--csv", type=Path)
    args = parser.parse_args(argv)
    if bool(args.store) != bool(args.run_id):
        parser.error("--run-id is required only with --store")
    try:
        shadow = (
            read_shadow_store(args.store, args.run_id)
            if args.store
            else json.loads(args.input.read_text(encoding="utf-8"))
        )
        result = compare_run(shadow, args.dataset)
        result["input_artifact"] = str(args.store if args.store else args.input)
        # Store file hash can change as other runs append; keep the selected snapshot hash too.
        result["input_snapshot_sha256"] = hashlib.sha256(
            json.dumps(shadow, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        write_outputs(result, args.output, args.report, args.csv)
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
        parser.error("Invalid comparison input/output; no business operation performed")
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "comparison_status",
                    "selected_case_count",
                    "total_disagreements",
                    "side_effects",
                )
            }
        )
    )
    return 0 if result["comparison_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
