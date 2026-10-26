import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from agent.graph import build_m1_graph
from agent.routing.decision import assess_risk, route_decision
from agent.state import ClaimState
from apps.api.config import Settings
from apps.api.main import create_app
from apps.api.repository import MockClaimRepository
from apps.api.workflow import AuditService
from domain.schemas import (
    AuditRequest,
    ClaimDetail,
    Decision,
    ERPStatus,
    HardGateResult,
    LLMAnalysis,
    OperationResponse,
    Outcome,
    RiskLevel,
)
from integrations.erp.mock import MockERP
from rag.retrieval.memory import InMemoryPolicyRetriever, PolicyCatalog
from rules.hard_gates import check_hard_gates
from rules.travel_rules import hotel_city_limit

CLAIM = "CLM-2026-00001"
EVENTS = [
    "CLAIM_LOADED",
    "ERP_CONTEXT_FETCHED",
    "DOCUMENTS_PARSED",
    "HARD_GATE_CHECKED",
    "RULES_CHECKED",
    "POLICY_RETRIEVED",
    "LLM_ANALYSIS_COMPLETED",
    "RISK_ASSESSED",
    "DECISION_MADE",
    "AUTO_APPROVED",
    "ERP_UPDATED",
    "WORKFLOW_COMPLETED",
]


def run_graph(repo=None, **adapters):
    repo = repo or MockClaimRepository()
    return ClaimState.model_validate(
        asyncio.run(
            build_m1_graph(repo, **adapters).ainvoke(
                {
                    "claim_id": CLAIM,
                    "run_id": uuid4(),
                    "audit_context": {"idempotency_key": "unit-m1"},
                }
            )
        )
    )


def policy_filters(incurred_on=date(2026, 9, 20)):
    return dict(legal_entity="DEMO-CN", category="HOTEL", city="深圳", incurred_on=incurred_on)


def retrieve(catalog=None, incurred_on=date(2026, 9, 20), version="POL-SZ-2026", **filters):
    return asyncio.run(
        InMemoryPolicyRetriever(catalog).retrieve(
            "深圳住宿每晚限额 900 元",
            **{**policy_filters(incurred_on), **filters},
            audit_started_at=datetime.now(UTC),
            authoritative_version_id=version,
        )
    )


@pytest.mark.parametrize(
    ("amount", "nights", "outcome", "limit"),
    [
        ("600.00", 1, "PASS", "700.00"),
        ("700.00", 1, "PASS", "700.00"),
        ("700.01", 1, "FAIL", "700.00"),
        ("1400.00", 2, "PASS", "1400.00"),
        ("1400.01", 2, "FAIL", "1400.00"),
    ],
)
def test_hotel_decimal_rule(amount, nights, outcome, limit):
    expense = (
        MockClaimRepository()
        .get(CLAIM)
        .expense.model_copy(update={"amount": Decimal(amount), "nights": nights})
    )
    rule = hotel_city_limit(expense, PolicyCatalog().resolve(**policy_filters()))
    assert rule.outcome == outcome
    assert rule.actual == Decimal(amount) and rule.limit == Decimal(limit)
    assert rule.policy_version_id == "POL-SZ-2026"


def test_hotel_missing_policy_is_unknown():
    assert hotel_city_limit(MockClaimRepository().get(CLAIM).expense, None).outcome == "UNKNOWN"


def test_hard_gate_happy_path():
    state = run_graph()
    assert len(check_hard_gates(state)) == 6
    assert all(g.outcome == "PASS" for g in state.hard_gate_results)


@pytest.mark.parametrize("field", ["employee", "expense", "documents"])
def test_hard_gate_missing_fields(field):
    state = run_graph()
    setattr(state, field, [] if field == "documents" else None)
    assert any(g.outcome == "FAIL" for g in check_hard_gates(state))


@pytest.mark.parametrize("gate", ["claim", "employee", "amount", "date", "document"])
def test_hard_gate_invalid_facts(gate):
    state = run_graph()
    if gate == "claim":
        state.audit_context["claim_loaded"] = False
    elif gate == "employee":
        state.erp_context["employee_active"] = False
    elif gate == "amount":
        state.expense.amount = Decimal("-0.01")
    elif gate == "date":
        state.expense.incurred_on = date(2099, 1, 1)
    else:
        state.documents[0].parse_status = "FAILED"
    assert any(g.outcome == "FAIL" for g in check_hard_gates(state))


