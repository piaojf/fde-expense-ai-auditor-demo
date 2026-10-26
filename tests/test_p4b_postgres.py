"""Published original source -> genuine graph/checkpoint -> finance/Mock ERP tests."""

import os
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_p4a_postgres import add_booking, submit_hotel
from test_p4a_postgres import hotel_portal as hotel_portal
from test_policies_postgres import policy_pg as policy_pg

from apps.api.compliance_api import install_compliance_api
from apps.api.config import Settings
from apps.api.main import create_app
from database.models import PolicyUpload, PolicyVersion
from domain.compliance import SemanticAnalysis
from integrations.llm.compliance import (
    SEMANTIC_TOPICS,
    ComplianceDeepSeekProvider,
    OfflineComplianceProvider,
)
from rag.retrieval.compliance import ComplianceRetriever
from rag.retrieval.pgvector import PgPolicyCatalog
from tests.test_enterprise import action, login

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="Disposable PostgreSQL required"),
]
BASE = "/api/v1.5/p4b/v1"


class ContractFixtureProvider:
    """Authored schema fixtures; no keyword classification and no real semantic accuracy."""

    model = "synthetic-contract-fixture"

    def __init__(self):
        self.requests = []
        self.response = None
        self.fail = False

    async def analyze(self, request):
        self.requests.append(request)
        # Serialize actual graph facts/evidence through the real bounded transport
        # contract, without sending any HTTP request.
        ComplianceDeepSeekProvider(api_key="synthetic-test-key", model=self.model)._body(request)
        if self.fail:
            raise TimeoutError("synthetic timeout")
        data = self.response or dict(
            semantic_findings=[
                dict(
                    finding_id=topic,
                    issue_type=topic,
                    conclusion="COMPLIANT",
                    evidence_refs=[request.evidence[0].chunk_id],
                    factual_basis=["trusted.employee_fact"],
                    uncertainty="",
                    explanation="人工编写的合成契约响应",
                )
                for topic in sorted(SEMANTIC_TOPICS)
            ],
            missing_evidence=[],
            conflicts=[],
            exceptions=[],
            suggested_risk="LOW",
            recommended_action="CHECK_PASSED",
            explanation="合成审核响应；不构成真实语义能力证明",
            provider="synthetic-contract",
            model=self.model,
        )
        return SemanticAnalysis.model_validate(data)


@pytest.fixture
def hybrid(hotel_portal):
    c, app, repo, store, users, pid = hotel_portal
    provider = ContractFixtureProvider()
    install_compliance_api(
        app.state.enterprise_app,
        Settings(
            app_env="test",
            enterprise_enabled=True,
            p4a_enabled=True,
            p4b_enabled=True,
            _env_file=None,
        ),
        repo,
        app.state.audit_service,
        provider,
        business_clock=lambda: date(2027, 1, 5),
    )
    login(c, "admin")
    source = c.get(BASE + "/policies/" + pid)
    assert source.status_code == 200, source.text
    confirmation = c.post(
        BASE + "/policies/" + pid + "/confirm-scope",
        json=dict(
            extraction_digest=source.json()["rules"]["extraction_digest"],
            confirmed=True,
            reason="合成制度正文及两项金额规则人工核对",
            idempotency_key=str(uuid4()),
        ),
    )
    assert confirmation.status_code == 200, confirmation.text
    return hotel_portal, provider


def nonhotel(env, kind="ALLOWANCE", amount="36.00", notes="客户提供午餐及晚餐", invoice=None):
    c = env[0]
    login(c)
    body = dict(
        expense_type=kind,
        city="深圳",
        amount=amount,
        incurred_on="2026-11-02",
        trip_start="2026-11-02",
        trip_end="2026-11-02",
        purpose="客户项目现场工作",
        notes=notes,
        seller="合成费用主体",
        invoice=invoice,
        candidate_full_days=1 if kind == "ALLOWANCE" else None,
        candidate_lunches=1 if kind == "ALLOWANCE" else None,
        candidate_dinners=1 if kind == "ALLOWANCE" else None,
        idempotency_key=str(uuid4()),
    )
    result = c.post(BASE + "/claims", json=body)
    assert result.status_code == 202, result.text
    cid = result.json()["claim_id"]
    assert c.post(BASE + "/claims", json=body).json()["claim_id"] == cid
    return c.get("/api/v1.5/claims/" + cid).json()


