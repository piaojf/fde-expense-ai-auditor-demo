"""Travel standards run through real enterprise SQLite, graph and finance authority."""

import json
import sqlite3
from datetime import date
from uuid import uuid4

import pytest
from pydantic import ValidationError

from apps.api.enterprise_travel import fingerprint
from apps.api.errors import APIError
from domain.enterprise_travel import PublishTravelPolicy, TravelDetails, TravelProfile, TravelRule
from tests.test_enterprise import action, fields, login
from tests.test_enterprise import portal as portal

SOURCE = (
    "深圳、杭州；华南销售部；客户经理；D职级。住宿每晚800元，杭州650元。\n"
    "高铁允许二等座；飞机允许经济舱，每次交通限额1000元。\n"
    "差旅补助每天180元，早餐扣减20%，午餐40%，晚餐40%。"
)


@pytest.fixture
def travel(portal, monkeypatch):
    client, app, repo, store = portal
    from scripts.enterprise_travel_migration import install

    install(store.db)
    travel = app.state.enterprise_app.state.travel
    monkeypatch.setattr("apps.api.enterprise_travel.today", lambda: date(2026, 8, 1))
    lines, start = [], 0
    for n, text in enumerate(SOURCE.splitlines()):
        lines.append(dict(text=text, start=start, end=start + len(text), line=n + 1))
        start += len(text) + 1
    source = dict(
        upload_id="synthetic-source",
        sha256="0" * 64,
        metadata=dict(
            title="测试制度",
            legal_entity="DEMO-CN",
            region="*",
            expense_type="ALL",
            version="test-v2",
            effective_from="2026-09-01",
            effective_to="2027-01-01",
        ),
        parsed=dict(text=SOURCE, lines=lines),
    )
    monkeypatch.setattr(travel, "source", lambda uid, actor: source)
    login(client, "admin")
    actor = client.get("/api/v1.5/auth/me").json()["user"]
    employee = next(u for u in store.users("DEMO-CN") if u["username"] == "employee")
    travel.set_profile(
        actor,
        employee["id"],
        TravelProfile(grade="D", effective_on="2026-01-01", reason="合成人事档案"),
    )
    return client, app, repo, store, travel, actor, source


def rule(kind="HOTEL", **updates):
    return (
        dict(
            expense_type=kind,
            cities=["深圳"],
            departments=["华南销售部"],
            positions=["客户经理"],
            grades=["D"],
            unit="NIGHT" if kind == "HOTEL" else "DAY" if kind == "ALLOWANCE" else "TRIP",
            limit="800.00" if kind == "HOTEL" else "180.00" if kind == "ALLOWANCE" else "1000.00",
            transport_mode="高铁" if kind == "TRANSPORT" else "",
            seats=["二等座"] if kind == "TRANSPORT" else [],
            source_start=0,
            source_end=len(SOURCE),
            **{},
        )
        | updates
    )


def publish(env, rules=None):
    client, app, repo, store, travel, actor, source = env
    body = PublishTravelPolicy(
        rules=rules or [rule()],
        expected_revision=0,
        confirmed=True,
        reason="合成制度人工核对",
        idempotency_key=str(uuid4()),
    )
    result = travel.publish("synthetic-source", actor, body)
    return result, body


def submit(env, kind="HOTEL", **updates):
    client = env[0]
    login(client)
    extra = updates.pop("details", {})
    detail = TravelDetails(expense_type=kind, **extra).model_dump(mode="json")
    body = dict(
        fields=fields(
            category="OTHER" if kind == "ALLOWANCE" else kind,
            nights=updates.pop("nights", 1 if kind == "HOTEL" else None),
            **updates,
        ),
        travel_details=detail,
        idempotency_key=str(uuid4()),
    )
    response = client.post("/api/v1.5/claims", json=body)
    assert response.status_code == 202, response.text
    result = client.get("/api/v1.5/claims/" + response.json()["claim_id"]).json()
    assert result["stage"] == "FINANCE_REVIEW", result
    return result, body