@pytest.mark.parametrize(
    ("day", "version"),
    [
        (date(2024, 1, 1), "2024"),
        (date(2024, 12, 31), "2024"),
        (date(2025, 1, 1), "2025"),
        (date(2025, 12, 31), "2025"),
        (date(2026, 1, 1), "2026"),
        (date(2026, 9, 20), "2026"),
    ],
)
def test_policy_date_filter_half_open(day, version):
    evidence = retrieve(incurred_on=day, version=f"POL-SZ-{version}")
    assert [p.version for p in evidence] == [version]


@pytest.mark.parametrize(
    "filters",
    [
        {"city": "上海"},
        {"category": "MEAL"},
        {"legal_entity": "OTHER"},
    ],
)
def test_policy_metadata_rejects_wrong_context(filters):
    assert retrieve(**filters) == []


def test_2025_high_similarity_never_overrides_2026_authority():
    evidence = retrieve()  # Query is an exact match for the 2025 text, not 2026.
    assert [p.version for p in evidence] == ["2026"]
    assert "700" in evidence[0].quote and "900" not in evidence[0].quote
    assert retrieve(version="POL-SZ-2025") == []
    assert retrieve(incurred_on=date(2027, 1, 1)) == []


def test_policy_status_and_authority_selection():
    catalog = PolicyCatalog()
    original = catalog.documents[-1]
    draft = original.model_copy(
        update={"document_id": "draft", "status": "DRAFT", "authority_level": 999}
    )
    retired = original.model_copy(update={"document_id": "retired", "status": "RETIRED"})
    weak = original.model_copy(
        update={"document_id": "weak", "authority_level": 1, "hotel_limit": Decimal("900")}
    )
    catalog.documents.extend([draft, retired, weak])
    assert catalog.resolve(**policy_filters()).document_id == "POL-SZ-2026"
    assert retrieve(catalog)[0].version == "2026"


def test_conflicting_equal_authority_is_uncertain():
    catalog = PolicyCatalog()
    catalog.documents.append(
        catalog.documents[-1].model_copy(
            update={
                "document_id": "conflict",
                "hotel_limit": Decimal("900"),
            }
        )
    )
    assert catalog.resolve(**policy_filters()) is None
    assert retrieve(catalog) == []
    state = run_graph(catalog=catalog)
    assert state.decision == "HUMAN_REVIEW" and state.erp_status == "NOT_STARTED"


@pytest.mark.parametrize(
    ("risk", "decision"),
    [
        (RiskLevel.LOW, Decision.APPROVE),
        (RiskLevel.MEDIUM, Decision.HUMAN_REVIEW),
        (RiskLevel.HIGH, Decision.REJECT),
    ],
)
def test_decision_router(risk, decision):
    assert route_decision(risk) == decision


@pytest.mark.parametrize("missing", ["rule_results", "policy_evidence", "llm_analysis"])
def test_risk_requires_complete_evidence(missing):
    state = run_graph()
    assert assess_risk(state) == RiskLevel.LOW
    setattr(state, missing, None if missing == "llm_analysis" else [])
    assert assess_risk(state) == RiskLevel.MEDIUM


def test_risk_hard_gate_and_rule_have_authority_over_llm():
    state = run_graph()
    state.hard_gate_results = [
        HardGateResult(gate_id="failed", outcome=Outcome.FAIL, reason="test")
    ]
    assert assess_risk(state) == RiskLevel.HIGH
    state.hard_gate_results[0].outcome = Outcome.PASS
    state.rule_results[0].outcome = Outcome.FAIL
    assert assess_risk(state) == RiskLevel.HIGH


def test_risk_ambiguity_is_not_low():
    state = run_graph()
    state.llm_analysis.anomalies = ["Ambiguous invoice"]
    assert assess_risk(state) == RiskLevel.MEDIUM


