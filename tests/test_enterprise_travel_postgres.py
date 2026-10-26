"""Real uploaded source, SHA reparse, role/tenant and publication lifecycle acceptance."""

import os
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session
from test_enterprise_policy_scope import confirm_document, upload
from test_enterprise_policy_scope import portal_policy as portal_policy
from test_enterprise_rule_recovery import document
from test_policies_postgres import policy_pg as policy_pg

from database.models import PolicyUpload
from tests.test_enterprise import login

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="Disposable PostgreSQL required"),
]

CLAUSE = (
    "深圳 D职级，管理部专员住宿每晚限额800元。高铁二等座每次限额1000元。"
    "差旅补助每天180元，午餐扣40%，晚餐扣40%。"
)


def prepare(client):
    from scripts.enterprise_travel_migration import install

    install(client.app.state.enterprise_app.state.store.db)
    uid = upload(client, document([CLAUSE]))
    confirm_document(client, uid)
    base = f"/api/v1.5/travel/policies/{uid}"
    source = client.get(base)
    assert source.status_code == 200, source.text
    line = next(v for v in source.json()["parsed"]["lines"] if v["text"] == CLAUSE)
    rules = []
    for kind, unit, amount in [
        ("HOTEL", "NIGHT", "800.00"),
        ("TRANSPORT", "TRIP", "1000.00"),
        ("ALLOWANCE", "DAY", "180.00"),
    ]:
        rules.append(
            dict(
                expense_type=kind,
                unit=unit,
                limit=amount,
                cities=["深圳"],
                departments=["管理部"],
                positions=["专员"],
                grades=["D"],
                source_start=line["start"],
                source_end=line["end"],
                transport_mode="高铁" if kind == "TRANSPORT" else "",
                seats=["二等座"] if kind == "TRANSPORT" else [],
                lunch_rate="0.4" if kind == "ALLOWANCE" else "0",
                dinner_rate="0.4" if kind == "ALLOWANCE" else "0",
            )
        )
    return (
        uid,
        base,
        dict(
            rules=rules,
            expected_revision=0,
            confirmed=True,
            reason="合成制度发布核对",
            idempotency_key=str(uuid4()),
        ),
    )


def test_source_publication_replay_retirement_and_original_retained(portal_policy):
    client, engine, draft = portal_policy
    uid, base, body = prepare(client)
    before = draft.read(uid)
    result = client.post(base + "/publish", json=body)
    assert result.status_code == 200, result.text
    published = result.json()
    assert len(published["rules"]) == 3
    assert published["file_sha256"] == before["sha256"]
    assert client.post(base + "/publish", json=body).json() == published
    assert client.get(base).json()["revision"] == 1
    assert draft.read(uid) == before  # Publication cannot rewrite draft or metadata.
    assert client.post(f"/api/v1.5/policies/{uid}/delete", json={}).status_code == 409
    assert (
        client.post(
            base + "/publish",
            json={**body, "expected_revision": 1, "idempotency_key": str(uuid4())},
        ).status_code
        == 422
    )
    retired = client.post(
        base + "/retire",
        json=dict(
            expected_revision=1,
            effective_on="2026-12-01",
            reason="合成退休",
            idempotency_key=str(uuid4()),
        ),
    )
    assert retired.status_code == 200, retired.text
    assert client.get(base).json()["retirements"][0]["effective_on"] == "2026-12-01"
    assert draft.read(uid)["sha256"] == before["sha256"]


@pytest.mark.parametrize(
    "change", ["span", "amount", "seat", "currency", "overlap", "stale", "confirmed"]
)
def test_publication_validates_source_and_revision(portal_policy, change):
    client, engine, draft = portal_policy
    uid, base, body = prepare(client)
    if change == "span":
        body["rules"][0]["source_start"] += 1
    if change == "amount":
        body["rules"][0]["limit"] = "900.00"
    if change == "seat":
        body["rules"][1]["seats"] = ["商务座"]
    if change == "currency":
        body["rules"][0]["currency"] = "USD"
    if change == "overlap":
        body["rules"].append(body["rules"][0])
    if change == "stale":
        body["expected_revision"] = 1
    if change == "confirmed":
        body["confirmed"] = False
    result = client.post(base + "/publish", json=body)
    assert result.status_code in {422, 409}, result.text
    assert client.get(base).json()["versions"] == []


def test_source_tampering_and_cross_company_forbidden(portal_policy):
    client, engine, draft = portal_policy
    uid, base, body = prepare(client)
    login(client, "employee")
    assert client.get(base).status_code == 403
    assert client.post(base + "/publish", json=body).status_code == 403
    login(client, "admin", "DEMO-B")
    assert client.get(base).status_code == 404
    assert client.post(base + "/publish", json=body).status_code == 404
    login(client, "admin")
    with Session(engine) as session, session.begin():
        row = session.get(PolicyUpload, uid)
        row.original = bytes(row.original) + b"tampered"
    result = client.post(base + "/publish", json=body)
    assert result.status_code == 409 and result.json()["error"]["code"] == "FILE_INTEGRITY"


def test_backdated_publication_rejected(portal_policy, monkeypatch):
    client, engine, draft = portal_policy
    uid, base, body = prepare(client)
    monkeypatch.setattr("apps.api.enterprise_travel.today", lambda: date(2026, 11, 2))
    result = client.post(base + "/publish", json=body)
    assert (
        result.status_code == 422 and result.json()["error"]["code"] == "FUTURE_EFFECTIVE_REQUIRED"
    )