@pytest.mark.parametrize(
    "kind,amount,details,expected,limit",
    [
        ("HOTEL", "780.00", {}, "PASS", "800.00"),
        ("HOTEL", "801.00", {}, "FAIL", "800.00"),
        ("TRANSPORT", "900.00", dict(transport_mode="高铁", seat="二等座"), "PASS", "1000.00"),
        ("TRANSPORT", "900.00", dict(transport_mode="高铁", seat="一等座"), "FAIL", None),
        ("TRANSPORT", "1001.00", dict(transport_mode="高铁", seat="二等座"), "FAIL", "1000.00"),
        ("ALLOWANCE", "36.00", dict(allowance_days=1, lunches=1, dinners=1), "PASS", "36.00"),
        ("ALLOWANCE", "37.00", dict(allowance_days=1, lunches=1, dinners=1), "FAIL", "36.00"),
    ],
)
def test_all_categories_and_finance_authority(travel, kind, amount, details, expected, limit):
    kwargs = dict(lunch_rate="0.40", dinner_rate="0.40") if kind == "ALLOWANCE" else {}
    snapshot, _ = publish(travel, [rule(kind, **kwargs)])
    claim, body = submit(travel, kind, amount=amount, details=details)
    checked = claim["ai_result"]["rule_results"][0]
    assert checked["outcome"] == expected and checked["limit"] == limit
    assert checked["policy_version_id"] == snapshot["policy_id"]
    assert claim["ai_analysis"]["provider"] == "mock"
    assert claim["employee_snapshot"]["grade"] == "D"
    assert travel[2].erp_write_count(claim["claim_id"]) == 0
    login(travel[0], "finance")
    result = action(travel[0], claim, "APPROVE")
    if expected == "PASS":
        assert result.status_code == 200, result.text
        assert result.json()["stage"] == "APPROVED"
        assert travel[2].erp_write_count(claim["claim_id"]) == 1
    else:
        assert result.status_code == 409, result.text
        assert travel[2].erp_write_count(claim["claim_id"]) == 0
        assert action(travel[0], claim, "RETURN").status_code == 200


@pytest.mark.parametrize(
    "change",
    [
        dict(cities=["杭州"]),
        dict(departments=["研发部"]),
        dict(positions=["总监"]),
        dict(grades=["C"]),
    ],
)
def test_every_identity_dimension_must_match(travel, change):
    publish(travel, [rule(**change)])
    claim, _ = submit(travel)
    assert claim["ai_result"]["rule_results"][0]["outcome"] == "UNKNOWN"
    login(travel[0], "finance")
    assert action(travel[0], claim, "APPROVE").status_code == 409
    assert travel[2].erp_write_count(claim["claim_id"]) == 0


def test_profile_snapshot_cannot_be_spoofed_or_rewritten(travel):
    from domain.enterprise_travel import TravelProfile

    publish(travel)
    claim, body = submit(travel)
    employee = next(u for u in travel[3].users("DEMO-CN") if u["username"] == "employee")
    travel[4].set_profile(
        travel[5],
        employee["id"],
        TravelProfile(grade="A", effective_on="2026-01-01", reason="后续更正"),
    )
    assert travel[4].profile(employee, "2026-09-20")["grade"] == "A"
    assert (
        travel[0].get("/api/v1.5/claims/" + claim["claim_id"]).json()["employee_snapshot"]["grade"]
        == "D"
    )
    body["employee_snapshot"] = {"grade": "A"}
    assert travel[0].post("/api/v1.5/claims", json=body).status_code == 422
    login(travel[0], "finance")
    assert action(travel[0], claim, "APPROVE").status_code == 200


@pytest.mark.parametrize(
    "details",
    [
        dict(expense_type="ALLOWANCE", allowance_days=1, lunches=2),
        dict(expense_type="HOTEL", lunches=1),
        dict(expense_type="TRANSPORT", allowance_days=1),
    ],
)
def test_unrelated_or_excess_meals_rejected(details):
    with pytest.raises(ValidationError):
        TravelDetails(**details)


def test_missing_rules_missing_facts_and_conflicts_are_unknown(travel):
    claim, _ = submit(travel)
    assert claim["ai_result"]["rule_results"][0]["outcome"] == "UNKNOWN"
    snapshot, _ = publish(travel, [rule("TRANSPORT")])
    claim, _ = submit(travel, "TRANSPORT")
    assert claim["ai_result"]["rule_results"][0]["outcome"] == "UNKNOWN"
    # A separately published overlapping source must not silently win by publication order.
    copy = {**snapshot, "policy_id": "TRAVEL-POL-conflict"}
    copy["sha256"] = fingerprint({k: v for k, v in copy.items() if k != "sha256"})
    with travel[3].lock, travel[3].db:
        travel[3].event(travel[5], "TRAVEL_POLICY_PUBLISHED", snapshot=copy)
    claim, _ = submit(travel, "TRANSPORT", details=dict(transport_mode="高铁", seat="二等座"))
    assert claim["ai_result"]["rule_results"][0]["outcome"] == "UNKNOWN"