def verify_and_reaudit(env, claim, **facts):
    c = env[0]
    login(c, "finance")
    common = dict(
        business_related=True, invoice_verified=True, prior_approval=True, duplicate_clear=True
    )
    body = dict(
        facts={**common, **facts},
        expected_revision=0,
        source_reference="合成第三方业务记录-001",
        basis="财务独立核对合成票据及业务记录",
        confirmed=True,
        idempotency_key=str(uuid4()),
    )
    result = c.post(BASE + f"/claims/{claim['claim_id']}/verify", json=body)
    assert result.status_code == 200, result.text
    assert c.post(BASE + f"/claims/{claim['claim_id']}/verify", json=body).json() == result.json()
    result = c.post(
        BASE + f"/claims/{claim['claim_id']}/reaudit",
        json=dict(expected_revision=claim["revision"], idempotency_key=str(uuid4())),
    )
    assert result.status_code == 202, result.text
    return c.get("/api/v1.5/claims/" + claim["claim_id"]).json()


def test_case_1_p4a_850_limit_800_and_original_row(hybrid):
    env, provider = hybrid
    stay = add_booking(env)
    cid, _, _ = submit_hotel(env, stay, amount="850")
    detail = env[0].get("/api/v1.5/claims/" + cid).json()
    report = detail["compliance_analysis"]
    assert report["compliance_status"] == "NON_COMPLIANT"
    assert report["rule_results"][0]["limit"] == "800.00"
    assert provider.requests[-1].rule_results[0]["actual"] == "850.00"
    proof = provider.requests[-1].facts["trusted"]["policy_verification"]
    assert proof["publication_and_authority_checked"] is True
    assert proof["policy_version_id"] == report["policy_version_id"]
    assert proof["effective_from"] <= proof["incurred_on"] < proof["effective_to"]
    assert proof["scope_confirmation"]["extraction_digest"]
    assert any("D 普通员工 | 800" in e["source_text"] for e in report["supporting_evidence"])
    login(env[0], "finance")
    assert action(env[0], detail, "APPROVE").status_code == 409
    assert env[2].erp_write_count(cid) == 0


def test_case_2_contrasting_notes_reach_provider_without_changing_monetary_rule(hybrid):
    env, provider = hybrid
    stay = add_booking(env)
    cid, _, body = submit_hotel(env, stay, amount="680")
    initial = env[0].get("/api/v1.5/claims/" + cid).json()
    original = provider.requests[-1]
    # The fixture is explicitly authored, not generated by a keyword/case classifier.
    # This proves the two actual submissions and structured results travel through
    # the graph; real semantic discrimination remains a separate live-model gate.
    provider.response = {
        k: initial["compliance_analysis"][k]
        for k in (
            "semantic_findings",
            "missing_evidence",
            "conflicts",
            "exceptions",
            "suggested_risk",
            "recommended_action",
            "explanation",
            "provider",
            "model",
        )
    }
    provider.response.update(
        missing_evidence=["航班取消证明", "核实行程", "负责人说明及授权"],
        exceptions=["客观延误需独立核实"],
        suggested_risk="MEDIUM",
        recommended_action="REQUEST_EVIDENCE",
    )
    body["fields"]["notes"] = "航班取消，必须额外多住一晚"
    body["idempotency_key"] = str(uuid4())
    result = env[0].post("/api/v1.5/p4a/v1/claims", json=body)
    assert result.status_code == 202, result.text
    second = env[0].get("/api/v1.5/claims/" + result.json()["claim_id"]).json()
    changed = provider.requests[-1]
    assert changed.rule_results == original.rule_results
    assert changed.facts != original.facts
    assert any(e.clause_id == "第十三条" for e in changed.evidence)
    assert (
        second["compliance_analysis"]["exceptions"] != initial["compliance_analysis"]["exceptions"]
    )
    assert "VERIFIED_CANCELLATION_NOTICE" in second["compliance_analysis"]["missing_evidence"]
    assert initial["compliance_analysis"]["missing_evidence"]
    login(env[0], "finance")
    assert action(env[0], initial, "APPROVE").status_code == 409


def test_no_relevant_source_stops_model_and_approval(hybrid, monkeypatch):
    env, provider = hybrid
    add_booking(env)
    monkeypatch.setattr("apps.api.compliance.rank_chunks", lambda *a, **k: [])
    cid, _, _ = submit_hotel(env, "stay-1", amount="680")
    detail = env[0].get("/api/v1.5/claims/" + cid).json()
    assert detail["compliance_analysis"]["analysis_status"] == "NO_POLICY"
    assert not provider.requests and env[2].erp_write_count(cid) == 0
    login(env[0], "finance")
    assert action(env[0], detail, "APPROVE").status_code == 409


