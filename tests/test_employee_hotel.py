"""Employee declaration and claim-bound finance evidence; no live providers."""

import os
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_p4a_postgres import hotel_portal as hotel_portal
from test_policies_postgres import policy_pg as policy_pg

from domain.employee_hotel import EmployeeHotel
from domain.enterprise import CreateUser
from tests.test_enterprise import PASSWORD, action, login

BASE = "/api/v1.5/p4a/v2"
V1 = "/api/v1.5/p4a/v1"


def declaration(**updates):
    return dict(
        city="深圳",
        address="深圳市南山区合成酒店地址",
        check_in="2026-11-02",
        check_out="2026-11-05",
        amount="1680.00",
        purpose="客户项目会议住宿",
        seller="合成酒店",
        notes="多晚合开发票",
        idempotency_key=str(uuid4()),
        **updates,
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"check_out": "2026-11-02"},
        {"check_out": "2026-11-01"},
        {"check_out": "2027-11-04"},
        {"amount": "0"},
        {"amount": "-1"},
        {"city": " "},
        {"address": " "},
        {"nights": 5},
        {"grade": "A"},
        {"verified": True},
        {"employee_id": "someone-else"},
    ],
)
def test_invalid_declarations_and_client_authority_rejected(updates):
    body = declaration()
    body.update(updates)
    with pytest.raises(ValidationError):
        EmployeeHotel.model_validate(body)


@pytest.fixture
def employee_portal(request):
    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("Disposable PostgreSQL required")
    env = request.getfixturevalue("hotel_portal")
    c, _, _, _, users, _ = env
    assert login(c, "admin").status_code == 200
    common = dict(
        effective_from="2026-01-01",
        effective_to="2027-02-01",
        source="合成 HR／城市标准",
        version="1",
        reason="核实标准",
    )
    for kind, key, value in [
        ("EMPLOYEE", users["employee"]["id"], {"grade": "D"}),
        ("CITY", "city-1", dict(name="深圳", tier="I", level="PREFECTURE", aliases=["深圳市"])),
    ]:
        r = c.post(V1 + "/facts", json=dict(**common, kind=kind, key=key, value=value))
        assert r.status_code == 200, r.text
    assert login(c).status_code == 200
    return env


def submit(env, **updates):
    c = env[0]
    body = declaration()
    body.update(updates)
    r = c.post(BASE + "/claims", json=body)
    assert r.status_code == 202, r.text
    return r.json()["claim_id"], body


def verify(c, cid, revision):
    return dict(
        expected_revision=revision,
        confirmed=True,
        source_reference="SYNTHETIC-ORDER-THREE-NIGHTS",
        basis="独立核对订单及住宿证明，地址及三晚日期一致",
        idempotency_key=str(uuid4()),
    )


def test_employee_submits_without_booking_and_finance_checks_three_night_total(employee_portal):
    c, app, repo, _, users, _ = employee_portal
    cid, body = submit(employee_portal)
    snapshot = c.get(V1 + "/claims/" + cid).json()
    assert snapshot["facts"]["stay"] is None
    assert snapshot["facts"]["employee_stay"]["status"] == "PENDING"
    assert snapshot["facts"]["declaration"]["address"] == body["address"]
    assert snapshot["rule_result"]["decision"] == "UNKNOWN"
    assert snapshot["rule_result"]["nights"] == 3
    assert repo.submission(cid)["amount"] == "1680.00"
    assert repo.submission(cid)["nights"] == 3
    assert not any(
        r["kind"] == "STAY" for r in app.state.audit_service.catalog.facts.list("DEMO-CN")
    )
    assert c.post(BASE + "/claims", json=body).json()["claim_id"] == cid
    detail = c.get("/api/v1.5/claims/" + cid).json()
    assert (
        c.post(
            BASE + f"/claims/{cid}/verify-stay", json=verify(c, cid, detail["revision"])
        ).status_code
        == 403
    )
    assert login(c, "finance").status_code == 200
    assert action(c, detail, "APPROVE").status_code == 409
    proof = verify(c, cid, detail["revision"])
    assert (
        c.post(
            BASE + f"/claims/{cid}/verify-stay", json={**proof, "expected_revision": 999}
        ).status_code
        == 409
    )
    response = c.post(BASE + f"/claims/{cid}/verify-stay", json=proof)
    assert response.status_code == 200, response.text
    assert c.post(BASE + f"/claims/{cid}/verify-stay", json=proof).status_code == 200
    snapshot = c.get(V1 + "/claims/" + cid).json()["facts"]
    assert snapshot["stay"]["actor"] == users["finance"]["id"]
    assert not snapshot["blockers"]
    from sqlalchemy.orm import Session

    from domain.hotel_matrix import HotelFields
    from rules.hotel_matrix import calculate

    catalog = app.state.audit_service.catalog
    fields = HotelFields.model_validate(repo.submission(cid))
    with Session(catalog.engine) as session:
        from datetime import UTC, datetime

        policy = catalog.policy(session, fields, "DEMO-CN", datetime.now(UTC))
    result = calculate(fields, snapshot, policy)
    assert result["decision"] == "PASS"
    assert result["total_limit"] == "2400.00"
    assert result["claimed_amount"] == "1680.00"
    assert repo.erp_write_count(cid) == 0