@pytest.mark.parametrize(
    "invalid",
    [
        dict(limit="999.00"),
        dict(cities=["*", "深圳"]),
        dict(expense_type="TRANSPORT", unit="TRIP", transport_mode="高铁", seats=["一等座"]),
        dict(expense_type="ALLOWANCE", unit="DAY", limit="180.00", lunch_rate="0.35"),
    ],
)
def test_invalid_standard_and_ungrounded_source_rejected(travel, invalid):
    with pytest.raises((ValidationError, APIError)):
        publish(travel, [rule(**invalid)])
    assert travel[4].publications("DEMO-CN") == []


def test_publication_replay_immutable_and_tenant_authority(travel):
    snapshot, body = publish(travel)
    assert travel[4].publish("synthetic-source", travel[5], body) == snapshot
    assert len(travel[4].publications("DEMO-CN")) == 1
    login(travel[0])
    assert travel[0].get("/api/v1.5/travel/profiles").status_code == 403
    uid = next(u["id"] for u in travel[3].users("DEMO-B") if u["username"] == "employee")
    login(travel[0], "admin")
    response = travel[0].post(
        f"/api/v1.5/users/{uid}/travel-profile",
        json=dict(grade="A", effective_on="2026-01-01", reason="跨企业"),
    )
    assert response.status_code == 404
    assert travel[4].publications("DEMO-B") == []


def test_review_required_future_missing_and_trip_bounds(travel):
    publish(travel, [rule(review_required=True)])
    claim, _ = submit(travel)
    assert claim["ai_result"]["rule_results"][0]["outcome"] == "UNKNOWN"


def test_snapshot_corruption_fails_closed(travel):
    publish(travel)
    with travel[3].db:
        row = (
            travel[3]
            .db.execute(
                "SELECT seq,body FROM enterprise_events WHERE body LIKE '%TRAVEL_POLICY_PUBLISHED%'"
            )
            .fetchone()
        )
        body = json.loads(row[1])
        body["snapshot"]["rules"][0]["limit"] = "9999.00"
        with pytest.raises(sqlite3.IntegrityError):
            travel[3].db.execute(
                "UPDATE enterprise_events SET body=? WHERE seq=?", (json.dumps(body), row[0])
            )
        travel[3].event(travel[5], "TRAVEL_POLICY_PUBLISHED", snapshot=body["snapshot"])
    login(travel[0])
    body = dict(
        fields=fields(),
        travel_details=TravelDetails(expense_type="HOTEL").model_dump(mode="json"),
        idempotency_key=str(uuid4()),
    )
    result = travel[0].post("/api/v1.5/claims", json=body)
    claim = travel[0].get("/api/v1.5/claims/" + result.json()["claim_id"]).json()
    assert claim["stage"] in {"SYSTEM_ERROR", "FINANCE_REVIEW"}
    assert not claim["ai_result"] or all(
        r["outcome"] != "PASS" for r in claim["ai_result"]["rule_results"]
    )
    assert travel[2].erp_write_count(claim["claim_id"]) == 0


def test_money_decimal_and_rule_contract():
    with pytest.raises(ValidationError):
        TravelRule(**rule(limit="800.001"))
    with pytest.raises(ValidationError):
        TravelRule(**rule("ALLOWANCE", breakfast_rate=".3", lunch_rate=".4", dinner_rate=".4"))


def test_omitting_details_cannot_bypass_published_grade_standard(travel):
    publish(travel, [rule(grades=["A"])])
    login(travel[0])
    response = travel[0].post(
        "/api/v1.5/claims", json=dict(fields=fields(), idempotency_key=str(uuid4()))
    )
    claim = travel[0].get("/api/v1.5/claims/" + response.json()["claim_id"]).json()
    assert claim["travel_details"]["expense_type"] == "HOTEL"
    assert claim["ai_result"]["rule_results"][0]["outcome"] == "UNKNOWN"
    login(travel[0], "finance")
    assert action(travel[0], claim, "APPROVE").status_code == 409


