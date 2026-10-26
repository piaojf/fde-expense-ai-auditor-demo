"""Real shared PostgreSQL authority, rollback and workflow using the original Word."""

import os
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session
from test_policies_postgres import policy_pg as policy_pg

from apps.api.config import Settings
from apps.api.hotel_adapter import HotelCatalog
from apps.api.hotel_facts import HotelFacts
from apps.api.hotel_publication import HotelPublication
from apps.api.main import create_app
from apps.api.repository import MockClaimRepository
from database.models import PolicyPublicationEvent, PolicyVersion
from domain.enterprise import CreateUser
from domain.hotel_matrix import MatrixConfirm, MatrixLifecycle, MatrixPreview, MatrixPublish
from domain.policies import ConfirmPolicy
from rag.retrieval.pgvector import PgPolicyCatalog, PgVectorPolicyRetriever
from tests.test_enterprise import PASSWORD, login
from tests.test_p4a_matrix import SOURCE, fields

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="Disposable PostgreSQL required"),
]


def prepared(policy_pg, *, confirm=True):
    engine, draft, embedding, url = policy_pg
    row = draft.upload(SOURCE.read_bytes(), "222.docx")
    uid = row["upload_id"]
    meta = {k: v["value"] for k, v in row["parsed"]["extracted_metadata"].items()}
    draft.confirm(
        uid,
        ConfirmPolicy(
            token="synthetic-local-policy-test-capability-00000000", confirmed=True, metadata=meta
        ),
    )
    draft.ingest(uid)
    pub = HotelPublication(
        draft, embedding, app_env="test", clock=lambda: datetime(2026, 10, 11, tzinfo=UTC)
    )
    x = pub.read(uid)
    if not confirm:
        return pub, uid
    pub.confirm_matrix(
        uid,
        MatrixConfirm(
            expected_revision=0,
            confirmed=True,
            reason="逐格原文核对",
            extraction_digest=x["extraction_digest"],
        ),
        "admin",
    )
    return pub, uid


def publish(pub, uid):
    b = MatrixPreview(predecessor_ids=["POL-SZ-2026", "POL-BEIJING-2026"])
    v = pub.preview(uid, b)
    assert not v["blockers"], v
    body = MatrixPublish(
        predecessor_ids=b.predecessor_ids,
        preview_digest=v["preview_digest"],
        confirmed=True,
        idempotency_key=str(uuid4()),
        reason="合成发布",
    )
    return pub.publish_matrix(uid, body, "admin"), body


def test_atomic_release_replay_and_historical_dates(policy_pg, tmp_path):
    pub, uid = prepared(policy_pg)
    result, body = publish(pub, uid)
    assert pub.publish_matrix(uid, body, "admin") == result
    assert len(result["policy"]["rules"]["matrix"]["rules"]) == 16
    engine = policy_pg[0]
    cat = HotelCatalog(
        PgPolicyCatalog(engine),
        HotelFacts(MockClaimRepository(store_path=str(tmp_path / "facts.sqlite3"))),
        engine,
    )
    with Session(engine) as s:
        old = cat.policy(s, fields(on="2026-10-20"), "DEMO-CN", datetime.now(UTC))
        new = cat.policy(s, fields(), "DEMO-CN", datetime.now(UTC))
        assert old.policy_version_id == "POL-SZ-2026"
        assert new.policy_version_id == result["policy"]["policy_version_id"]
        assert cat.policy(s, fields(on="2027-01-02"), "DEMO-CN", datetime.now(UTC)) is None
        assert s.get(PolicyVersion, "POL-SZ-2026").effective_to == date(2027, 1, 1)


def test_publication_failure_rolls_back_everything(policy_pg):
    pub, uid = prepared(policy_pg)
    engine = policy_pg[0]
    v = pub.preview(uid, MatrixPreview(predecessor_ids=["POL-SZ-2026", "POL-BEIJING-2026"]))

    def fail_event(conn, cursor, sql, parameters, context, many):
        if sql.startswith("INSERT INTO policy_publication_events"):
            raise RuntimeError("synthetic publication failure")

    event.listen(engine, "before_cursor_execute", fail_event)
    try:
        with pytest.raises(RuntimeError):
            pub.publish_matrix(
                uid,
                MatrixPublish(
                    predecessor_ids=["POL-SZ-2026", "POL-BEIJING-2026"],
                    preview_digest=v["preview_digest"],
                    confirmed=True,
                    idempotency_key=str(uuid4()),
                    reason="合成故障",
                ),
                "admin",
            )
    finally:
        event.remove(engine, "before_cursor_execute", fail_event)
    with Session(engine) as s:
        assert not s.scalars(
            select(PolicyVersion).where(
                PolicyVersion.city == "*", PolicyVersion.authority_level == 100
            )
        ).all()
        assert not s.scalars(select(PolicyPublicationEvent)).all()