def test_case_3_verified_meals_reaudit_preserves_old_report_and_erp_idempotency(hybrid):
    env, _ = hybrid
    add_booking(env)
    first = nonhotel(env)
    assert first["compliance_analysis"]["rule_results"][0]["outcome"] == "UNKNOWN"
    cid = first["claim_id"]
    old_run = first["claim"]["active_run_id"]
    final = verify_and_reaudit(env, first, full_days=1, lunches=1, dinners=1)
    report = final["compliance_analysis"]
    assert report["rule_results"][0]["limit"] == "36.00"
    assert report["compliance_status"] == "CHECK_PASSED" and final["stage"] == "FINANCE_REVIEW"
    cat = env[1].state.audit_service.catalog
    assert cat.report(cid, old_run) == first["compliance_analysis"]
    assert env[2].run_context(cid, old_run)["superseded"]
    assert len(final["history"]) == 2 and env[2].erp_write_count(cid) == 0
    evidence = report["supporting_evidence"][0]
    fetched = env[0].get(BASE + f"/claims/{cid}/evidence/{evidence['chunk_id']}")
    assert fetched.status_code == 200 and fetched.json() == evidence
    key = str(uuid4())
    result = action(env[0], final, "APPROVE", key=key)
    assert result.status_code == 200, result.text
    assert action(env[0], final, "APPROVE", key=key).status_code == 200
    assert env[2].erp_write_count(cid) == 1


def test_case_4_attendees_missing_then_four_general_customers_fail(hybrid):
    env, _ = hybrid
    add_booking(env)
    first = nonhotel(env, "ENTERTAINMENT", "1200.00", "与客户讨论明年采购合同")
    assert first["compliance_analysis"]["compliance_status"] == "INSUFFICIENT_EVIDENCE"
    assert "VERIFIED_PARTICIPANTS" in first["compliance_analysis"]["missing_evidence"]
    latest = verify_and_reaudit(
        env, first, participants=4, entertainment_category="GENERAL", customer_identified=True
    )
    rule = latest["compliance_analysis"]["rule_results"][0]
    assert rule["outcome"] == "FAIL" and rule["limit"] == "1000.00"
    assert "300.00" in rule["reason"]
    assert action(env[0], latest, "APPROVE").status_code == 409
    assert env[2].erp_write_count(first["claim_id"]) == 0


def test_actual_invoice_binding_original_integrity_and_finance_boundary(hybrid):
    from test_invoices import FIELDS

    env, _ = hybrid
    c, app, repo = env[:3]
    add_booking(env)
    did = str(uuid4())
    assert c.post("/api/v1.5/invoices/drafts", json={"draft_id": did}).status_code == 200
    base = "/api/v1.5/invoices/drafts/" + did
    attachment = c.post(
        base + "/attachments",
        files={
            "file": (
                "synthetic.pdf",
                Path("tests/fixtures/invoices/text-invoice.pdf").read_bytes(),
                "application/pdf",
            )
        },
    )
    assert attachment.status_code == 200, attachment.text
    aid = attachment.json()["attachment_id"]
    assert c.post(base + "/attachments/" + aid + "/ocr", json={}).status_code == 200
    invoice = dict(
        draft_id=did,
        attachment_id=aid,
        confirmed=True,
        confirmed_fields=FIELDS,
        discrepancy_reason="合成票据与申报金额不同，由财务核实",
    )
    first = nonhotel(env, "ENTERTAINMENT", "1000.00", "合成客户采购会议工作餐", invoice)
    assert first["invoice_evidence"]["attachment"]["sha256"]
    assert "VERIFIED_PARTICIPANTS" in first["compliance_analysis"]["missing_evidence"]
    reviewed = verify_and_reaudit(
        env, first, participants=4, entertainment_category="GENERAL", customer_identified=True
    )
    assert reviewed["compliance_analysis"]["compliance_status"] == "CHECK_PASSED"
    original = app.state.invoice_store.path(aid)
    original.write_bytes(b"synthetic tampering")
    assert action(c, reviewed, "APPROVE").status_code == 409
    assert repo.erp_write_count(first["claim_id"]) == 0


@pytest.mark.parametrize("mode", ["mock", "timeout", "fact_change"])
def test_unresolved_analysis_or_changed_facts_cannot_be_finance_approved(hybrid, mode):
    env, provider = hybrid
    add_booking(env)
    first = nonhotel(env)
    cat = env[1].state.audit_service.catalog
    if mode == "mock":
        cat.provider = OfflineComplianceProvider()
    if mode == "timeout":
        provider.fail = True
    latest = verify_and_reaudit(env, first, full_days=1, lunches=1, dinners=1)
    if mode == "fact_change":
        changed = env[0].post(
            BASE + f"/claims/{first['claim_id']}/verify",
            json=dict(
                facts={"lunches": 0},
                expected_revision=1,
                source_reference="补充核实来源",
                basis="原午餐核实记录被新的事实更正",
                confirmed=True,
                idempotency_key=str(uuid4()),
            ),
        )
        assert changed.status_code == 200
    assert action(env[0], latest, "APPROVE").status_code == 409
    assert env[2].erp_write_count(first["claim_id"]) == 0