def test_existing_provider_receives_dimensions_calculation_and_exact_evidence(travel):
    from integrations.llm.mock import MockLLMProvider

    class CapturingProvider(MockLLMProvider):
        async def analyze(self, request):
            self.request = request
            return await super().analyze(request)

    provider = CapturingProvider()
    travel[1].state.audit_service.graph_factory.keywords["provider"] = provider
    publish(travel, [rule("ALLOWANCE", lunch_rate=".4", dinner_rate=".4")])
    submit(
        travel, "ALLOWANCE", amount="36.00", details=dict(allowance_days=1, lunches=1, dinners=1)
    )
    facts = provider.request.facts
    assert facts["department"] == "华南销售部" and facts["position"] == "客户经理"
    assert facts["grade"] == "D" and facts["expense_type"] == "ALLOWANCE"
    assert facts["standard_outcome"] == "PASS" and facts["limit"] == "36.00"
    assert provider.request.evidence[0].quote == SOURCE


@pytest.mark.parametrize(
    "kind,details,updates",
    [
        ("ALLOWANCE", dict(allowance_days=3), {}),
        ("HOTEL", {}, dict(nights=3)),
    ],
)
def test_declared_units_exceeding_trip_are_unknown(travel, kind, details, updates):
    publish(travel, [rule(kind)])
    claim, _ = submit(travel, kind, details=details, **updates)
    assert claim["ai_result"]["rule_results"][0]["outcome"] == "UNKNOWN"


def test_seat_only_standard_and_multiple_transport_units(travel):
    publish(travel, [rule("TRANSPORT", limit=None)])
    claim, _ = submit(
        travel,
        "TRANSPORT",
        amount="1600.00",
        details=dict(transport_mode="高铁", seat="二等座", transport_count=2),
    )
    assert claim["ai_result"]["rule_results"][0]["outcome"] == "PASS"
    assert claim["ai_result"]["rule_results"][0]["limit"] is None


def test_retirement_preserves_pinned_human_review(travel):
    publish(travel)
    claim, _ = submit(travel)
    # Later retirement event cannot change a run already pinned before that event.
    with travel[3].lock, travel[3].db:
        travel[3].event(
            travel[5],
            "TRAVEL_POLICY_RETIRED",
            policy_id=claim["ai_result"]["rule_results"][0]["policy_version_id"],
            upload_id="synthetic-source",
            effective_on="2026-09-10",
            reason="合成后续退休事件",
        )
    login(travel[0], "finance")
    assert action(travel[0], claim, "APPROVE").status_code == 200
    newer, _ = submit(travel)
    assert newer["ai_result"]["rule_results"][0]["outcome"] == "UNKNOWN"


def test_schema_migration_is_explicit_and_required(portal):
    from scripts.enterprise_travel_migration import install

    client, app, repo, store = portal
    travel = app.state.enterprise_app.state.travel
    with pytest.raises(APIError) as error:
        travel.require_schema()
    assert error.value.code == "TRAVEL_SCHEMA_REQUIRED"
    install(store.db)
    install(store.db)
    travel.require_schema()


def test_published_events_and_employee_snapshot_are_database_immutable(travel):
    publish(travel)
    claim, _ = submit(travel)
    db = travel[3].db
    with pytest.raises(sqlite3.IntegrityError):
        with db:
            db.execute(
                "DELETE FROM enterprise_events "
                "WHERE json_extract(body,'$.kind')='TRAVEL_POLICY_PUBLISHED'"
            )
    raw = db.execute(
        "SELECT body FROM enterprise_versions WHERE claim_id=?", (claim["claim_id"],)
    ).fetchone()[0]
    changed = json.loads(raw)
    changed["employee_snapshot"]["grade"] = "A"
    with pytest.raises(sqlite3.IntegrityError):
        with db:
            db.execute(
                "UPDATE enterprise_versions SET body=? WHERE claim_id=?",
                (json.dumps(changed), claim["claim_id"]),
            )