def test_unconfirmed_draft_and_missing_predecessor_blocked(policy_pg):
    pub, uid = prepared(policy_pg)
    assert pub.preview(uid, MatrixPreview())["blockers"] == ["PREDECESSOR_REQUIRED"]
    with Session(policy_pg[0]) as s:
        assert not s.scalars(select(PolicyPublicationEvent)).all()


def test_rollback_retains_original_and_changes_only_future(policy_pg):
    pub, uid = prepared(policy_pg)
    result, _ = publish(pub, uid)
    pid = result["policy"]["policy_version_id"]
    b = SimpleNamespace(kind="ROLLBACK", effective_on=date(2026, 12, 1), restore_id="POL-SZ-2026")
    v = pub.matrix_lifecycle_preview(pid, b)
    body = MatrixLifecycle(
        kind=b.kind,
        effective_on=b.effective_on,
        restore_id=b.restore_id,
        preview_digest=v["preview_digest"],
        confirmed=True,
        idempotency_key=str(uuid4()),
        reason="合成回退",
    )
    r = pub.matrix_lifecycle(pid, body, "admin")
    assert pub.matrix_lifecycle(pid, body, "admin") == r
    with Session(policy_pg[0]) as s:
        assert s.get(PolicyVersion, pid).authoritative_rules["rule_type"] == "HOTEL_LIMIT_MATRIX"
        assert (
            s.get(PolicyVersion, r["policy"]["policy_version_id"]).authoritative_rules[
                "hotel_limit_per_night"
            ]
            == "700.00"
        )


def test_real_workflow_trusted_master_idempotency_and_hard_block(policy_pg, tmp_path):
    pub, uid = prepared(policy_pg)
    publish(pub, uid)
    engine, draft, embedding, url = policy_pg
    repo = MockClaimRepository(store_path=str(tmp_path / "portal.sqlite3"))
    app = create_app(
        Settings(
            app_env="test",
            p4a_enabled=True,
            enterprise_enabled=True,
            enterprise_cookie_secure=False,
            _env_file=None,
        ),
        repo,
        policy_store=draft,
        catalog=PgPolicyCatalog(engine),
        retriever=PgVectorPolicyRetriever(engine, embedding),
        business_date_provider=lambda: date(2026, 11, 4),
    )
    store = app.state.enterprise_app.state.store
    people = {}
    for username, role in [("employee", "EMPLOYEE"), ("finance", "FINANCE"), ("admin", "ADMIN")]:
        people[username] = store.create_user(
            "DEMO-CN",
            CreateUser(
                username=username,
                password=PASSWORD,
                name="张三",
                department="研发部",
                position="工程师",
                role=role,
            ),
        )
    base = "/api/v1.5/p4a/v1"
    with TestClient(app) as c:
        assert login(c, "admin").status_code == 200
        common = dict(
            effective_from="2026-01-01",
            effective_to="2027-01-01",
            source="合成 HR 与酒店订单证据",
            version="synthetic-v1",
            reason="原文及订单核对",
        )
        for kind, key, value in [
            ("EMPLOYEE", people["employee"]["id"], {"grade": "D"}),
            (
                "CITY",
                "SZ",
                {"name": "深圳", "tier": "I", "level": "PREFECTURE", "aliases": ["深圳市"]},
            ),
            (
                "STAY",
                "booking-001",
                dict(
                    employee_id=people["employee"]["id"],
                    city_key="SZ",
                    address="深圳合成酒店实际地址",
                    check_in="2026-11-02",
                    check_out="2026-11-03",
                    evidence_reference="synthetic-booking-001",
                ),
            ),
        ]:
            r = c.post(base + "/facts", json=dict(**common, kind=kind, key=key, value=value))
            assert r.status_code == 200, r.text
        assert login(c).status_code == 200
        denied = c.post(
            base + "/facts",
            json=dict(
                **common, kind="EMPLOYEE", key=people["employee"]["id"], value={"grade": "A"}
            ),
        )
        assert denied.status_code == 403
        body = dict(
            fields=fields(amount="850").model_dump(mode="json"),
            stay_id="booking-001",
            idempotency_key=str(uuid4()),
        )
        r = c.post(base + "/claims", json=body)
        assert r.status_code == 202, r.text
        cid = r.json()["claim_id"]
        report = c.get(base + "/claims/" + cid).json()
        assert report["rule_result"]["decision"] == "FAIL", report
        assert report["rule_result"]["limit_per_night"] == "800.00"
        assert c.post(base + "/claims", json=body).json()["claim_id"] == cid
        assert repo.erp_write_count(cid) == 0
        assert login(c, "finance").status_code == 200
        detail = c.get("/api/v1.5/claims/" + cid).json()
        action = c.post(
            "/api/v1.5/claims/" + cid + "/finance-action",
            json=dict(
                action="APPROVE",
                reason="合成禁止强批",
                expected_revision=detail["revision"],
                idempotency_key=str(uuid4()),
            ),
        )
        assert action.status_code == 409, action.text
        assert repo.erp_write_count(cid) == 0
    repo.close()


