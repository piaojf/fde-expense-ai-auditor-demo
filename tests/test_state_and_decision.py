import json
import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent.graph import build_m0_graph
from agent.nodes.human_review import validate_human_authority
from agent.state import ClaimState
from domain.schemas import DecisionResult, Expense


def example_decision():
    contract = Path("docs/api-contract.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)\n```", contract, flags=re.S)
    return next(json.loads(block) for block in blocks if '"schema_version"' in block)


def test_contract_decision_example_is_valid():
    result = DecisionResult.model_validate(example_decision())
    assert result.decision == "REJECT"
    assert result.rule_results[0].actual == Decimal("800.00")


@pytest.mark.parametrize(
    "patch",
    [
        {"decision": "APPROVE", "risk_level": "LOW"},
        {"hard_gate_passed": False, "risk_level": "LOW"},
        {"requires_human": True},
        {"confidence": 1.1},
        {"decided_at": "2026-10-07T02:00:00"},
    ],
)
def test_inconsistent_decisions_are_rejected(patch):
    payload = example_decision()
    payload.update(patch)
    with pytest.raises(ValidationError):
        DecisionResult.model_validate(payload)


def test_unknown_cannot_auto_approve():
    payload = example_decision()
    payload.update(decision="APPROVE", risk_level="LOW")
    payload["rule_results"][0].update(outcome="UNKNOWN")
    with pytest.raises(ValidationError):
        DecisionResult.model_validate(payload)


def test_unknown_legacy_rule_is_rejected_before_hybrid_authority(monkeypatch):
    state = ClaimState(claim_id='legacy-unknown', decision='HUMAN_REVIEW', requires_human=True,
                       hard_gate_results=[dict(gate_id='VALID', outcome='PASS', reason='checked')],
                       rule_results=[dict(rule_id='HOTEL', outcome='UNKNOWN', severity='BLOCK',
                                          reason='missing facts')])
    monkeypatch.setattr('agent.nodes.human_review.check_hard_gates', lambda s: s.hard_gate_results)
    calls = []
    catalog = SimpleNamespace(validate_hybrid_authority=lambda s: calls.append(s) or True)
    with pytest.raises(ValueError, match='UNKNOWN'):
        validate_human_authority(state, catalog)
    assert calls == []


def manual_decision():
    payload = example_decision()
    payload.update(decision="APPROVE", risk_level="LOW")
    payload["rule_results"][0].update(outcome="UNKNOWN")
    payload['manual_approval'] = dict(decision='APPROVE', reviewer_id='finance-1',
                                      comment=payload['reason'], reviewed_at=payload['decided_at'])
    return payload


def test_unknown_can_have_a_separate_matching_manual_approval():
    payload = manual_decision()
    result = DecisionResult.model_validate(payload)
    assert result.rule_results[0].outcome == 'UNKNOWN'
    assert result.manual_approval.reviewer_id == 'finance-1'
    assert DecisionResult.model_validate_json(result.model_dump_json()) == result
    assert 'manual_approval' not in DecisionResult.model_validate(example_decision()).model_dump()


@pytest.mark.parametrize('mutation', ['reason', 'time', 'failed_rule', 'hard_gate', 'no_policy'])
def test_manual_record_cannot_override_hard_constraints_or_mismatch(mutation):
    payload = manual_decision()
    if mutation == 'reason':
        payload['manual_approval']['comment'] = 'different basis'
    elif mutation == 'time':
        payload['manual_approval']['reviewed_at'] = '2026-10-17T00:00:00Z'
    elif mutation == 'failed_rule':
        payload['rule_results'][0]['outcome'] = 'FAIL'
    elif mutation == 'hard_gate':
        payload['hard_gate_passed'] = False
    else:
        payload['policy_evidence'] = []
    with pytest.raises(ValidationError):
        DecisionResult.model_validate(payload)


def test_state_defaults_are_independent_and_unapproved():
    state = ClaimState(claim_id="one")
    other = ClaimState(claim_id="two")
    state.audit_context["test"] = True
    assert other.audit_context == {}
    assert state.decision is None and state.risk_level is None
    assert state.hard_gate_results == []
    assert set(ClaimState.model_json_schema()["properties"]) == {
        "claim_id",
        "run_id",
        "revision",
        "mode",
        "status",
        "employee",
        "expense",
        "documents",
        "erp_context",
        "rule_results",
        "hard_gate_results",
        "policy_evidence",
        "llm_analysis",
        "risk_level",
        "decision",
        "requires_human",
        "human_review",
        "erp_status",
        "errors",
        "audit_context",
    }


@pytest.mark.parametrize("amount", [800.0, "800.001", "-1.00", "NaN", "Infinity"])
def test_invalid_money(amount):
    with pytest.raises(ValidationError):
        Expense(category="HOTEL", city="深圳", amount=amount, incurred_on="2026-10-01")


def test_money_round_trip():
    expense = Expense(category="HOTEL", city="深圳", amount="800", incurred_on="2026-10-01")
    assert expense.amount == Decimal("800.00")
    assert expense.model_dump(mode="json")["amount"] == "800.00"


def test_m0_langgraph_only_loads_claim():
    graph = build_m0_graph()
    result = ClaimState.model_validate(graph.invoke({"claim_id": "CLM-2026-001"}))
    assert result.employee.employee_id == "EMP-001"
    assert result.expense.amount == Decimal("800.00")
    assert result.decision is None and result.risk_level is None
    assert result.erp_status == "NOT_STARTED"
    assert result.audit_context["milestone"] == "M0"
    assert set(graph.get_graph().nodes) == {"__start__", "load_claim", "__end__"}


def test_graph_missing_claim():
    with pytest.raises(ValueError, match="Claim not found"):
        build_m0_graph().invoke({"claim_id": "missing"})