def test_missing_city_still_submits_but_no_silent_grade_or_city_guess(employee_portal):
    c = employee_portal[0]
    cid, _ = submit(employee_portal, city="未维护县城")
    detail = c.get(V1 + "/claims/" + cid).json()
    assert "CITY_TIER_UNKNOWN" in detail["facts"]["blockers"]
    assert login(c, "finance").status_code == 200
    row = c.get("/api/v1.5/claims/" + cid).json()
    assert (
        c.post(
            BASE + f"/claims/{cid}/verify-stay", json=verify(c, cid, row["revision"])
        ).status_code
        == 409
    )


def test_returned_claim_prefills_from_snapshot_and_address_change_resets_verification(
    employee_portal,
):
    c, _, repo, _, _, _ = employee_portal
    cid, body = submit(employee_portal)
    assert login(c, "finance").status_code == 200
    detail = c.get("/api/v1.5/claims/" + cid).json()
    assert (
        c.post(
            BASE + f"/claims/{cid}/verify-stay", json=verify(c, cid, detail["revision"])
        ).status_code
        == 200
    )
    r = action(c, detail, "RETURN", reason="请更正住宿地址")
    assert r.status_code == 200, r.text
    detail = c.get("/api/v1.5/claims/" + cid).json()
    assert login(c).status_code == 200
    corrected = {
        **body,
        "address": "深圳市福田区更正后的实际地址",
        "expected_revision": detail["revision"],
        "idempotency_key": str(uuid4()),
    }
    r = c.post(BASE + f"/claims/{cid}/resubmit", json=corrected)
    assert r.status_code == 202, r.text
    assert r.json()["claim_id"] == cid
    snapshot = c.get(V1 + "/claims/" + cid).json()["facts"]
    assert snapshot["stay"] is None and snapshot["employee_stay"]["status"] == "PENDING"
    assert snapshot["declaration"]["address"] == corrected["address"]
    assert (
        len(
            repo._db.execute("SELECT * FROM p4a_claim_versions WHERE claim_id=?", (cid,)).fetchall()
        )
        == 2
    )
    assert repo.erp_write_count(cid) == 0


def test_company_isolation_future_dates_and_role(employee_portal):
    c, _, _, store, _, _ = employee_portal
    r = c.post(BASE + "/claims", json={**declaration(), "check_out": "2027-01-06"})
    assert r.status_code == 422
    cid, _ = submit(employee_portal)
    store.create_user(
        "DEMO-B",
        CreateUser(
            username="finance",
            password=PASSWORD,
            name="另一公司财务",
            department="财务部",
            position="财务",
            role="FINANCE",
        ),
    )
    assert login(c, "finance", company="DEMO-B").status_code == 200
    assert c.post(BASE + f"/claims/{cid}/verify-stay", json=verify(c, cid, 0)).status_code == 404
    assert login(c, "finance").status_code == 200
    assert c.post(BASE + "/claims", json=declaration()).status_code == 403


def test_employee_authority_rechecked_at_snapshot_boundary(employee_portal, monkeypatch):
    c, _, repo, store, users, _ = employee_portal
    original = store.live_actor
    before = len(repo.list())

    def revoked(token, *roles, **kwargs):
        if kwargs.get("expected_generation") is not None:
            with store.lock, store.db:
                store.db.execute(
                    "UPDATE enterprise_authorities SET generation=generation+1 WHERE user_id=?",
                    (users["employee"]["id"],),
                )
        return original(token, *roles, **kwargs)

    monkeypatch.setattr(store, "live_actor", revoked)
    r = c.post(BASE + "/claims", json=declaration())
    assert r.status_code == 403, r.text
    assert len(repo.list()) == before


def test_finance_authority_rechecked_before_verification_write(employee_portal, monkeypatch):
    c, _, _, store, users, _ = employee_portal
    cid, _ = submit(employee_portal)
    assert login(c, "finance").status_code == 200
    row = c.get("/api/v1.5/claims/" + cid).json()
    original = store.live_actor

    def revoked(token, *roles, **kwargs):
        if kwargs.get("expected_generation") is not None:
            store.db.execute(
                "UPDATE enterprise_authorities SET generation=generation+1 WHERE user_id=?",
                (users["finance"]["id"],),
            )
        return original(token, *roles, **kwargs)

    monkeypatch.setattr(store, "live_actor", revoked)
    r = c.post(BASE + f"/claims/{cid}/verify-stay", json=verify(c, cid, row["revision"]))
    assert r.status_code == 403, r.text
    assert c.get(V1 + "/claims/" + cid).json()["facts"]["stay"] is None