def test_concurrent_publish_and_stale_preview_cannot_split_matrix(policy_pg):
    from concurrent.futures import ThreadPoolExecutor

    pub, uid = prepared(policy_pg)
    view = pub.preview(uid, MatrixPreview(predecessor_ids=["POL-SZ-2026", "POL-BEIJING-2026"]))
    body = MatrixPublish(
        predecessor_ids=["POL-SZ-2026", "POL-BEIJING-2026"],
        preview_digest=view["preview_digest"],
        confirmed=True,
        idempotency_key=str(uuid4()),
        reason="并发",
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: pub.publish_matrix(uid, body, "admin"), range(2)))
    assert results[0] == results[1]
    with Session(policy_pg[0]) as s:
        matrix = s.scalars(
            select(PolicyVersion).where(
                PolicyVersion.city == "*", PolicyVersion.authority_level == 100
            )
        ).all()
        assert len(matrix) == 1 and len(matrix[0].authoritative_rules["cells"]) == 16
        events = s.scalars(select(PolicyPublicationEvent)).all()
        assert len(events) == 2 and {e.predecessor_id for e in events} == {
            "POL-SZ-2026",
            "POL-BEIJING-2026",
        }


def test_future_activation_and_pinned_audit_during_governance_change(policy_pg, tmp_path):
    pub, uid = prepared(policy_pg)
    result, _ = publish(pub, uid)
    pid = result["policy"]["policy_version_id"]
    cat = HotelCatalog(
        PgPolicyCatalog(policy_pg[0]),
        HotelFacts(MockClaimRepository(store_path=str(tmp_path / "facts.sqlite3"))),
        policy_pg[0],
    )
    started = datetime(2026, 10, 11, 1, tzinfo=UTC)
    with Session(policy_pg[0]) as s:
        assert (
            cat.policy(s, fields(on="2026-10-31"), "DEMO-CN", started).policy_version_id
            == "POL-SZ-2026"
        )
        assert cat.policy(s, fields(), "DEMO-CN", started).policy_version_id == pid
    # A later retirement must not alter an audit that already pinned the previous visibility time.
    pub.clock = lambda: datetime(2026, 10, 12, tzinfo=UTC)
    b = SimpleNamespace(kind="RETIRE", effective_on=date(2026, 11, 15), restore_id=None)
    v = pub.matrix_lifecycle_preview(pid, b)
    body = MatrixLifecycle(
        kind="RETIRE",
        effective_on=b.effective_on,
        preview_digest=v["preview_digest"],
        confirmed=True,
        idempotency_key=str(uuid4()),
        reason="合成退休",
    )
    pub.matrix_lifecycle(pid, body, "admin")
    with Session(policy_pg[0]) as s:
        assert cat.policy(s, fields(on="2026-11-20"), "DEMO-CN", started).policy_version_id == pid
        assert (
            cat.policy(s, fields(on="2026-11-20"), "DEMO-CN", datetime(2026, 10, 13, tzinfo=UTC))
            is None
        )


