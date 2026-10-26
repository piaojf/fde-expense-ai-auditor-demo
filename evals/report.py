"""Render measured artifacts; never substitute illustrative scores for actual results."""


def fraction(metric):
    value = metric["value"]
    percent = f"{value:.2%}" if value is not None else "undefined"
    return f"{metric['numerator']}/{metric['denominator']} ({percent})"


def render_report(run, artifact):
    m = run["metrics"]
    lines = [
        "# M5A Evaluation Report",
        "",
        f"Evaluation execution: **{run['evaluation_status']}**; mode `{run['mode']}`.",
        "Framework checkpoint acceptance is documented separately below; production business",
        "automatic approval accuracy threshold: **NOT_CERTIFIED**.",
        "",
        f"Run ID: `{run['run_id']}`; started `{run['started_at']}`; "
        f"completed `{run['completed_at']}`.",
        f"Actual results: [{artifact.name}]({artifact.name}); "
        f"{run['case_count']} **Synthetic** cases.",
        "These are simulated inputs, not actual enterprise historical reimbursements.",
        f"Dataset SHA256: `{run['dataset_sha256']}`.",
        f"Policy SHA256: `{run['policy_sha256']}`.",
        f"Execution Git base: `{run['git_commit']}`; dirty working tree: {run['git_dirty']}.",
        f"Model: `{run['model']}`; prompt version SHA256: `{run['prompt_version_sha256']}`.",
        f"Retrieval: {run['retrieval']}.",
        "",
        "## Dataset and label quality",
        "",
        "Each input has explicit policy/version, decision, risk, human flag, evidence IDs,",
        "rule basis, rationale and risk category. Labels come from independent Decimal/date/scope",
        "calculations and manual-review requirements. No evaluated LLM authored ground truth.",
        "Extra stays require finance verification of necessity, trip dates and proof;",
        "labels are not certified by an enterprise finance reviewer.",
        "",
        "| Category | Cases |",
        "| --- | ---: |",
    ]
    lines += [f"| {category} | {count} |" for category, count in sorted(m["categories"].items())]
    lines += [
        "",
        "## Metrics",
        "",
        "Exact numerator/denominator definitions: [specification](m5a-evaluation-spec.md).",
        "Execution errors stay in denominators. Undefined denominators are null.",
        "",
        "| Metric | Measured numerator / denominator |",
        "| --- | ---: |",
    ]
    names = {
        "decision_accuracy": "Decision Accuracy",
        "false_approval_unsafe_opportunity": "False Approval / should not auto-approve",
        "false_approval_contamination": "False Approval / all actual approvals",
        "false_rejection_rate": "False Rejection / expected approvals",
        "human_review_rate": "Human Review Rate",
        "automation_rate": "Automation Rate",
        "policy_retrieval_accuracy": "Policy Retrieval Accuracy (including abstention)",
        "policy_retrieval_available": "Policy Retrieval (available policy, no RAG fault)",
        "policy_retrieval_fault_cases": "Policy Retrieval (injected fault, empty expected)",
        "schema_validation_rate": "LLM Schema Validation / all attempted calls",
        "decision_schema_rate": "Decision DTO Validation / cases",
        "risk_accuracy": "Risk Accuracy",
        "human_flag_accuracy": "Human Flag Accuracy",
    }
    lines += [f"| {label} | {fraction(m[key])} |" for key, label in names.items()]
    lines += [
        f"| Hard Block Violation Count | {m['hard_block_violations']} |",
        "",
        "LLM schema rate includes deliberately invalid responses and dependency timeouts.",
        "Their correct safe routing is required; schema success is not adjusted to hide them.",
        "",
        "## Confusion matrix",
        "",
        "Rows = expected; columns = actual.",
        "",
        "| Expected | APPROVE | REJECT | HUMAN_REVIEW | ERROR |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for expected, counts in m["confusion_matrix"].items():
        lines.append(f"| {expected} | " + " | ".join(str(v) for v in counts.values()) + " |")
    lines += [
        "",
        "## Results by business risk",
        "",
        "| Risk category | Cases | Decision correct | False approvals | Label mismatches |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for category, group in m["risk_groups"].items():
        lines.append(f"| {category} | " + " | ".join(str(v) for v in group.values()) + " |")
    lines += ["", "## Error cases and governance", ""]
    lines += [f"- `{r['case_id']}`: {', '.join(r['mismatches'])}" for r in m["error_cases"]]
    if not m["error_cases"]:
        lines.append(
            "No expected/actual decision, risk, human flag, evidence or execution mismatches."
        )
    lines.append(
        "Expected injected dependency errors: " + ", ".join(m["expected_dependency_failures"]) + "."
    )
    lines.append("")
    lines += [
        f"- {gate}: {'NOT_RUN' if value is None else 'PASS' if value else 'FAIL'}"
        for gate, value in run["governance"].items()
    ]
    lines += [
        "",
        "The 780/700 probe verifies FAIL/BLOCK, REJECT and zero MockERP writes.",
        "Similarity probes record old/current lexical scores and current policy IDs.",
        "Invalid JSON/schema probes record failed validation and safe review, zero writes.",
        "Each case retains expected/actual results, rules, evidence, errors and audit events.",
        "",
        "## Latency and real calling cost",
        "",
        "| Graph latency ms | Measured |",
        "| --- | ---: |",
    ]
    lines += [
        f"| {key} | {value:.3f} |" for key, value in m["latency_ms"].items() if value is not None
    ]
    lines += [
        "",
        "Graph latency includes local parsing, retries and disposable MockERP;",
        "excludes fixture setup. LLM attempt latency is retained in JSON; p95 is nearest rank.",
        f"Paid API calls: {m['paid_api_calls']}; mock/injected attempts: "
        f"{m['injected_or_mock_attempts']}; maximum online calls: {run['max_live_calls']}.",
        "",
        "| Token field | Reported sum | Calls reporting | Calls missing usage |",
        "| --- | ---: | ---: | ---: |",
    ]
    for key, value in m["token_usage"].items():
        lines.append(f"| {key} | " + " | ".join(str(v) for v in value.values()) + " |")
    if run["mode"] == "mock-offline":
        lines += [
            "",
            "LIVE_DEEPSEEK_EVALUATION: NOT_RUN. Billable token usage and API cost are zero.",
            "No online latency/cost or live accuracy observation was made in M5A.",
        ]
    else:
        lines += [
            "",
            "Injected faults are simulated and consume no paid calls. Missing API usage is",
            "unknown; reported sums are partial when coverage is incomplete. Monetary cost",
            "requires actual billing/rate evidence; no cost or live accuracy is fabricated.",
        ]
    lines += [
        "",
        "## Repeatable commands",
        "",
        "From repository root:",
        "",
        "```bash",
        ".venv/bin/python -m evals.runner --output output/m5a-local/repeat.json \\",
        "  --report output/m5a-local/repeat.md",
        "# Offline subset; overall governance cannot be certified without all required probes:",
        ".venv/bin/python -m evals.runner --case hard_block-01 \\",
        "  --output output/m5a-local/subset.json",
        "# Only when explicitly choosing paid API calls; maximum 72, no paid retries:",
        ".venv/bin/python -m evals.runner --live-deepseek --max-calls 72 \\",
        "  --output output/m5a-local/live.json --report output/m5a-local/live.md",
        "```",
        "",
        "Use a fresh output path for each run; existing results are never overwritten.",
        "Optional `--dataset PATH` and UUID `--run-id UUID` select dataset/run identity.",
        "",
        "## Known limitations",
        "",
        "- Synthetic 72-case sample contains related variants, not independent production draws.",
        "- MockLLM accuracy is framework evidence; it cannot establish real DeepSeek accuracy.",
        "- Offline retrieval is lexical; pgvector/BGE-M3 are covered by separate regression gates.",
        "- Initial decision only; no human approval is invented or resumed by the runner.",
        "- Mock invoice facts; no OCR, fraud/authenticity or real ERP acceptance.",
        "- Dates use existing graph business clock in Asia/Shanghai; run records business_date.",
        "- Live results can vary; budget exhaustion fails, usage metadata may be unavailable.",
        "- No enterprise expert label review or production business threshold certification.",
        "- Confidence calibration, financial-loss weighting and statistical confidence intervals",
        "  require a separately approved representative dataset and business thresholds.",
        "",
        "NEXT: M5B Shadow Mode (not implemented). STOP.",
        "",
    ]
    return "\n".join(lines)