def test_daily_transport_uses_declared_days_not_entire_trip(travel):
    daily = "深圳华南销售部客户经理 D职级，高铁二等座每天限额180元。"
    travel[6]["parsed"] = {
        "text": daily,
        "lines": [{"text": daily, "line": 1, "start": 0, "end": len(daily)}],
    }
    publish(travel, [rule("TRANSPORT", unit="DAY", limit="180.00", source_end=len(daily))])
    claim, _ = submit(
        travel,
        "TRANSPORT",
        amount="181.00",
        details=dict(transport_mode="高铁", seat="二等座", transport_count=1),
    )
    assert claim["ai_result"]["rule_results"][0]["limit"] == "180.00"
    assert claim["ai_result"]["rule_results"][0]["outcome"] == "FAIL"


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"cities": ["杭州"]}, "城市为深圳，标准适用杭州"),
        ({"departments": ["研发部"]}, "部门为华南销售部，标准适用研发部"),
        ({"positions": ["总监"]}, "职位为客户经理，标准适用总监"),
        ({"grades": ["C"]}, "职级为D，标准适用C"),
        ({"transport_mode": "飞机", "seats": ["经济舱"]}, "交通工具为高铁，标准适用飞机"),
    ],
)
def test_unmatched_standard_explains_actual_dimension(travel, change, expected):
    publish(travel, [rule("TRANSPORT", **change)])
    claim, _ = submit(travel, "TRANSPORT", details={"transport_mode": "高铁", "seat": "二等座"})
    checked = claim["ai_result"]["rule_results"][0]
    assert checked["outcome"] == "UNKNOWN"
    assert expected in checked["reason"]
    assert "测试制度／TRAVEL-1" in checked["reason"]
    login(travel[0], "finance")
    assert action(travel[0], claim, "APPROVE").status_code == 409
    assert travel[2].erp_write_count(claim["claim_id"]) == 0


def test_no_publication_and_no_category_have_distinct_causes(travel):
    claim, _ = submit(travel, "TRANSPORT", details={"transport_mode": "飞机", "seat": "经济舱"})
    reason = claim["ai_result"]["rule_results"][0]["reason"]
    assert "企业尚未正式发布交通费标准" in reason
    assert "企业管理员" in reason
    publish(travel)
    claim, _ = submit(travel, "TRANSPORT", details={"transport_mode": "飞机", "seat": "经济舱"})
    assert "没有交通费规则" in claim["ai_result"]["rule_results"][0]["reason"]


@pytest.mark.parametrize("incurred", ["2026-08-31", "2026-09-21"])
def test_unmatched_standard_explains_incurred_date(travel, incurred):
    travel[6]["metadata"]["effective_to"] = "2026-09-21"
    publish(travel)
    claim, _ = submit(travel, incurred_on=incurred)
    reason = claim["ai_result"]["rule_results"][0]["reason"]
    assert f"费用发生日{incurred}没有有效的住宿费标准" in reason
    assert ("尚未生效，生效日为2026-09-01" if incurred == "2026-08-31"
            else "已到期，截止日为2026-09-21") in reason


def test_historical_diagnosis_ignores_later_publications(travel):
    claim, _ = submit(travel, "TRANSPORT", details={"transport_mode": "飞机", "seat": "经济舱"})
    version = travel[3].history(claim["claim_id"])[-1]
    context = {"started_at": version["submitted_at"]}
    original = travel[4].unmatched_diagnosis("DEMO-CN", version, context)
    publish(travel, [rule("TRANSPORT", transport_mode="飞机", seats=["经济舱"])])
    assert travel[4].unmatched_diagnosis("DEMO-CN", version, context) == original
    assert "企业尚未正式发布交通费标准" in original



def test_historical_detail_explains_without_rewriting_audit(travel):
    claim, _ = submit(travel, "TRANSPORT", details={"transport_mode": "飞机", "seat": "经济舱"})
    cid, run_id = claim["claim_id"], claim["claim"]["active_run_id"]
    context = travel[2].run_context(cid, run_id)
    legacy = "缺少适用的已发布标准，请核对城市、职位/职级、部门及生效日期"
    context["ai_decision_result"]["rule_results"][0]["reason"] = legacy
    with travel[3].db:
        travel[3].db.execute("UPDATE runs SET body=? WHERE claim_id=? AND run_id=?",
                             (json.dumps(context), cid, run_id))
    before = travel[3].db.execute("SELECT body FROM runs WHERE claim_id=?", (cid,)).fetchone()[0]
    result = travel[0].get("/api/v1.5/claims/" + cid).json()
    assert "企业尚未正式发布交通费标准" in result["travel_standard_diagnosis"]
    assert result["ai_result"]["rule_results"][0]["reason"] == legacy
    after = travel[3].db.execute("SELECT body FROM runs WHERE claim_id=?", (cid,)).fetchone()[0]
    assert after == before
    assert travel[2].erp_write_count(cid) == 0