def test_trusted_fact_append_only_overlap_and_historical_snapshot(tmp_path):
    import sqlite3

    from apps.api.errors import APIError
    from domain.hotel_matrix import TrustedFact

    repo = MockClaimRepository(store_path=str(tmp_path / "facts.sqlite3"))
    facts = HotelFacts(repo)
    body = TrustedFact(
        kind="EMPLOYEE",
        key="synthetic-employee",
        effective_from="2026-01-01",
        effective_to="2026-11-01",
        value={"grade": "D"},
        source="合成 HR",
        version="1",
        reason="核对",
    )
    facts.add("DEMO-CN", "admin", body)
    with pytest.raises(APIError):
        facts.add("DEMO-CN", "admin", body)
    facts.add(
        "DEMO-CN",
        "admin",
        body.model_copy(
            update={
                "effective_from": date(2026, 11, 1),
                "effective_to": date(2027, 1, 1),
                "value": {"grade": "B"},
                "version": "2",
            }
        ),
    )
    assert (
        facts.resolve("DEMO-CN", "EMPLOYEE", body.key, date(2026, 10, 20))["value"]["grade"] == "D"
    )
    assert (
        facts.resolve("DEMO-CN", "EMPLOYEE", body.key, date(2026, 11, 2))["value"]["grade"] == "B"
    )
    assert facts.resolve("OTHER", "EMPLOYEE", body.key, date(2026, 11, 2)) is None
    with pytest.raises(sqlite3.IntegrityError):
        facts.db.execute("DELETE FROM p4a_facts")
    repo.close()


@pytest.fixture
def hotel_portal(policy_pg, tmp_path):
    pub, uid = prepared(policy_pg)
    published, _ = publish(pub, uid)
    engine, draft, embedding, _ = policy_pg
    repo = MockClaimRepository(store_path=str(tmp_path / "portal.sqlite3"))
    app = create_app(
        Settings(
            app_env="test",
            p4a_enabled=True,
            enterprise_enabled=True,
            enterprise_cookie_secure=False,
            _env_file=None,
        ),
        repo,
        policy_store=draft,
        catalog=PgPolicyCatalog(engine),
        retriever=PgVectorPolicyRetriever(engine, embedding),
        business_date_provider=lambda: date(2027, 1, 5),
    )
    store = app.state.enterprise_app.state.store
    users = {}
    for username, role in [("employee", "EMPLOYEE"), ("finance", "FINANCE"), ("admin", "ADMIN")]:
        users[username] = store.create_user(
            "DEMO-CN",
            CreateUser(
                username=username,
                password=PASSWORD,
                name="张三",
                department="研发部",
                position="工程师",
                role=role,
            ),
        )
    with TestClient(app) as client:
        yield client, app, repo, store, users, published["policy"]["policy_version_id"]
    repo.close()


def add_booking(
    portal, *, grade="D", city="深圳", tier="I", level="PREFECTURE", on="2026-11-02", suffix="1"
):
    c, _, _, _, users, _ = portal
    from datetime import timedelta

    base = "/api/v1.5/p4a/v1"
    assert login(c, "admin").status_code == 200
    common = dict(
        effective_from="2026-01-01",
        effective_to="2027-02-01",
        source="合成可信主数据",
        version="1",
        reason="原文核对",
    )
    values = [
        ("CITY", "city-" + suffix, dict(name=city, tier=tier, level=level)),
        (
            "STAY",
            "stay-" + suffix,
            dict(
                employee_id=users["employee"]["id"],
                city_key="city-" + suffix,
                address=city + "合成订单实际地址",
                check_in=on,
                check_out=str(date.fromisoformat(on) + timedelta(days=1)),
                evidence_reference="合成订单-" + suffix,
            ),
        ),
    ]
    # One employee grade per fixture; intentionally omit it to exercise UNKNOWN.
    if grade:
        values.insert(0, ("EMPLOYEE", users["employee"]["id"], {"grade": grade}))
    for kind, key, value in values:
        result = c.post(base + "/facts", json=dict(**common, kind=kind, key=key, value=value))
        assert result.status_code == 200, result.text
    assert login(c).status_code == 200
    return "stay-" + suffix