def test_real_graph_state_adapters_and_order():
    repo = MockClaimRepository()
    state = run_graph(repo)
    assert state.employee.name == "张伟" and state.employee.department == "华南销售部"
    assert state.decision == "APPROVE" and state.risk_level == "LOW"
    assert state.requires_human is False and state.errors == []
    assert state.erp_status == "SUCCEEDED" and repo.erp_status(CLAIM) == "APPROVED"
    assert state.policy_evidence[0].version == "2026"
    events = state.audit_context["events"]
    assert [e["event_type"] for e in events] == EVENTS
    assert {e["payload"]["node"] for e in events} == {
        "load_claim",
        "fetch_erp_context",
        "parse_documents",
        "rule_check",
        "retrieve_policy",
        "llm_analysis",
        "risk_assessment",
        "decision_router",
        "auto_approve",
        "write_erp",
        "audit_log",
    }
    assert all(e["claim_id"] == CLAIM and e["run_id"] == str(state.run_id) for e in events)
    assert all({"node", "status", "summary", "duration_ms"} <= e["payload"].keys() for e in events)


def test_mock_erp_read_write_and_persistence(tmp_path):
    path = str(tmp_path / "mock.sqlite3")
    repo = MockClaimRepository(store_path=path)
    context = asyncio.run(MockERP(repo).fetch_claim_context(CLAIM))
    assert context["employee"]["name"] == "张伟" and context["department"] == "华南销售部"
    run_graph(repo)
    repo.close()
    reopened = MockClaimRepository(store_path=path)
    assert reopened.erp_status(CLAIM) == "APPROVED"
    reopened.close()


def test_api_happy_path_contract_and_log(client):
    response = client.post(
        f"/claims/{CLAIM}/audit", json={"idempotency_key": "m1-api", "mode": "LIVE"}
    )
    assert response.status_code == 202
    operation = OperationResponse.model_validate(response.json())
    assert operation.decision_result.decision == "APPROVE"
    assert operation.decision_result.risk_level == "LOW"
    assert operation.decision_result.requires_human is False
    assert operation.decision_result.policy_evidence[0].version == "2026"
    assert operation.status == "COMPLETED" and operation.revision == 1
    UUID(response.headers["X-Request-ID"])
    detail = ClaimDetail.model_validate(client.get(f"/claims/{CLAIM}").json())
    assert detail.status == "COMPLETED" and detail.erp_status == "SUCCEEDED"
    assert detail.active_run_id == operation.run_id
    log = client.get(f"/claims/{CLAIM}/audit-log").json()
    assert log["total"] == len(EVENTS) and [e["event_type"] for e in log["items"]] == EVENTS
    page = client.get(f"/claims/{CLAIM}/audit-log?limit=3&offset=3").json()
    assert page["total"] == 12 and page["items"] == log["items"][3:6]
    assert client.get("/dashboard/metrics").json()["by_decision"]["APPROVE"] == 1


def test_completed_run_and_events_survive_app_restart(tmp_path):
    settings = Settings(
        app_env="development", mock_store_path=str(tmp_path / "demo.db"), _env_file=None
    )
    with TestClient(create_app(settings)) as client:
        body = {"idempotency_key": "persisted", "mode": "LIVE"}
        response = client.post(f"/claims/{CLAIM}/audit", json=body).json()
    with TestClient(create_app(settings)) as client:
        assert client.get(f"/claims/{CLAIM}").json()["status"] == "COMPLETED"
        assert client.get(f"/claims/{CLAIM}/audit-log").json()["total"] == 12
        assert client.post(f"/claims/{CLAIM}/audit", json=body).json() == response


def test_basic_replay_conflict_and_no_second_write(client):
    url = f"/claims/{CLAIM}/audit"
    body = {"idempotency_key": "replay"}
    first = client.post(url, json=body)
    assert client.post(url, json=body).json() == first.json()
    assert client.get(f"/claims/{CLAIM}/audit-log").json()["total"] == 12
    conflict = client.post(url, json={"idempotency_key": "replay", "mode": "SHADOW"})
    assert (
        conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    )
    assert client.post(url, json={"idempotency_key": "another"}).status_code == 409


