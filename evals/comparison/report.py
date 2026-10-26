"""Markdown presentation of actual comparison artifacts, with explicit limitations."""

from pathlib import Path

from evals.comparison.comparator import SEVERITIES


def rate(value):
    v = value["value"]
    return (
        f"{value['numerator']}/{value['denominator']} = {v:.2%}"
        if v is not None
        else f"undefined (denominator={value['denominator']})"
    )


def render_report(result, json_path, csv_path=None):
    m, coverage = result["metrics"], result["coverage"]
    lines = [
        "# M5B-2 — Reference vs Agent Decision Comparison",
        "",
        f"Comparison acceptance: **{result['comparison_status']}**. "
        "This is **Synthetic + Mock**, not actual finance staff decisions or "
        "real DeepSeek accuracy.",
        "",
        "## Source and scope",
        "",
        f"- Reference Source: `{result['reference_source']}`; "
        "M5A RULE_DERIVED and HUMAN_REVIEW_REQUIRED labels are synthetic rule labels.",
        "- EXPERT_REVIEWED / HISTORICAL_HUMAN_DECISION: unused; no real human decision imported.",
        f"- Dataset: `{result['dataset_id']}`; {result['selected_case_count']} selected / "
        f"{result['dataset_case_count']} total cases; 12 synthetic categories in the full run.",
        f"- Dataset version: `{result['dataset_version']}`.",
        f"- Policy SHA256: `{result['source_manifest']['policy_fixture_sha256']}`.",
        f"- Provider: {result['agent_provider']}; lexical RAG, mock invoice parser.",
        f"- Shadow run: `{result['shadow_run_id']}`; original results read without replay.",
        f"- Input artifact: `{result.get('input_artifact', 'test fixture')}`.",
        "- Original Golden Labels, M5A formulas, public API and frontend remain unchanged.",
        "",
        "## Agreement and M5A metrics",
        "",
        "| Metric | Result |",
        "| --- | --- |",
    ]
    for name, key in (
        ("Decision Agreement", "decision"),
        ("Risk Agreement", "risk"),
        ("Policy Agreement (evidence + authoritative version)", "policy"),
        ("Human flag Agreement", "human_review"),
    ):
        lines.append(f"| {name} | {rate(result['agreement'][key])} |")
    for name, key in (
        ("M5A decision accuracy", "decision_accuracy"),
        ("False Approval Rate (unsafe opportunity)", "false_approval_unsafe_opportunity"),
        ("False Approval Rate (approval contamination)", "false_approval_contamination"),
        ("False Rejection Rate", "false_rejection_rate"),
        ("Human Review Rate", "human_review_rate"),
        ("Automation Rate", "automation_rate"),
        ("M5A policy retrieval agreement (evidence only)", "policy_retrieval_accuracy"),
    ):
        lines.append(f"| {name} | {rate(m[key])} |")
    lines.extend(
        [
            f"| Hard Block Violations | {m['hard_block_violations']} |",
            f"| Valid Agent Output Coverage | {rate(coverage['valid_agent_output'])} |",
            "| Successful Dependency Execution Coverage | "
            f"{rate(coverage['successful_dependency_execution'])} |",
            f"| Evaluation Failure Count | {coverage['evaluation_failure_count']} |",
            f"| Expected Injected Dependency Failures | "
            f"{coverage['expected_injected_dependency_failures']} |",
            f"| Missing/Invalid Agent Results | {coverage['unavailable_agent_results']} |",
            "| Missing/Unavailable Reference Labels | "
            f"{coverage['unavailable_reference_labels']} |",
            "",
            "M5A denominators are unchanged: false approvals / all non-APPROVE references;",
            "approval contamination / all actual approvals; false rejections / APPROVE references.",
            "HUMAN_REVIEW is its own decision, never a rejection. All selected cases, including",
            "failed or missing results, remain in agreement denominators. Missing labels cannot",
            "certify a comparison. Correct explicit empty policy evidence differs "
            "from a missing field.",
            "",
            "## Decision confusion matrix",
            "",
            "Rows are reference labels, columns are valid Agent decisions; "
            "absent/invalid output is ERROR.",
            "",
            "| Reference / Agent | APPROVE | REJECT | HUMAN_REVIEW | ERROR |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for decision, row in m["confusion_matrix"].items():
        lines.append(
            f"| {decision} | "
            + " | ".join(str(row[k]) for k in ("APPROVE", "REJECT", "HUMAN_REVIEW", "ERROR"))
            + " |"
        )
    lines.extend(
        [
            "",
            "## Disagreements and severity",
            "",
            f"Total distinct cases with disagreement: **{result['total_disagreements']}**; "
            f"business field disagreements: **{result['business_disagreements']}**.",
            "Multiple categories can apply to one case. Severity counts partition cases by their",
            "maximum severity; category counts may exceed distinct cases. Dependency failure with",
            "a matching safe fallback is EVALUATION_ERROR, with field agreement retained.",
            "",
            "| Severity | Cases |",
            "| --- | --- |",
        ]
    )
    lines.extend(f"| {s} | {result['disagreement_severities'][s]} |" for s in SEVERITIES)
    lines.extend(["", "| Category | Cases |", "| --- | --- |"])
    lines.extend(f"| {k} | {v} |" for k, v in result["disagreement_categories"].items())
    lines.extend(
        [
            "",
            "Severity rules are deterministic (no LLM scoring): Hard Block automatically approved",
            "is CRITICAL; reference rejection auto-approved or known applicable "
            "policy misuse is HIGH;",
            "missing Agent/reference is HIGH; required human judgment bypassed, "
            "false rejection, or",
            "dependency failure with valid fallback is MEDIUM; risk-only mismatch "
            "or extra review of",
            "an approvable claim is LOW. A Hard Block sent to review rather than rejected is HIGH.",
            "",
            "## Observed examples",
            "",
            "| Case | Reference / Agent | Category | Severity | Reason |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    examples = [c for c in result["cases"] if c["disagreements"]]
    for c in examples[:10]:
        categories = ", ".join(d["category"] for d in c["disagreements"])
        reasons = c["reason"] + [
            f"{e['node']}:{e['error_type']}" for e in c["dependency_diagnostics"]
        ]
        lines.append(
            f"| {c['case_id']} | {c['reference_decision']} / {c['agent_decision']} | "
            f"{categories} | {c['severity']} | {', '.join(reasons)} |"
        )
    if not examples:
        lines.append("| None | All fields match | — | — | No observed disagreements |")
    lines.extend(
        [
            "",
            "The deliberate error cases are isolated in `tests/test_m5b_comparison.py`:",
            "wrong automatic approval/rejection, missed/extra human review, "
            "historical policy misuse,",
            "wrong risk and missing Agent result. They never enter the 72-case "
            "dataset or this report.",
            "",
            "## Per-case artifacts and isolation",
            "",
            f"- JSON (all cases, severity sorted): `{Path(json_path)}`.",
            f"- CSV: `{Path(csv_path)}`." if csv_path else "- CSV: not requested.",
            "- CLI: `.venv/bin/python -m evals.comparison.runner --input SHADOW.json "
            "--output NEW.json --report NEW.md --csv NEW.csv`.",
            "- Read-only Store: replace `--input` with `--store STORE.sqlite3 --run-id UUID`.",
            "- Existing outputs cannot be overwritten; duplicate/unselected cases "
            "and run provenance/",
            "  version conflicts are rejected. Missing selected results remain ERROR cases.",
            f"- Side-effect evidence: **{result['side_effect_isolation']}**; "
            + ", ".join(f"{k}={v}" for k, v in result["side_effects"].items())
            + ".",
            "- Comparator only reads persisted evidence and generates comparison "
            "files; it does not",
            "  execute ERP writes, alter claim status/revision, or submit human review actions.",
            "- Unit tests trap formal repository/workflow/ERP entry points and "
            "compare formal SQLite",
            "  dumps and source Shadow Store hashes before/after. Missing counters "
            "cannot certify zero.",
            f"- Governance: **{result['governance_certification']}**; `{result['governance']}`.",
            "",
            "## Known limitations and future reference imports",
            "",
            "- 72 correlated synthetic cases with MockLLM and lexical retrieval "
            "cannot establish real",
            "  DeepSeek quality, production performance, invoice authenticity, or "
            "finance acceptance.",
            "- The injected dependency failures deliberately model infrastructure "
            "trouble. Safe routing",
            "  can match a synthetic label while dependency execution coverage remains below 100%.",
            "- Comparison consumes trusted local evidence; observed historical counters are not an",
            "  independent production audit. No live provider, real ERP or "
            "production data is accessed.",
            "- Future EXPERT_REVIEWED imports require redacted frozen claim facts, "
            "independent expert",
            "  decision/rationale, reviewer and review event evidence, applicable "
            "policy/date/version,",
            "  dataset version and source approval. Historical imports "
            "additionally retain original",
            "  approval event identity/time and distinguish historical outcome "
            "from correct policy.",
            "  Validate those sources separately before comparison; setting an "
            "enum is insufficient.",
            "- Current CLI deliberately accepts only the existing synthetic "
            "dataset and source. There",
            "  is no expert/history importer, automatic decision correction, or new dashboard.",
            "",
            "NEXT: M5B-3 Real DeepSeek Shadow Pilot (not executed).",
            "",
            "STOP — M5B-2 only.",
            "",
        ]
    )
    return "\n".join(lines)
