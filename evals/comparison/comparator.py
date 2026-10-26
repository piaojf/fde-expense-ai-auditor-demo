"""Deterministic, private comparison; consumes evidence and never executes a decision."""

from enum import StrEnum

from pydantic import TypeAdapter, ValidationError

from domain.schemas import AgentError, Decision, HardGateResult, RiskLevel, RuleResult
from evals.shadow_mode.schema import ReferenceSource, ShadowResult


class Disagreement(StrEnum):
    FALSE_APPROVAL = "FALSE_APPROVAL"
    FALSE_REJECTION = "FALSE_REJECTION"
    MISSED_HUMAN_REVIEW = "MISSED_HUMAN_REVIEW"
    UNNECESSARY_HUMAN_REVIEW = "UNNECESSARY_HUMAN_REVIEW"
    POLICY_MISMATCH = "POLICY_MISMATCH"
    RISK_LEVEL_MISMATCH = "RISK_LEVEL_MISMATCH"
    EVALUATION_ERROR = "EVALUATION_ERROR"
    REFERENCE_UNAVAILABLE = "REFERENCE_UNAVAILABLE"


SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
EFFECT_KEYS = ("erp_write_count", "claim_mutation_count", "human_review_action_count")


def expected_for(case):
    return {
        "decision": case.expected_decision.value,
        "risk": case.expected_risk.value,
        "requires_human": case.requires_human,
        "policy_evidence": case.expected_policy_evidence,
        "hard_block": case.hard_block,
    }


