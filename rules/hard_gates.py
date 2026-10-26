"""Small deterministic gates; missing facts never imply a pass."""

from datetime import date

from agent.state import ClaimState
from domain.schemas import HardGateResult, Outcome


def check_hard_gates(state: ClaimState) -> list[HardGateResult]:
    expense, employee = state.expense, state.employee
    checks = {
        "CLAIM_EXISTS": bool(state.audit_context.get("claim_loaded")),
        "EMPLOYEE_EXISTS": employee is not None
        and state.erp_context.get("employee_active") is True
        and bool(state.erp_context.get("employee")),
        "AMOUNT_VALID": expense is not None and expense.amount.is_finite() and expense.amount > 0,
        "DATE_VALID": expense is not None
        and isinstance(expense.incurred_on, date)
        and expense.incurred_on <= date.fromisoformat(state.audit_context["business_date"]),
        "REQUIRED_FIELDS": employee is not None
        and expense is not None
        and bool(employee.employee_id and employee.name and employee.department and expense.city)
        and bool(state.documents),
        "DOCUMENTS_VALID": bool(state.documents)
        and all(d.parse_status == "PARSED" for d in state.documents)
        and state.audit_context.get("document_facts_match") is True,
    }
    if employee is not None and state.erp_context.get("employee"):
        checks["EMPLOYEE_EXISTS"] = checks["EMPLOYEE_EXISTS"] and (
            state.erp_context["employee"].get("employee_id") == employee.employee_id
        )
    return [
        HardGateResult(
            gate_id=key,
            outcome=Outcome.PASS if passed else Outcome.FAIL,
            reason="Validated" if passed else "Missing or invalid authoritative facts",
        )
        for key, passed in checks.items()
    ]
