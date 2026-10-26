"""Combined P4A authority and existing travel graph, finance and immutable snapshots."""

import json
import os
import sqlite3
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from test_p4a_postgres import add_booking, submit_hotel
from test_p4a_postgres import hotel_portal as hotel_portal
from test_policies_postgres import policy_pg as policy_pg

from domain.enterprise_travel import PublishTravelPolicy
from scripts.enterprise_travel_migration import install
from tests.test_enterprise import action, fields, login
from tests.test_enterprise_travel import SOURCE, rule

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="Disposable PostgreSQL required"),
]


@pytest.fixture
def integrated(hotel_portal, monkeypatch):
    c, app, repo, store, users, pid = hotel_portal
    install(store.db)  # Only this fixture's brand-new tmp_path SQLite.
    travel = app.state.enterprise_app.state.travel
    lines, start = [], 0
    for number, text in enumerate(SOURCE.splitlines(), 1):
        lines.append(dict(text=text, start=start, end=start + len(text), line=number))
        start += len(text) + 1
    source = dict(
        upload_id="synthetic-travel-source", sha256="0" * 64,
        parsed=dict(text=SOURCE, lines=lines),
        metadata=dict(title="合成非住宿标准", legal_entity="DEMO-CN", region="深圳",
                      expense_type="ALL", version="travel-test", effective_from="2026-09-01",
                      effective_to="2027-01-01"),
    )
    monkeypatch.setattr(travel, "source", lambda uid, actor: source)
    monkeypatch.setattr("apps.api.enterprise_travel.today", lambda: date(2026, 8, 1))
    login(c, "admin")
    actor = c.get("/api/v1.5/auth/me").json()["user"]
    # Deliberately disagreeing legacy HR data must never upgrade a new P4A snapshot.
    with store.lock, store.db:
        store.event(actor, "TRAVEL_PROFILE_SET", user_id=users["employee"]["id"],
                    grade="A", effective_on="2026-01-01", reason="历史档案，仅兼容历史")
    return hotel_portal


def fact(env, kind, key, value, **updates):
    c = env[0]
    login(c, "admin")
    body = dict(kind=kind, key=key, value=value, effective_from="2026-01-01",
                effective_to="2027-02-01", source="合成 HR/城市档案", version="test-1",
                reason="明确原始依据")
    body.update(updates)
    result = c.post("/api/v1.5/p4a/v1/facts", json=body)
    assert result.status_code == 200, result.text
    return result.json()


def master(env, *, grade=True, city=True, grade_end="2027-02-01"):
    if grade:
        fact(env, "EMPLOYEE", env[4]["employee"]["id"], {"grade": "D"},
             effective_to=grade_end)
    if city:
        fact(env, "CITY", "sz", dict(name="深圳市", tier="I", level="PREFECTURE",
                                    aliases=["深圳"]))


def publish_nonhotel(env, kind, **updates):
    c, app, _, _, _, _ = env
    login(c, "admin")
    actor = c.get("/api/v1.5/auth/me").json()["user"]
    travel = app.state.enterprise_app.state.travel
    standard = rule(kind, departments=["*"], positions=["*"], **updates)
    body = PublishTravelPolicy(rules=[standard], expected_revision=0, confirmed=True,
                               reason="合成原文核对", idempotency_key=str(uuid4()))
    return travel.publish("synthetic-travel-source", actor, body)


def submit_nonhotel(env, kind="TRANSPORT", amount="900.00", **details):
    c = env[0]
    login(c)
    declaration = fields(applicant_name="张三", department="研发部", position="工程师",
                         category="OTHER" if kind == "ALLOWANCE" else kind,
                         nights=None, amount=amount)
    body = dict(fields=declaration, travel_details=dict(expense_type=kind, **details),
                idempotency_key=str(uuid4()))
    result = c.post("/api/v1.5/claims", json=body)
    assert result.status_code == 202, result.text
    cid = result.json()["claim_id"]
    return c.get("/api/v1.5/claims/" + cid).json(), body


@pytest.mark.parametrize("kind,amount,details,expected", [
    ("TRANSPORT", "900.00", dict(transport_mode="高铁", seat="二等座"), "PASS"),
    ("TRANSPORT", "900.00", dict(transport_mode="高铁", seat="一等座"), "FAIL"),
    ("TRANSPORT", "1001.00", dict(transport_mode="高铁", seat="二等座"), "FAIL"),
    ("ALLOWANCE", "36.00", dict(allowance_days=1, lunches=1, dinners=1), "PASS"),
    ("ALLOWANCE", "37.00", dict(allowance_days=1, lunches=1, dinners=1), "FAIL"),
])
def test_combined_calculation_finance_and_erp(integrated, kind, amount, details, expected):
    master(integrated)
    rates = dict(lunch_rate="0.40", dinner_rate="0.40") if kind == "ALLOWANCE" else {}
    standard = publish_nonhotel(integrated, kind, **rates)
    claim, body = submit_nonhotel(integrated, kind, amount, **details)
    checked = claim["ai_result"]["rule_results"][0]
    assert claim["stage"] == "FINANCE_REVIEW" and checked["outcome"] == expected
    assert checked["policy_version_id"] == standard["policy_id"]
    snapshot = claim["employee_snapshot"]
    assert snapshot["authority"] == "p4a.1" and snapshot["grade"] == "D"
    assert snapshot["city_fact"]["value"]["tier"] == "I"
    assert not snapshot["p4a_blockers"]
    c, _, repo, _, _, _ = integrated
    assert repo.erp_write_count(claim["claim_id"]) == 0
    assert c.post("/api/v1.5/claims", json=body).json()["claim_id"] == claim["claim_id"]
    login(c, "finance")
    key = str(uuid4())
    result = action(c, claim, "APPROVE", key=key)
    assert result.status_code == (200 if expected == "PASS" else 409), result.text
    if expected == "PASS":
        assert action(c, claim, "APPROVE", key=key).status_code == 200
    assert repo.erp_write_count(claim["claim_id"]) == (1 if expected == "PASS" else 0)