def compare_case(case, result, *, dataset_version, policy_sha256, run_id, prompt_version=None,
                 real_mode=False, simulated=False):
    """Golden labels are authoritative; duplicated shadow labels cannot replace them.

    Absent reference is an explicit unavailable outcome. Malformed GoldenCase is rejected
    by the existing dataset loader. Persisted redundant agent fields must agree; mismatch
    flags saved by an earlier runner are deliberately recomputed.
    """
    case_id = case.case_id if case else result.get("case_id") if isinstance(result, dict) else None
    reference = expected_for(case) if case else None
    output = {
        "case_id": case_id,
        "reference_source": ReferenceSource.SYNTHETIC_RULE_LABEL.value if case else None,
        "reference_decision": reference["decision"] if reference else None,
        "reference_risk": reference["risk"] if reference else None,
        "reference_requires_human": reference["requires_human"] if reference else None,
        "reference_policy": list(reference["policy_evidence"]) if reference else None,
        "reference_authoritative_policy": (
            case.applicable_policy.policy_version_id if case else None
        ),
        "agent_decision": None,
        "agent_risk": None,
        "agent_requires_human": None,
        "agent_policy": None,
        "agent_authoritative_policy": None,
        "agent_hard_block": None,
        "decision_match": False,
        "risk_match": False,
        "policy_match": False,
        "human_review_match": False,
        "reference_available": case is not None,
        "agent_result_valid": False,
        "execution_status": "MISSING",
        "dependency_failure": False,
        "dependency_diagnostics": [],
        "expected_dependency_failure": False,
        "disagreements": [],
        "severity": None,
        "match_status": "AGENT_UNAVAILABLE",
        "reason": [],
        "side_effects": None,
    }

    def add(category, severity, reason):
        output["disagreements"].append(
            {"category": category.value, "severity": severity, "reason": reason}
        )
        output["reason"].append(reason)

    def finish():
        if output["disagreements"]:
            output["severity"] = min(
                (d["severity"] for d in output["disagreements"]), key=SEVERITIES.index
            )
        return output

    if case is None:
        add(Disagreement.REFERENCE_UNAVAILABLE, "HIGH", "REFERENCE_LABEL_MISSING")
        output["match_status"] = "REFERENCE_UNAVAILABLE"
        return finish()
    if result is None:
        add(Disagreement.EVALUATION_ERROR, "HIGH", "AGENT_RESULT_MISSING")
        return finish()
    try:
        shadow = ShadowResult.model_validate(result)
    except (ValidationError, TypeError, ValueError):
        add(Disagreement.EVALUATION_ERROR, "HIGH", "AGENT_RECORD_INVALID")
        return finish()
    output["execution_status"] = shadow.execution_status
    row = shadow.evaluation_row
    if (
        shadow.reference_source != ReferenceSource.SYNTHETIC_RULE_LABEL
        or shadow.reference_decision != case.expected_decision
        or row.get("expected") != reference
        or row.get("applicable_policy") != case.applicable_policy.model_dump()
    ):
        output["reference_available"] = False
        output["match_status"] = "REFERENCE_UNAVAILABLE"
        add(Disagreement.REFERENCE_UNAVAILABLE, "HIGH", "REFERENCE_PROVENANCE_OR_LABEL_CONFLICT")
        return finish()
    if (
        shadow.case_id != case.case_id
        or row.get("case_id") != case.case_id
        or str(shadow.shadow_run_id) != run_id
        or row.get("evaluation_run_id") != run_id
        or shadow.dataset_version != dataset_version
        or shadow.policy_fixture_sha256 != policy_sha256
        or (prompt_version is not None and shadow.prompt_version != prompt_version)
    ):
        add(Disagreement.EVALUATION_ERROR, "HIGH", "CASE_RUN_OR_VERSION_CONFLICT")
        return finish()
    effects = shadow.side_effects
    raw_effects = result.get("side_effects", {})
    if all(type(raw_effects.get(k)) is int and effects[k] >= 0 for k in EFFECT_KEYS):
        output["side_effects"] = {k: effects[k] for k in EFFECT_KEYS}
    actual = row.get("actual")
    if not isinstance(actual, dict) or row.get("decision_schema_valid") is not True:
        add(Disagreement.EVALUATION_ERROR, "HIGH", "AGENT_OUTPUT_INVALID")
        return finish()
    policy = actual.get("policy_evidence")
    authority = actual.get("authoritative_version_id") or None
    probe = row.get("probe_result")
    if probe is not None and (
        not isinstance(probe, dict)
        or not all(
            type(probe.get(k)) in (int, float)
            for k in (
                "old_similarity",
                "current_similarity",
            )
        )
    ):
        add(Disagreement.EVALUATION_ERROR, "HIGH", "AGENT_PROBE_EVIDENCE_INVALID")
        return finish()
    try:
        TypeAdapter(list[AgentError]).validate_python(actual.get("errors"))
        rules = TypeAdapter(list[RuleResult]).validate_python(actual.get("rule_results"))
        gates = TypeAdapter(list[HardGateResult]).validate_python(actual.get("hard_gate_results"))
    except (ValidationError, ValueError, TypeError):
        add(Disagreement.EVALUATION_ERROR, "HIGH", "AGENT_EVIDENCE_INVALID")
        return finish()
    valid = (
        isinstance(actual.get("decision"), str)
        and actual["decision"] in {d.value for d in Decision}
        and isinstance(actual.get("risk"), str)
        and actual["risk"] in {r.value for r in RiskLevel}
        and type(actual.get("requires_human")) is bool
        and isinstance(policy, list)
        and all(isinstance(p, str) and p for p in policy)
        and len(policy) == len(set(policy))
        and "authoritative_version_id" in actual
        and (
            actual["authoritative_version_id"] is None
            or isinstance(actual["authoritative_version_id"], str)
        )
        and isinstance(actual.get("errors"), list)
        and type(actual.get("hard_block")) is bool
        and shadow.agent_decision == actual.get("decision")
        and shadow.agent_risk == actual.get("risk")
        and shadow.requires_human == actual.get("requires_human")
        and shadow.policy_version == authority
        and actual["requires_human"] == (actual["decision"] == "HUMAN_REVIEW")
        and shadow.execution_status != "FAILED"
        and not row.get("execution_error")
        and shadow.model_provider == ("deepseek" if real_mode else "mock")
        and (real_mode or shadow.token_usage.get("paid_api_calls") == 0)
        and (real_mode or shadow.token_usage.get("billable_tokens") == 0)
        and isinstance(row.get("llm_attempts"), list)
        and all(
            isinstance(a, dict)
            and a.get("source") in (
                ("simulated_deepseek_http" if simulated else "deepseek", "injected")
                if real_mode else ("mock", "injected")
            )
            and type(a.get("schema_valid")) is bool
            for a in row["llm_attempts"]
        )
    )
    if not valid:
        add(Disagreement.EVALUATION_ERROR, "HIGH", "AGENT_OUTPUT_OR_PROJECTION_INVALID")
        return finish()
    output.update(
        agent_decision=actual["decision"],
        agent_risk=actual["risk"],
        agent_requires_human=actual["requires_human"],
        agent_policy=list(policy),
        agent_authoritative_policy=authority,
        agent_result_valid=True,
        decision_match=reference["decision"] == actual["decision"],
        risk_match=reference["risk"] == actual["risk"],
        human_review_match=reference["requires_human"] == actual["requires_human"],
        policy_match=set(reference["policy_evidence"]) == set(policy)
        and case.applicable_policy.policy_version_id == authority,
    )
    output["agent_hard_block"] = (
        actual["hard_block"]
        or any(g.outcome != "PASS" for g in gates)
        or any(r.outcome == "FAIL" and r.severity == "BLOCK" for r in rules)
    )
    blocked = reference["hard_block"] or output["agent_hard_block"]
    if actual["decision"] == "APPROVE" and (reference["decision"] != "APPROVE" or blocked):
        severity = (
            "CRITICAL" if blocked else "HIGH" if reference["decision"] == "REJECT" else "MEDIUM"
        )
        add(
            Disagreement.FALSE_APPROVAL,
            severity,
            "HARD_BLOCK_AUTO_APPROVED" if blocked else "REFERENCE_NON_APPROVAL_AUTO_APPROVED",
        )
    if reference["decision"] == "APPROVE" and actual["decision"] == "REJECT":
        add(Disagreement.FALSE_REJECTION, "MEDIUM", "APPROVABLE_CLAIM_REJECTED")
    if reference["requires_human"] and not actual["requires_human"]:
        add(Disagreement.MISSED_HUMAN_REVIEW, "MEDIUM", "REQUIRED_HUMAN_JUDGMENT_BYPASSED")
    if not reference["requires_human"] and actual["requires_human"]:
        severity = "HIGH" if blocked else "LOW" if reference["decision"] == "APPROVE" else "MEDIUM"
        add(
            Disagreement.UNNECESSARY_HUMAN_REVIEW,
            severity,
            "HARD_BLOCK_NOT_REJECTED" if blocked else "UNNECESSARY_HUMAN_ESCALATION",
        )
    if not output["policy_match"]:
        add(
            Disagreement.POLICY_MISMATCH,
            "HIGH" if case.applicable_policy.policy_version_id else "LOW",
            "POLICY_EVIDENCE_OR_AUTHORITATIVE_VERSION_DIFFERS",
        )
    if not output["risk_match"]:
        add(Disagreement.RISK_LEVEL_MISMATCH, "LOW", "RISK_LABEL_DIFFERS")
    dependency = shadow.execution_status == "DEPENDENCY_FAILED" or bool(actual["errors"])
    output["dependency_failure"] = dependency
    expected_errors = set()
    if case.retrieval_fault == "TIMEOUT_ALWAYS":
        expected_errors.add(("retrieve_policy", "TimeoutError"))
        if real_mode:
            expected_errors.add(("llm_analysis", "PilotStopped"))
    if case.llm_fault == "TIMEOUT_ALWAYS":
        expected_errors.add(("llm_analysis", "TimeoutError"))
    if case.llm_fault in ("INVALID_OUTPUT", "INVALID_RESPONSE"):
        expected_errors.add(("llm_analysis", "ValidationError"))
    observed_errors = {(e["node"], e["message"]) for e in actual["errors"]}
    output["expected_dependency_failure"] = (
        dependency and bool(observed_errors) and observed_errors <= expected_errors
    )
    if dependency:
        output["dependency_diagnostics"] = [
            {
                "node": e["node"]
                if e["node"] in ("retrieve_policy", "llm_analysis")
                else "OTHER_NODE",
                "error_type": e["message"]
                if e["message"] in ("TimeoutError", "ValidationError")
                else "OTHER_DEPENDENCY_ERROR",
            }
            for e in actual["errors"]
        ]
        add(Disagreement.EVALUATION_ERROR, "MEDIUM", "DEPENDENCY_FAILURE_WITH_VALID_FALLBACK")
    fields_match = all(
        output[k]
        for k in (
            "decision_match",
            "risk_match",
            "policy_match",
            "human_review_match",
        )
    )
    output["match_status"] = (
        "MATCH_WITH_EVALUATION_ERROR"
        if fields_match and dependency
        else "MATCH"
        if fields_match
        else "MISMATCH"
    )
    if fields_match and not output["disagreements"]:
        output["reason"] = ["ALL_COMPARABLE_FIELDS_MATCH"]
    return finish()


def metric_row(case, result, comparison, run_id):
    """Normalize invalid output to ERROR before feeding unchanged M5A formulas."""
    raw = result.get("evaluation_row", {}) if isinstance(result, dict) else {}
    actual = (
        {**raw["actual"], "hard_block": comparison["agent_hard_block"]}
        if comparison["agent_result_valid"]
        else {}
    )
    return {
        "evaluation_run_id": run_id,
        "case_id": case.case_id,
        "category": case.category,
        "risk_category": case.risk_category,
        "expected": expected_for(case),
        "actual": actual,
        "execution_error": None if comparison["agent_result_valid"] else "UNAVAILABLE",
        "decision_schema_valid": comparison["agent_result_valid"],
        "llm_attempts": raw.get("llm_attempts", []) if comparison["agent_result_valid"] else [],
        "latency_ms": float(result["latency_ms"]) if comparison["agent_result_valid"] else 0,
        "retrieval_fault": case.retrieval_fault,
        "llm_fault": case.llm_fault,
        "probe_result": raw.get("probe_result") if comparison["agent_result_valid"] else None,
    }
