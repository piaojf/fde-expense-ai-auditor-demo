"""P4B deterministic aggregation. No model can lower a rule/hard-gate restriction."""


def aggregate(*, rules, gates, semantic, missing, conflicts, retrieval_valid):
    missing, conflicts = list(missing), list(conflicts)
    if semantic:
        missing.extend(semantic.missing_evidence)
        conflicts.extend(semantic.conflicts)
    if not gates or any(g.outcome.value != "PASS" for g in gates):
        return "NON_COMPLIANT", "REJECT", "HIGH", missing, conflicts
    if any(r.outcome.value == "FAIL" and r.severity.value == "BLOCK" for r in rules):
        return "NON_COMPLIANT", "REJECT", "HIGH", missing, conflicts
    if conflicts or (
        semantic and any(f.conclusion == "POLICY_CONFLICT" for f in semantic.semantic_findings)
    ):
        return "POLICY_CONFLICT", "HUMAN_REVIEW", "MEDIUM", missing, conflicts
    if not retrieval_valid or semantic is None:
        return "UNKNOWN", "HUMAN_REVIEW", "MEDIUM", missing, conflicts
    findings = {f.conclusion for f in semantic.semantic_findings}
    if "UNSUPPORTED" in findings:
        return "UNKNOWN", "HUMAN_REVIEW", "MEDIUM", missing, conflicts
    if "NON_COMPLIANT" in findings:
        return "NON_COMPLIANT", "HUMAN_REVIEW", "HIGH", missing, conflicts
    if (
        missing
        or "INSUFFICIENT_EVIDENCE" in findings
        or not rules
        or any(r.outcome.value != "PASS" for r in rules)
    ):
        return "INSUFFICIENT_EVIDENCE", "REQUEST_EVIDENCE", "MEDIUM", missing, conflicts
    if (
        semantic.exceptions
        or semantic.suggested_risk != "LOW"
        or semantic.recommended_action != "CHECK_PASSED"
    ):
        return "CHECK_PASSED", "HUMAN_REVIEW", semantic.suggested_risk, missing, conflicts
    return "CHECK_PASSED", "CHECK_PASSED", "LOW", missing, conflicts