@pytest.mark.parametrize("missing", ["grade", "city", "grade_expired", "ambiguous_city"])
def test_missing_trusted_facts_cannot_pass_wildcard_standard(integrated, missing):
    master(integrated, grade=missing != "grade", city=missing != "city",
           grade_end="2026-09-21" if missing == "grade_expired" else "2027-02-01")
    if missing == "ambiguous_city":
        fact(integrated, "CITY", "another-city",
             dict(name="另一受控城市", tier="III", level="PREFECTURE", aliases=["深圳"]))
    publish_nonhotel(integrated, "TRANSPORT", cities=["*"], grades=["*"])
    claim, _ = submit_nonhotel(integrated, transport_mode="高铁", seat="二等座")
    assert claim["ai_result"]["rule_results"][0]["outcome"] == "UNKNOWN"
    assert claim["employee_snapshot"]["p4a_blockers"]
    login(integrated[0], "finance")
    assert action(integrated[0], claim, "APPROVE").status_code == 409
    assert integrated[2].erp_write_count(claim["claim_id"]) == 0


def test_snapshot_return_resubmit_and_database_guards(integrated):
    master(integrated)
    publish_nonhotel(integrated, "TRANSPORT")
    claim, body = submit_nonhotel(integrated, amount="1001.00",
                                  transport_mode="高铁", seat="二等座")
    snapshot = claim["employee_snapshot"]
    store, cid = integrated[3], claim["claim_id"]
    raw = store.db.execute("SELECT body FROM enterprise_versions WHERE claim_id=?", (cid,))
    damaged = json.loads(raw.fetchone()[0])
    damaged["employee_snapshot"]["grade"] = "A"
    with pytest.raises(sqlite3.IntegrityError), store.db:
        store.db.execute("UPDATE enterprise_versions SET body=? WHERE claim_id=?",
                         (json.dumps(damaged), cid))
    login(integrated[0], "finance")
    returned = action(integrated[0], claim, "RETURN").json()
    login(integrated[0])
    body = {**body, "fields": {**body["fields"], "amount": "900.00"},
            "expected_revision": returned["revision"], "idempotency_key": str(uuid4())}
    result = integrated[0].post(f"/api/v1.5/claims/{cid}/resubmit", json=body)
    assert result.status_code == 202, result.text
    latest = integrated[0].get(f"/api/v1.5/claims/{cid}").json()
    assert latest["history"][0]["employee_snapshot"] == snapshot
    assert latest["history"][0]["ai_result"]["rule_results"][0]["outcome"] == "FAIL"
    assert latest["ai_result"]["rule_results"][0]["outcome"] == "PASS"
    assert latest["version"] == 2 and latest["claim_id"] == cid


def test_p4a_hotel_remains_single_authority_and_requires_invoice(integrated):
    master(integrated)
    publish_nonhotel(integrated, "TRANSPORT")
    stay = add_booking(integrated, grade=None, suffix="booking")
    cid, result, _ = submit_hotel(integrated, stay, amount="780.00")
    assert result["rule_result"]["decision"] == "PASS"
    assert result["rule_result"]["policy_version_id"] == integrated[5]
    c = integrated[0]
    detail = c.get("/api/v1.5/claims/" + cid).json()
    assert detail["p4a"]
    login(c, "finance")
    assert action(c, detail, "APPROVE").status_code == 409
    assert integrated[2].erp_write_count(cid) == 0


@pytest.mark.parametrize("with_details", [False, True])
def test_old_hotel_submission_cannot_bypass_p4a(integrated, with_details):
    c = integrated[0]
    login(c)
    body = dict(fields=fields(applicant_name="张三", department="研发部", position="工程师"),
                idempotency_key=str(uuid4()))
    if with_details:
        body["travel_details"] = dict(expense_type="HOTEL")
    result = c.post("/api/v1.5/claims", json=body)
    assert result.status_code == 409 and result.json()["error"]["code"] == "P4A_ROUTE_REQUIRED"