def test_shadow_is_explicitly_reserved_without_side_effects(client):
    before = client.get(f"/claims/{CLAIM}").json()
    response = client.post(
        f"/claims/{CLAIM}/audit", json={"idempotency_key": "shadow", "mode": "SHADOW"}
    )
    assert response.status_code == 501
    assert client.get(f"/claims/{CLAIM}").json() == before


class EmptyRetriever:
    async def retrieve(self, query, **filters):
        return []


class OptimisticLLM:
    async def analyze(self, request):
        return LLMAnalysis(
            summary="approve everything",
            suggested_risk=RiskLevel.LOW,
            confidence=1.0,
            provider="test",
            model="test",
        )


def test_llm_cannot_approve_missing_policy():
    repo = MockClaimRepository()
    state = run_graph(repo, retriever=EmptyRetriever(), provider=OptimisticLLM())
    assert state.decision == "HUMAN_REVIEW" and state.requires_human
    assert state.erp_status == "NOT_STARTED" and repo.erp_status(CLAIM) is None
    assert "auto_approve" not in [e["payload"]["node"] for e in state.audit_context["events"]]


def test_llm_cannot_override_hotel_failure(tmp_path):
    repo = MockClaimRepository()
    graph = build_m1_graph(repo, provider=OptimisticLLM())
    state = ClaimState.model_validate(
        asyncio.run(
            graph.ainvoke(
                {
                    "claim_id": "CLM-2026-001",
                    "run_id": uuid4(),
                    "audit_context": {"idempotency_key": "over-limit"},
                }
            )
        )
    )
    assert state.decision == "REJECT" and state.risk_level == "HIGH"
    assert state.erp_status == "NOT_STARTED" and repo.erp_status("CLM-2026-001") is None


class FailingERP(MockERP):
    async def write_decision(self, result, **kwargs):
        raise TimeoutError("mock write failure")


def test_erp_failure_does_not_report_success_or_retry():
    repo = MockClaimRepository()
    state = run_graph(repo, erp=FailingERP(repo))
    assert state.status == "FAILED" and state.erp_status == ERPStatus.FAILED
    assert state.errors[0].node == "write_erp"
    types = [e["event_type"] for e in state.audit_context["events"]]
    assert "ERP_UPDATED" not in types and types[-1] == "WORKFLOW_FAILED"
    assert types.count("WRITE_ERP_FAILED") == 1


class FailingLLM:
    async def analyze(self, request):
        raise RuntimeError("mock unavailable")


def test_llm_failure_is_recorded_and_prevents_erp():
    state = run_graph(provider=FailingLLM())
    assert state.errors[0].node == "llm_analysis"
    assert state.decision != "APPROVE" and state.erp_status == "NOT_STARTED"


def test_document_mismatch_cannot_pass(tmp_path):
    from integrations.documents.mock import MockDocumentParser

    path = tmp_path / "documents.json"
    path.write_text(json.dumps([]), encoding="utf-8")
    state = run_graph(parser=MockDocumentParser(path))
    assert state.decision == "REJECT"
    assert any(
        g.gate_id == "DOCUMENTS_VALID" and g.outcome == "FAIL" for g in state.hard_gate_results
    )


def test_service_persists_acceptance_before_graph_runs(tmp_path):
    repo = MockClaimRepository(store_path=str(tmp_path / "accepted.db"))
    service = AuditService(repo)
    original = service.graph_factory

    class ObservedGraph:
        def __init__(self, graph):
            self.graph = graph

        async def ainvoke(self, state, config):
            accepted = repo.operation(CLAIM, "acceptance")[1]
            assert accepted.status == "AUDITING" and accepted.decision_result is None
            assert repo.get(CLAIM).status == "AUDITING"
            return await self.graph.ainvoke(state, config)

        async def aget_state(self, config):
            return await self.graph.aget_state(config)

    service.graph_factory = lambda **kwargs: ObservedGraph(original(**kwargs))
    result = asyncio.run(service.audit(CLAIM, AuditRequest(idempotency_key="acceptance")))
    assert result.status == "COMPLETED"
