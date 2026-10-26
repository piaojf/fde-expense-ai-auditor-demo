"""Manual evidence can recover grammar, never bypass conditional business rules."""

import os
from decimal import Decimal
from io import BytesIO
from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_enterprise_policy_scope import (
    confirm_document,
    upload,
)
from test_enterprise_policy_scope import (
    portal_policy as portal_policy,
)
from test_policies_postgres import policy_pg as policy_pg

from database.models import PolicyPublicationEvent, PolicyRuleReview, PolicyUpload, PolicyVersion
from integrations.policies.worker import WORD_NS
from tests.test_enterprise import login

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="Disposable PostgreSQL required"),
]
CLAUSE = "深圳每晚住宿费用不得超过人民币800元。"


def document(clauses, table=None):
    output = BytesIO()
    with (
        ZipFile("tests/fixtures/policies/publication-800.docx") as source,
        ZipFile(output, "w") as dest,
    ):
        for entry in source.infolist():
            value = source.read(entry)
            if entry.filename == "word/document.xml":
                root = ET.fromstring(value)
                body = root.find(WORD_NS + "body")
                for child in list(body)[8:]:
                    body.remove(child)
                for clause in clauses:
                    p = ET.SubElement(body, WORD_NS + "p")
                    ET.SubElement(ET.SubElement(p, WORD_NS + "r"), WORD_NS + "t").text = clause
                if table:
                    tbl = ET.SubElement(body, WORD_NS + "tbl")
                    for cells in table:
                        tr = ET.SubElement(tbl, WORD_NS + "tr")
                        for cell in cells:
                            tc = ET.SubElement(tr, WORD_NS + "tc")
                            p = ET.SubElement(tc, WORD_NS + "p")
                            ET.SubElement(
                                ET.SubElement(p, WORD_NS + "r"), WORD_NS + "t"
                            ).text = cell
                value = ET.tostring(root, encoding="utf-8")
            dest.writestr(entry, value)
    return output.getvalue()


def prepare(client, clauses, table=None):
    uid = upload(client, document(clauses, table))
    confirm_document(client, uid)
    base = f"/api/v1.5/publication/policies/{uid}"
    data = client.post(base + "/rules", json={}).json()
    return uid, base, data


def manual_body(line, **updates):
    return {
        "source_mode": "MANUAL",
        "actor": "forged",
        "reason": "合成原文补录测试",
        "confirmed": True,
        "amount": "800.00",
        "currency": "CNY",
        "clause_start": line["start"],
        "clause_end": line["end"],
        "expected_revision": 0,
        **updates,
    }


def source_line(data, text):
    return next(line for line in data["source_lines"] if line["text"] == text)


