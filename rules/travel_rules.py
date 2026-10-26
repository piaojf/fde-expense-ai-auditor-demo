"""Decimal arithmetic belongs to the rule engine, never to the LLM."""

from domain.schemas import Expense, Outcome, RuleResult, Severity
from rag.retrieval.memory import PolicyDocument


def hotel_city_limit(expense: Expense | None, policy: PolicyDocument | None) -> RuleResult:
    if expense is None or expense.category != "HOTEL" or policy is None:
        return RuleResult(
            rule_id="HOTEL_CITY_LIMIT",
            outcome=Outcome.UNKNOWN,
            severity=Severity.REVIEW,
            reason="No unambiguous authoritative hotel policy for this expense",
        )
    limit = policy.hotel_limit * expense.nights
    passed = expense.amount <= limit
    return RuleResult(
        rule_id="HOTEL_CITY_LIMIT",
        outcome=Outcome.PASS if passed else Outcome.FAIL,
        severity=Severity.BLOCK,
        reason=f"{expense.city}住宿 {expense.amount:.2f} / 制度限额 {limit:.2f}",
        actual=expense.amount,
        limit=limit,
        policy_version_id=policy.document_id,
    )