def submit_hotel(portal, stay, **updates):
    c = portal[0]
    body = dict(
        fields=fields(**updates).model_dump(mode="json"), stay_id=stay, idempotency_key=str(uuid4())
    )
    response = c.post("/api/v1.5/p4a/v1/claims", json=body)
    assert response.status_code == 202, response.text
    cid = response.json()["claim_id"]
    return cid, c.get("/api/v1.5/p4a/v1/claims/" + cid).json(), body


@pytest.mark.parametrize(
    "on,city,grade,tier,level,amount,limit,decision",
    [
        ("2026-10-20", "深圳", "D", "I", "PREFECTURE", "780", "700.00", "FAIL"),
        ("2026-11-02", "深圳", "D", "I", "PREFECTURE", "780", "800.00", "PASS"),
        ("2026-11-02", "深圳", "D", "I", "PREFECTURE", "850", "800.00", "FAIL"),
        ("2026-11-02", "深圳", "B", "I", "PREFECTURE", "980", "1000.00", "PASS"),
        ("2026-11-02", "杭州", "D", "II", "PREFECTURE", "680", "650.00", "FAIL"),
        ("2026-11-02", "青田县", "D", "IV", "COUNTY", "400", "420.00", "PASS"),
    ],
)
def test_t01_through_t06_real_pipeline_and_pinned_rag(
    hotel_portal, on, city, grade, tier, level, amount, limit, decision
):
    c, _, repo, _, _, pid = hotel_portal
    stay = add_booking(hotel_portal, grade=grade, city=city, tier=tier, level=level, on=on)
    cid, report, _ = submit_hotel(hotel_portal, stay, city=city, amount=amount, on=on)
    rule = report["rule_result"]
    assert rule["decision"] == decision and rule["limit_per_night"] == limit
    assert repo.erp_write_count(cid) == 0  # No invoice / human authority supplied.
    detail = c.get("/api/v1.5/claims/" + cid).json()
    assert detail["stage"] == "FINANCE_REVIEW"
    evidence = detail["ai_result"]["policy_evidence"]
    expected_pid = "POL-SZ-2026" if on < "2026-11-01" else pid
    assert evidence and all(e["policy_version_id"] == expected_pid for e in evidence), detail
    if on >= "2026-11-01":
        assert "\n".join(rule["source_reference"]["original_row"]) in evidence[0]["quote"]


def test_return_and_resubmit_preserves_original_facts_results_and_identity(hotel_portal):
    from tests.test_enterprise import action

    c, app, repo, store, _, _ = hotel_portal
    stay = add_booking(hotel_portal)
    cid, original, _ = submit_hotel(hotel_portal, stay, amount="850")
    old = c.get("/api/v1.5/claims/" + cid).json()
    assert old["p4a"]
    assert login(c, "finance").status_code == 200
    returned = action(c, old, "RETURN", reason="按原住宿金额核对").json()
    assert returned["stage"] == "RETURNED"
    assert login(c).status_code == 200
    body = dict(
        fields=fields(amount="780").model_dump(mode="json"),
        stay_id=stay,
        expected_revision=returned["revision"],
        idempotency_key=str(uuid4()),
    )
    result = c.post("/api/v1.5/p4a/v1/claims/" + cid + "/resubmit", json=body)
    assert result.status_code == 202, result.text
    fresh = c.get("/api/v1.5/claims/" + cid).json()
    assert (
        fresh["version"] == 2 and fresh["claim"]["active_run_id"] != old["claim"]["active_run_id"]
    )
    assert fresh["history"][0]["fields"]["amount"] == "850.00"
    assert fresh["history"][1]["fields"]["amount"] == "780.00"
    report = c.get("/api/v1.5/p4a/v1/claims/" + cid).json()
    assert report["rule_result"]["decision"] == "PASS"
    assert {r["decision"] for r in report["reports"]} == {"PASS", "FAIL"}
    assert c.post("/api/v1.5/p4a/v1/claims/" + cid + "/resubmit", json=body).status_code == 202
    assert (
        store.db.execute(
            "SELECT count(*) FROM p4a_claim_versions WHERE claim_id=?", (cid,)
        ).fetchone()[0]
        == 2
    )
    assert repo.run_context(cid, old["claim"]["active_run_id"])["superseded"]
    assert repo.erp_write_count(cid) == 0