def test_fact_auth_and_append_only_records(hybrid):
    env, _ = hybrid
    add_booking(env)
    claim = nonhotel(env)
    denied = env[0].post(
        BASE + f"/claims/{claim['claim_id']}/verify",
        json=dict(
            facts={"business_related": True},
            expected_revision=0,
            source_reference="员工自己的说明",
            basis="不能自证",
            confirmed=True,
            idempotency_key=str(uuid4()),
        ),
    )
    assert denied.status_code == 403
    store = env[1].state.audit_service.catalog.store
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("DELETE FROM p4b_records")


def test_returned_claim_can_receive_independent_evidence_without_overwriting_application(hybrid):
    env, _ = hybrid
    add_booking(env)
    first = nonhotel(env)
    c, app, repo = env[:3]
    original = repo.submission(first["claim_id"])
    login(c, "finance")
    returned = action(c, first, "RETURN")
    assert returned.status_code == 200, returned.text
    latest = verify_and_reaudit(env, returned.json(), full_days=1, lunches=1, dinners=1)
    assert latest["compliance_analysis"]["compliance_status"] == "CHECK_PASSED"
    assert repo.submission(first["claim_id"]) == original
    assert len(latest["history"]) == 2
    assert action(c, latest, "APPROVE").status_code == 200
    assert repo.erp_write_count(first["claim_id"]) == 1


def test_restart_restores_real_langgraph_checkpoint_without_new_model_call(hybrid):
    from fastapi.testclient import TestClient

    env, provider = hybrid
    c, app, repo = env[:3]
    add_booking(env)
    first = nonhotel(env)
    reviewed = verify_and_reaudit(env, first, full_days=1, lunches=1, dinners=1)
    cid = reviewed["claim_id"]
    calls = len(provider.requests)
    current = app.state.audit_service
    recreated = create_app(
        Settings(
            app_env="test",
            p4a_enabled=True,
            p4b_enabled=True,
            enterprise_enabled=True,
            enterprise_cookie_secure=False,
            _env_file=None,
        ),
        repo,
        policy_store=app.state.policy_app.state.store,
        catalog=PgPolicyCatalog(current.catalog.engine),
        retriever=current.graph_factory.keywords["retriever"],
        compliance_provider=provider,
        business_date_provider=lambda: date(2027, 1, 5),
    )
    with TestClient(recreated) as restarted:
        login(restarted, "finance")
        restored = restarted.get("/api/v1.5/claims/" + cid).json()
        assert restored["compliance_analysis"] == reviewed["compliance_analysis"]
        result = action(restarted, restored, "APPROVE")
        assert result.status_code == 200, result.text
        assert repo.erp_write_count(cid) == 1 and len(provider.requests) == calls


@pytest.mark.parametrize("mode", ["draft", "expired", "wrong_version", "conflict", "tamper"])
def test_pg_authority_before_any_retrieval(hybrid, mode):
    env, _ = hybrid
    engine = env[1].state.audit_service.catalog.engine
    pid = env[5]
    with Session(engine) as s:
        policy = s.get(PolicyVersion, pid)
        uid = policy.authoritative_rules["upload_id"]
        if mode == "draft":
            policy.status = "DRAFT"
        if mode == "conflict":
            values = {
                c.name: getattr(policy, c.name)
                for c in PolicyVersion.__table__.columns
                if c.name not in {"policy_version_id", "created_at"}
            }
            values.update(policy_key="conflicting-scope", version="conflicting-version")
            s.add(PolicyVersion(policy_version_id="CONFLICT", **values))
        if mode == "tamper":
            s.get(PolicyUpload, uid).original += b"modified"
        if mode in {"draft", "tamper"}:
            with pytest.raises(SQLAlchemyError):
                s.commit()
            s.rollback()
            # Existing PostgreSQL guards reject mutation before retrieval sees it.
            if mode == "tamper":
                return
            values = {
                c.name: getattr(policy, c.name)
                for c in PolicyVersion.__table__.columns
                if c.name not in {"policy_version_id", "created_at"}
            }
            values.update(policy_key="draft-scope", version="draft-version", status="DRAFT")
            s.add(PolicyVersion(policy_version_id="DRAFT-P4B", **values))
        s.commit()
    with pytest.raises(ValueError):
        ComplianceRetriever(engine).read(
            company="DEMO-CN",
            city="深圳",
            incurred_on=date(2027, 1, 2) if mode == "expired" else date(2026, 11, 2),
            started=datetime.now(UTC),
            expected_id="DRAFT-P4B"
            if mode == "draft"
            else "WRONG"
            if mode == "wrong_version"
            else pid,
        )