@pytest.mark.parametrize("lifecycle", ["RETIRE", "ROLLBACK"])
def test_manual_grammar_recovery_persists_publishes_replays_and_retires(portal_policy, lifecycle):
    client, engine, _ = portal_policy
    uid, base, data = prepare(client, [CLAUSE])
    assert "HOTEL_LIMIT_MISSING" in data["extraction"]["blockers"]
    body = manual_body(source_line(data, CLAUSE))
    result = client.post(base + "/confirm-rule", json=body)
    assert result.status_code == 200, result.text
    review = result.json()
    assert review["confirmed_rule"]["execution_status"] == "READY"
    assert review["extraction"] == data["extraction"]  # Never erase extraction warnings.
    assert review["actor"] == client.get("/api/v1.5/auth/me").json()["user"]["id"]
    assert client.post(base + "/confirm-rule", json=body).json()["error"]["code"] == "REVIEW_STALE"
    assert client.post(f"/api/v1.5/policies/{uid}/ingest", json={}).status_code == 200
    view_body = {"predecessor_id": "POL-SZ-2026"}
    preview = client.post(base + "/publication-preview", json=view_body).json()
    assert not preview["blockers"], preview
    publish_body = {
        **view_body,
        "confirmed": True,
        "preview_digest": preview["preview_digest"],
        "idempotency_key": str(uuid4()),
    }
    release = client.post(base + "/publish", json=publish_body)
    assert release.status_code == 200, release.text
    assert client.post(base + "/publish", json=publish_body).json() == release.json()
    pid = release.json()["policy"]["policy_version_id"]
    assert (
        client.post(base + "/confirm-rule", json={**body, "expected_revision": 1}).status_code
        == 409
    )
    with Session(engine) as session:
        saved = session.get(PolicyVersion, pid)
        assert Decimal(saved.authoritative_rules["hotel_limit_per_night"]) == Decimal("800")
        assert (saved.city, saved.category, saved.authority_level) == ("深圳", "HOTEL", 100)
        assert saved.authoritative_rules["source"]["quote"] == CLAUSE
    lifecycle_base = f"/api/v1.5/publication/policies/{pid}"
    change = {"kind": lifecycle, "effective_on": "2026-12-01"}
    if lifecycle == "ROLLBACK":
        change["restore_id"] = "POL-SZ-2026"
    view = client.post(lifecycle_base + "/lifecycle-preview", json=change).json()
    assert not view["blockers"], view
    changed = client.post(
        lifecycle_base + "/lifecycle",
        json={
            **change,
            "reason": "合成未来生命周期测试",
            "confirmed": True,
            "preview_digest": view["preview_digest"],
            "idempotency_key": str(uuid4()),
        },
    )
    assert changed.status_code == 200, changed.text


def test_table_rows_and_grade_conditions_remain_reference_only(portal_policy):
    client, engine, _ = portal_policy
    uid, base, data = prepare(
        client,
        ["第五章 住宿费标准", "职级及城市档位决定每晚标准。"],
        [
            ["地区", "职级", "每晚住宿标准（人民币元）"],
            ["深圳", "D", "800"],
            ["深圳", "B", "1000"],
        ],
    )
    assert data["condition_warning"] is True
    assert len(data["reference_tables"]) == 3
    row = data["reference_tables"][1]
    assert row["cells"] == ["深圳", "D", "800"]
    body = manual_body(row, conditions="仅深圳 D 级员工，待接入可信职级")
    result = client.post(base + "/confirm-rule", json=body)
    assert result.status_code == 200, result.text
    rule = result.json()["confirmed_rule"]
    assert rule["execution_status"] == "REFERENCE_ONLY"
    assert "RULE_CONDITIONS_UNSUPPORTED" in rule["execution_blockers"]
    assert rule["clause"]["quote"] == "深圳\nD\n800"
    assert client.post(f"/api/v1.5/policies/{uid}/ingest", json={}).status_code == 200
    view = client.post(base + "/publication-preview", json={"predecessor_id": "POL-SZ-2026"}).json()
    assert view["blockers"] == ["RULE_REFERENCE_ONLY"], view
    rejected = client.post(
        base + "/publish",
        json={
            "predecessor_id": "POL-SZ-2026",
            "confirmed": True,
            "preview_digest": "0" * 64,
            "idempotency_key": str(uuid4()),
        },
    )
    assert rejected.status_code == 409
    with Session(engine) as session:
        assert (
            session.scalar(
                select(PolicyPublicationEvent).where(PolicyPublicationEvent.upload_id == uid)
            )
            is None
        )
        saved = session.scalar(select(PolicyRuleReview).where(PolicyRuleReview.upload_id == uid))
        assert saved.confirmed_rule["conditions"] == body["conditions"]


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": "1800.00"},
        {"clause_start": 1},
        {"clause_end": 100000},
        {"currency": "USD"},
        {"conditions": "x" * 1001},
        {"confirmed": False},
        {"source_mode": "UNSAFE"},
        {"amount": 800.00},
    ],
)
def test_manual_invalid_values_cannot_create_review(portal_policy, changes):
    client, engine, _ = portal_policy
    uid, base, data = prepare(client, [CLAUSE])
    result = client.post(
        base + "/confirm-rule", json=manual_body(source_line(data, CLAUSE), **changes)
    )
    assert result.status_code in {409, 422}, result.text
    with Session(engine) as session:
        assert (
            session.scalar(select(PolicyRuleReview).where(PolicyRuleReview.upload_id == uid))
            is None
        )