@pytest.mark.parametrize(
    "grade,city,tier,level,on,reason",
    [
        (None, "深圳", "I", "PREFECTURE", "2026-11-02", "EMPLOYEE_GRADE_UNKNOWN"),
        ("D", "深圳", "IV", "COUNTY", "2026-11-02", "CITY_CLASSIFICATION_CONFLICT"),
        ("D", "深圳", "I", "PREFECTURE", "2027-01-02", "POLICY_UNKNOWN_OR_CONFLICT"),
    ],
)
def test_unknown_facts_fraudulent_tier_and_expiry_fail_closed(
    hotel_portal, grade, city, tier, level, on, reason
):
    stay = add_booking(hotel_portal, grade=grade, city=city, tier=tier, level=level, on=on)
    cid, report, _ = submit_hotel(hotel_portal, stay, city=city, on=on)
    assert report["rule_result"]["decision"] == "UNKNOWN"
    assert reason in report["rule_result"]["reason"]
    assert hotel_portal[2].erp_write_count(cid) == 0


def test_rag_version_conflict_records_conflict_and_never_approves(hotel_portal, monkeypatch):
    from tests.test_enterprise import action

    original = PgVectorPolicyRetriever.retrieve

    async def conflicting(self, *args, **kwargs):
        evidence = await original(self, *args, **kwargs)
        return [
            e.model_copy(update={"policy_version_id": "forged-other-version"}) for e in evidence
        ]

    monkeypatch.setattr(PgVectorPolicyRetriever, "retrieve", conflicting)
    stay = add_booking(hotel_portal)
    cid, report, _ = submit_hotel(hotel_portal, stay)
    assert report["rule_result"]["decision"] == "PASS"
    c, _, repo, _, _, _ = hotel_portal
    detail = c.get("/api/v1.5/claims/" + cid).json()
    assert not detail["ai_result"]["policy_evidence"]
    assert any(
        e.event_type == "POLICY_RETRIEVED"
        and e.payload.get("retrieval_outcome") == "RULE_RAG_VERSION_CONFLICT"
        for e in repo.events(cid)
    )
    login(c, "finance")
    assert action(c, detail, "APPROVE").status_code == 409
    assert repo.erp_write_count(cid) == 0


