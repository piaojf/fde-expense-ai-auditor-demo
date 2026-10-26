"""Human authority resolves ambiguity; hard gates and failed rules retain authority."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import Field

from agent.state import ClaimState
from domain.schemas import (
    DTO,
    AuditEvent,
    Decision,
    DecisionResult,
    HumanReview,
    Outcome,
    RiskLevel,
    Severity,
)
from rules.hard_gates import check_hard_gates
from rules.travel_rules import hotel_city_limit


class HumanResumePayload(DTO):
    claim_id: str
    run_id: UUID
    checkpoint_id: str
    revision: int = Field(ge=0)
    review: HumanReview


def human_event(state, kind, review=None, **details):
    payload = {
        "node": "human_review",
        "status": "PENDING" if review is None else review.decision,
        "summary": kind if review is None else review.comment,
        **details,
    }
    if review:
        payload.update(reviewer=review.reviewer_id, action=review.decision, reason=review.comment)
    return AuditEvent(
        event_id=uuid4(),
        claim_id=state.claim_id,
        run_id=state.run_id,
        event_type=kind,
        actor=f"human:{review.reviewer_id}" if review else "agent:human_review",
        occurred_at=datetime.now(UTC),
        payload=payload,
    )


def validate_human_authority(state: ClaimState, catalog, decision="APPROVE"):
    """Recheck pure deterministic constraints without rerunning external adapters."""
    if state.decision != Decision.HUMAN_REVIEW or not state.requires_human:
        raise ValueError("Human review requires an unresolved HUMAN_REVIEW decision")
    if decision == "REJECT" and state.audit_context.get("mandatory_finance_review"):
        # Negative authority cannot approve a failed hard gate. This exception is
        # confined to the opt-in enterprise workflow and never affects legacy runs.
        return
    if not state.hard_gate_results or not state.rule_results:
        raise ValueError("Incomplete or failed workflow is not approvable")
    if any(g.outcome != Outcome.PASS for g in state.hard_gate_results):
        raise ValueError("Hard gate cannot be overridden")
    if any(g.outcome != Outcome.PASS for g in check_hard_gates(state)):
        raise ValueError("Hard gate recheck failed")
    if any(r.outcome == Outcome.FAIL and r.severity == Severity.BLOCK for r in state.rule_results):
        raise ValueError("BLOCK/FAIL cannot be overridden")
    if decision == "REJECT":
        return
    if state.errors:
        raise ValueError("Failed workflow cannot be overridden")
    if (any(r.outcome != Outcome.PASS for r in state.rule_results)
            and not state.audit_context.get("p4b_advisory_mode")):
        raise ValueError("Failed or UNKNOWN rule cannot be overridden")
    if (hasattr(catalog, "validate_hybrid_authority")
            and catalog.validate_hybrid_authority(state)):
        return
    if any(r.outcome != Outcome.PASS for r in state.rule_results):
        raise ValueError("Failed or UNKNOWN rule cannot be overridden")
    if hasattr(catalog, "validate_hotel_authority") and catalog.validate_hotel_authority(state):
        return
    if "travel_policy_ids" in state.audit_context:
        checked = catalog.evaluate_travel(state, pinned=True)
        if (checked is None or checked["rules"] != state.rule_results
                or checked["evidence"] != state.policy_evidence
                or any(r.outcome != Outcome.PASS for r in checked["rules"])):
            raise ValueError("Pinned travel standard recheck failed")
        return
    filters = dict(
        legal_entity=state.employee.legal_entity, category=state.expense.category,
        city=state.expense.city, incurred_on=state.expense.incurred_on,
    )
    if hasattr(catalog, "resolve_pinned"):
        policy = catalog.resolve_pinned(
            **filters, policy_version_id=state.rule_results[0].policy_version_id,
            audit_started_at=datetime.fromisoformat(state.audit_context["started_at"]),
        )
    else:
        policy = catalog.resolve(**filters)
    checked = hotel_city_limit(state.expense, policy)
    if checked.outcome != Outcome.PASS or checked != state.rule_results[0]:
        raise ValueError("Deterministic rule recheck failed or policy changed")
    if not state.policy_evidence or any(
        p.policy_version_id != checked.policy_version_id
        or p.effective_from > state.expense.incurred_on
        or (p.effective_to is not None and state.expense.incurred_on >= p.effective_to)
        for p in state.policy_evidence
    ):
        raise ValueError("Authoritative policy evidence must remain valid")


def human_decision_result(state: ClaimState, review: HumanReview, catalog) -> DecisionResult:
    validate_human_authority(state, catalog, review.decision)
    blocked = not state.hard_gate_results or not all(
        g.outcome == Outcome.PASS for g in state.hard_gate_results
    ) or any(
        r.outcome == Outcome.FAIL and r.severity == Severity.BLOCK for r in state.rule_results
    )
    enterprise_reject = (review.decision == "REJECT"
                         and state.audit_context.get("mandatory_finance_review"))
    return DecisionResult(
        claim_id=state.claim_id,
        run_id=state.run_id,
        decision=Decision(review.decision),
        risk_level=RiskLevel.HIGH if enterprise_reject and blocked else
        RiskLevel.LOW if review.decision == "APPROVE" else RiskLevel.MEDIUM,
        hard_gate_passed=bool(state.hard_gate_results) and all(
            g.outcome == Outcome.PASS for g in state.hard_gate_results
        ) if enterprise_reject else True,
        rule_results=state.rule_results,
        policy_evidence=state.policy_evidence,
        reason=review.comment,
        confidence=1.0,
        requires_human=False,
        decided_at=review.reviewed_at,
        manual_approval=review if review.decision == "APPROVE" and
        state.audit_context.get("p4b_advisory_mode") else None,
    )


def validate_resume_payload(state, raw, run_context):
    payload = HumanResumePayload.model_validate(raw)
    if (
        payload.claim_id != state.claim_id
        or payload.run_id != state.run_id
        or payload.checkpoint_id != run_context["checkpoint_id"]
        or payload.revision != run_context["revision"]
        or payload.review.model_dump(mode="json") != run_context.get("human_review")
        or not run_context.get("interrupted")
    ):
        raise ValueError("Resume must match the persisted review and original checkpoint")
    return payload