@pytest.mark.parametrize(
    "clause,other,blocker",
    [
        ("深圳住宿每晚标准800元，B级1000元", [], "RULE_CONDITIONS_UNSUPPORTED"),
        ("深圳住宿每晚标准800元", ["深圳住宿每晚标准900元"], "CONFLICTING_HOTEL_LIMITS"),
        ("深圳住宿上限800元", [], "RULE_NIGHT_UNIT_REQUIRED"),
        ("深圳每晚住宿费用不得超过人民币800元", ["职级决定限额"], "RULE_CONDITIONS_UNSUPPORTED"),
        ("深圳住宿每晚上限800元", [], "RULE_CONDITIONS_UNSUPPORTED"),
    ],
)
def test_manual_conditions_do_not_disappear_when_selecting_one_clause(
    portal_policy, clause, other, blocker
):
    client, _, _ = portal_policy
    uid, base, data = prepare(client, [clause, *other])
    conditions = "人工待核实条件" if not other and clause == "深圳住宿每晚上限800元" else ""
    result = client.post(
        base + "/confirm-rule", json=manual_body(source_line(data, clause), conditions=conditions)
    )
    assert result.status_code == 200, result.text
    assert result.json()["confirmed_rule"]["execution_status"] == "REFERENCE_ONLY"
    assert blocker in result.json()["confirmed_rule"]["execution_blockers"]
    client.post(f"/api/v1.5/policies/{uid}/ingest", json={})
    assert client.post(base + "/publication-preview", json={}).json()["blockers"] == [
        "RULE_REFERENCE_ONLY"
    ]


def test_manual_tamper_role_and_tenant_protection(portal_policy):
    client, engine, _ = portal_policy
    uid, base, data = prepare(client, [CLAUSE])
    body = manual_body(source_line(data, CLAUSE))
    login(client, "employee")
    assert client.post(base + "/rules", json={}).status_code == 403
    assert client.post(base + "/confirm-rule", json=body).status_code == 403
    login(client, "admin", "DEMO-B")
    assert client.post(base + "/rules", json={}).status_code == 404
    assert client.post(base + "/confirm-rule", json=body).status_code == 404
    login(client, "admin")
    with Session(engine) as session, session.begin():
        row = session.get(PolicyUpload, uid)
        row.parsed = {**row.parsed, "text": row.parsed["text"].replace("800", "900")}
    result = client.post(base + "/confirm-rule", json=body)
    assert result.status_code == 409 and result.json()["error"]["code"] == "PARSE_INTEGRITY"


def test_manual_review_tamper_blocks_preview(portal_policy):
    client, engine, _ = portal_policy
    uid, base, data = prepare(client, [CLAUSE])
    review = client.post(base + "/confirm-rule", json=manual_body(source_line(data, CLAUSE))).json()
    client.post(f"/api/v1.5/policies/{uid}/ingest", json={})
    with pytest.raises(SQLAlchemyError, match="immutable"):
        with Session(engine) as session, session.begin():
            row = session.get(PolicyRuleReview, review["review_id"])
            row.confirmed_rule = {**row.confirmed_rule, "amount": "900.00"}
    # Simulate a corrupted new DB record without disabling immutable-history triggers.
    with Session(engine) as session, session.begin():
        row = session.get(PolicyRuleReview, review["review_id"])
        values = {c.key: getattr(row, c.key) for c in PolicyRuleReview.__table__.columns}
        session.add(
            PolicyRuleReview(
                **{
                    **values,
                    "review_id": str(uuid4()),
                    "revision": 2,
                    "confirmed_rule": {**row.confirmed_rule, "amount": "900.00"},
                }
            )
        )
    view = client.post(base + "/publication-preview", json={"predecessor_id": "POL-SZ-2026"}).json()
    assert view["blockers"] == ["RULE_SOURCE_MISMATCH"], view