def test_policy_database_failure_cannot_approve(hotel_portal, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from tests.test_enterprise import action

    def unavailable(*args, **kwargs):
        raise OperationalError("synthetic database outage", {}, RuntimeError("synthetic"))

    stay = add_booking(hotel_portal)
    monkeypatch.setattr(HotelCatalog, "policy", unavailable)
    cid, report, _ = submit_hotel(hotel_portal, stay)
    assert report["rule_result"] is None
    c, _, repo, _, _, _ = hotel_portal
    detail = c.get("/api/v1.5/claims/" + cid).json()
    assert any(e.event_type == "RULE_CHECK_FAILED" for e in repo.events(cid))
    login(c, "finance")
    assert action(c, detail, "APPROVE").status_code == 409
    assert repo.erp_write_count(cid) == 0


def test_real_invoice_finance_approval_and_erp_replay(hotel_portal):
    from pathlib import Path

    from tests.test_enterprise import action
    from tests.test_invoices import FIELDS

    c, _, repo, _, _, _ = hotel_portal
    stay = add_booking(hotel_portal)
    did = str(uuid4())
    base = "/api/v1.5/invoices/drafts/" + did
    assert c.post("/api/v1.5/invoices/drafts", json={"draft_id": did}).status_code == 200
    result = c.post(
        base + "/attachments",
        files={
            "file": (
                "synthetic.pdf",
                Path("tests/fixtures/invoices/text-invoice.pdf").read_bytes(),
                "application/pdf",
            )
        },
    )
    aid = result.json()["attachment_id"]
    assert c.post(base + "/attachments/" + aid + "/ocr", json={}).status_code == 200
    body = dict(
        fields=fields(amount="600").model_dump(mode="json"),
        stay_id=stay,
        idempotency_key=str(uuid4()),
        invoice=dict(
            draft_id=did,
            attachment_id=aid,
            confirmed=True,
            confirmed_fields=FIELDS,
            discrepancy_reason="合成测试原票日期差异已人工核对",
        ),
    )
    result = c.post("/api/v1.5/p4a/v1/claims", json=body)
    assert result.status_code == 202, result.text
    cid = result.json()["claim_id"]
    detail = c.get("/api/v1.5/claims/" + cid).json()
    assert repo.erp_write_count(cid) == 0
    assert detail["invoice_evidence"]["attachment"]["ocr"]["provider"] != "mock-test"
    login(c, "finance")
    key = str(uuid4())
    approved = action(c, detail, "APPROVE", key=key)
    assert approved.status_code == 200 and approved.json()["stage"] == "APPROVED", approved.text
    assert action(c, detail, "APPROVE", key=key).status_code == 200
    assert repo.erp_write_count(cid) == 1


def test_unconfirmed_matrix_and_stale_whole_matrix_preview(policy_pg):
    from apps.api.errors import APIError

    pub, uid = prepared(policy_pg, confirm=False)
    body = MatrixPreview(predecessor_ids=["POL-SZ-2026", "POL-BEIJING-2026"])
    assert pub.preview(uid, body)["blockers"] == ["MATRIX_CONFIRM_REQUIRED"]
    x = pub.read(uid)
    confirm = MatrixConfirm(
        expected_revision=0,
        extraction_digest=x["extraction_digest"],
        confirmed=True,
        reason="第一次整体核对",
    )
    pub.confirm_matrix(uid, confirm, "admin")
    preview = pub.preview(uid, body)
    pub.confirm_matrix(
        uid, confirm.model_copy(update={"expected_revision": 1, "reason": "第二次核对"}), "admin"
    )
    with pytest.raises(APIError, match="预览已变化"):
        pub.publish_matrix(
            uid,
            MatrixPublish(
                **body.model_dump(),
                preview_digest=preview["preview_digest"],
                confirmed=True,
                idempotency_key=str(uuid4()),
                reason="陈旧预览",
            ),
            "admin",
        )
    with Session(policy_pg[0]) as s:
        assert not s.scalars(select(PolicyPublicationEvent)).all()


def test_conflicting_official_policy_is_never_arbitrarily_selected(policy_pg, tmp_path):
    pub, uid = prepared(policy_pg)
    publish(pub, uid)
    with Session(policy_pg[0]) as s, s.begin():
        old = s.get(PolicyVersion, "POL-SZ-2026")
        pub.copy_version(
            s,
            old,
            pub.chunks(s, old),
            pid="synthetic-conflicting-official",
            version="synthetic-conflict",
            start=date(2026, 11, 1),
            end=date(2027, 1, 1),
            rules=old.authoritative_rules,
            timestamp=datetime.now(UTC),
        )
    repo = MockClaimRepository(store_path=str(tmp_path / "conflict.sqlite3"))
    cat = HotelCatalog(PgPolicyCatalog(policy_pg[0]), HotelFacts(repo), policy_pg[0])
    with Session(policy_pg[0]) as s:
        assert cat.policy(s, fields(), "DEMO-CN", datetime.now(UTC)) is None
    repo.close()


def test_api_unknown_city_and_grade_forgery_have_no_approval(hotel_portal):
    c, _, repo, _, users, _ = hotel_portal
    stay = add_booking(hotel_portal)
    body = dict(fields=fields().model_dump(mode="json"), stay_id=stay, idempotency_key=str(uuid4()))
    body["fields"]["grade"] = "A"
    assert c.post("/api/v1.5/p4a/v1/claims", json=body).status_code == 422
    login(c, "admin")
    response = c.post(
        "/api/v1.5/p4a/v1/facts",
        json=dict(
            kind="STAY",
            key="unmapped-city-booking",
            effective_from="2026-01-01",
            effective_to="2027-02-01",
            value=dict(
                employee_id=users["employee"]["id"],
                city_key="not-in-controlled-master",
                address="合成住宿地址待核实行政区划",
                check_in="2026-11-02",
                check_out="2026-11-03",
                evidence_reference="synthetic-unknown-city-order",
            ),
            source="合成订单，缺少城市主数据",
            version="1",
            reason="未知城市必须等待核实",
        ),
    )
    assert response.status_code == 200, response.text
    login(c)
    cid, report, _ = submit_hotel(hotel_portal, "unmapped-city-booking")
    assert report["rule_result"]["decision"] == "UNKNOWN"
    assert "CITY_TIER_UNKNOWN" in report["rule_result"]["reason"]
    assert repo.erp_write_count(cid) == 0
