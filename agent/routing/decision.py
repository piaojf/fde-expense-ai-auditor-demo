"""Conservative deterministic authority; analysis is not approval authority."""

from agent.state import ClaimState
from domain.schemas import Decision, Outcome, RiskLevel, Severity


def assess_risk(state: ClaimState) -> RiskLevel:
    if not state.hard_gate_results or any(
        g.outcome != Outcome.PASS for g in state.hard_gate_results
    ):
        return RiskLevel.HIGH
    if any(r.outcome == Outcome.FAIL and r.severity == Severity.BLOCK for r in state.rule_results):
        return RiskLevel.HIGH
    if state.audit_context.get("p4b_enabled"):
        report = state.audit_context.get("p4b_report")
        if not report or report.get("analysis_status") != "SUCCEEDED":
            return RiskLevel.MEDIUM
        if report.get("recommended_action") == "REJECT":
            return RiskLevel.HIGH
        if (
            report.get("compliance_status") != "CHECK_PASSED"
            or report.get("recommended_action") != "CHECK_PASSED"
        ):
            return RiskLevel.MEDIUM
        expected = {r.policy_version_id for r in state.rule_results}
        if (
            state.errors
            or not state.rule_results
            or not state.policy_evidence
            or any(r.outcome != Outcome.PASS for r in state.rule_results)
            or state.expense is None
            or any(
                p.policy_version_id not in expected
                or (p.effective_from > state.expense.incurred_on
                    and not state.audit_context.get("hotel_rule_result"))
                or (p.effective_to is not None and state.expense.incurred_on >= p.effective_to)
                for p in state.policy_evidence
            )
        ):
            return RiskLevel.MEDIUM
        # P4B uses its own stronger evidence gate; an employee note is not by itself
        # an adverse finding after a validated semantic review and human fact check.
        return RiskLevel.LOW
    if (
        state.errors
        or state.audit_context.get("business_analysis", {}).get("ambiguity")
        or not state.rule_results
        or any(r.outcome != Outcome.PASS for r in state.rule_results)
        or not state.policy_evidence
        or state.llm_analysis is None
        or state.llm_analysis.anomalies
    ):
        return RiskLevel.MEDIUM
    if state.llm_analysis.suggested_risk != RiskLevel.LOW:
        return RiskLevel.MEDIUM
    expense = state.expense
    expected = {r.policy_version_id for r in state.rule_results}
    if expense is None or any(
        p.policy_version_id not in expected
        or (p.effective_from > expense.incurred_on
            and not state.audit_context.get("hotel_rule_result"))
        or (p.effective_to is not None and expense.incurred_on >= p.effective_to)
        for p in state.policy_evidence
    ):
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def route_decision(risk: RiskLevel) -> Decision:
    return {
        RiskLevel.LOW: Decision.APPROVE,
        RiskLevel.MEDIUM: Decision.HUMAN_REVIEW,
        RiskLevel.HIGH: Decision.REJECT,
    }[risk]