def test_old_grade_and_hotel_publication_writes_are_disabled(integrated):
    c = integrated[0]
    login(c, "admin")
    uid = integrated[4]["employee"]["id"]
    result = c.post(f"/api/v1.5/users/{uid}/travel-profile",
                    json=dict(grade="A", effective_on="2026-01-01", reason="旧入口"))
    assert result.status_code == 409 and result.json()["error"]["code"] == "P4A_ROUTE_REQUIRED"
    travel = integrated[1].state.enterprise_app.state.travel
    actor = c.get("/api/v1.5/auth/me").json()["user"]
    from apps.api.errors import APIError

    with pytest.raises(APIError) as error:
        travel.publish("unused", actor, PublishTravelPolicy(
            rules=[rule()], expected_revision=0, confirmed=True,
            reason="旧酒店入口", idempotency_key=str(uuid4()),
        ))
    assert error.value.code == "P4A_ROUTE_REQUIRED"
    for path in ("confirm-rule", "publish", "lifecycle"):
        result = c.post("/api/v1.5/publication/policies/unused/" + path, json={})
        assert result.status_code == 409 and result.json()["error"]["code"] == "P4A_ROUTE_REQUIRED"


def test_inflight_legacy_snapshot_keeps_pinned_authority(integrated):
    c, app, repo, _, _, _ = integrated
    travel = app.state.enterprise_app.state.travel
    publish_nonhotel(integrated, "TRANSPORT", grades=["A"])
    facts = travel.facts
    # Model a saved pre-integration request, then activate P4A without changing its data.
    travel.facts = None
    try:
        claim, _ = submit_nonhotel(integrated, transport_mode="高铁", seat="二等座")
    finally:
        travel.facts = facts
    assert claim["employee_snapshot"]["grade"] == "A"
    assert "authority" not in claim["employee_snapshot"]
    master(integrated)  # New authoritative grade D must not rewrite the old snapshot.
    assert claim["ai_result"]["rule_results"][0]["outcome"] == "PASS"
    login(c, "finance")
    assert action(c, claim, "APPROVE").status_code == 200
    assert repo.erp_write_count(claim["claim_id"]) == 1


def test_p4a_mode_preserves_real_source_travel_publication(request, monkeypatch):
    from functools import partial

    from apps.api.config import Settings
    from tests.test_enterprise_travel_postgres import prepare

    monkeypatch.setattr("test_p4a_postgres.Settings", partial(
        Settings, policy_demo_enabled=True,
        policy_demo_token="synthetic-local-policy-test-capability-00000000",
    ))
    hotel_portal = request.getfixturevalue("hotel_portal")
    c, app, repo, store, _, _ = hotel_portal
    monkeypatch.setattr("apps.api.enterprise_travel.today", lambda: date(2026, 10, 11))

    class TestClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 4, tzinfo=UTC)

    monkeypatch.setattr("domain.submission.datetime", TestClock)
    login(c, "admin")
    master(hotel_portal)
    uid, base, body = prepare(c)  # Actual DOCX upload, PostgreSQL confirmation and SHA reparse.
    body["rules"] = [r for r in body["rules"] if r["expense_type"] != "HOTEL"]
    for standard in body["rules"]:
        standard.update(departments=["*"], positions=["*"])
    result = c.post(base + "/publish", json=body)
    assert result.status_code == 200, result.text
    assert c.post(base + "/publish", json=body).json() == result.json()
    snapshot = result.json()
    assert {r["expense_type"] for r in snapshot["rules"]} == {"TRANSPORT", "ALLOWANCE"}
    assert snapshot["file_sha256"] == app.state.policy_app.state.store.read(uid)["sha256"]
    login(c)
    submission = dict(
        fields=fields(category="TRANSPORT", nights=None, amount="900.00",
                      applicant_name="张三", department="研发部", position="工程师",
                      incurred_on="2026-11-02", trip_start="2026-11-02",
                      trip_end="2026-11-03"),
        travel_details=dict(expense_type="TRANSPORT", transport_mode="高铁", seat="二等座"),
        idempotency_key=str(uuid4()),
    )
    submitted = c.post("/api/v1.5/claims", json=submission)
    assert submitted.status_code == 202, submitted.text
    cid = submitted.json()["claim_id"]
    claim = c.get("/api/v1.5/claims/" + cid).json()
    assert claim["employee_snapshot"]["authority"] == "p4a.1"
    assert claim["ai_result"]["rule_results"][0]["policy_version_id"] == snapshot["policy_id"]
    login(c, "admin")
    retired = c.post(base + "/retire", json=dict(
        expected_revision=1, effective_on="2026-12-01", reason="合成退休",
        idempotency_key=str(uuid4()),
    ))
    assert retired.status_code == 200, retired.text
    assert c.post(f"/api/v1.5/policies/{uid}/delete", json={}).status_code == 409
    assert store.db.execute(
        "SELECT COUNT(*) FROM enterprise_events WHERE body LIKE '%TRAVEL_POLICY_PUBLISHED%'"
    ).fetchone()[0] == 1
    login(c, "finance")
    assert action(c, claim, "APPROVE").status_code == 200
    assert repo.erp_write_count(cid) == 1
