"""Read-only policy files: real tenant boundaries and byte-exact downloads, no models."""

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from test_enterprise_policy_scope import portal_policy as portal_policy
from test_enterprise_policy_scope import upload
from test_enterprise_travel_postgres import prepare as prepare_travel
from test_policies import META, TOKEN
from test_policies_postgres import policy_pg as policy_pg

from apps.api.enterprise import COOKIE
from apps.api.policy_library import publication_status
from database.models import PolicyPublicationEvent, PolicyUpload, PolicyVersion
from domain.enterprise import CreateUser
from domain.policies import ConfirmPolicy
from tests.test_enterprise import PASSWORD, login

BASE = "/api/v1.5/policies/files"


@pytest.mark.parametrize("start,end,cutovers,expected", [
    ("2026-11-01", None, [], "SCHEDULED"),
    ("2026-10-01", None, [], "EFFECTIVE"),
    ("2026-10-01", "2026-10-20", [], "EXPIRED"),
    ("2026-10-01", None, [("2026-10-20", "PUBLISH")], "REPLACED"),
    ("2026-10-01", None, [("2026-10-20", "ROLLBACK")], "REPLACED"),
    ("2026-10-01", None, [("2026-10-20", "RETIRE")], "RETIRED"),
    ("2026-10-01", None, [("2026-11-01", "PUBLISH")], "EFFECTIVE"),
])
def test_half_open_dates_and_cutovers(start, end, cutovers, expected):
    assert publication_status(
        date.fromisoformat(start), date.fromisoformat(end) if end else None,
        [(date.fromisoformat(day), kind) for day, kind in cutovers], date(2026, 10, 20),
    ) == expected


@pytest.fixture
def library(portal_policy):
    client, engine, draft = portal_policy
    store = client.app.state.enterprise_app.state.store
    for company in ("DEMO-CN", "DEMO-B"):
        store.create_user(company, CreateUser(
            username="finance", password=PASSWORD, name="合成财务", department="财务部",
            position="审核员", role="FINANCE"))
    uid = upload(client)
    login(client, "admin", "DEMO-B")
    foreign = client.post("/api/v1.5/policies/upload", files={"file": (
        "foreign-company.docx", Path("tests/fixtures/policies/draft-800.docx").read_bytes(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )})
    assert foreign.status_code == 200, foreign.text
    login(client, "admin")
    return client, engine, draft, uid, foreign.json()["upload_id"], store


def pg_snapshot(engine):
    with engine.connect() as connection:
        return {table: connection.execute(text(
            f"SELECT row_to_json(t)::text FROM {table} t ORDER BY 1"
        )).scalars().all() for table in (
            "policy_uploads", "policy_versions", "policy_chunks", "policy_rule_reviews",
            "policy_publication_events")}


@pytest.mark.parametrize("role", ["admin", "finance"])
def test_owned_list_download_and_no_side_effects(library, monkeypatch, role):
    client, engine, draft, uid, foreign, store = library
    login(client, role)

    def forbidden(*args, **kwargs):
        pytest.fail("Read-only file access invoked parser, indexer or embedding")

    monkeypatch.setattr(draft.parser, "parse", forbidden)
    monkeypatch.setattr(draft, "ingest", forbidden)
    monkeypatch.setattr(draft, "embedding_factory", forbidden)
    before = pg_snapshot(engine)
    sqlite_before = list(store.db.iterdump())
    response = client.get(BASE)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["upload_id"] for item in items] == [uid]
    assert items[0]["status"] == "DRAFT"
    assert set(items[0]) == {
        "upload_id", "filename", "version", "status", "effective_from", "effective_to"}
    assert foreign not in response.text and TOKEN not in response.text
    download = client.get(f"{BASE}/{uid}/original")
    assert download.status_code == 200
    assert download.content == Path("tests/fixtures/policies/publication-800.docx").read_bytes()
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["cache-control"] == "no-store"
    assert download.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in download.headers["content-security-policy"]
    assert client.get(f"{BASE}/{foreign}/original").status_code == 404
    assert client.get(f"{BASE}/{uuid4()}/original").status_code == 404
    assert pg_snapshot(engine) == before
    assert list(store.db.iterdump()) == sqlite_before
    login(client, role, "DEMO-B")
    assert [item["upload_id"] for item in client.get(BASE).json()["items"]] == [foreign]
    assert client.get(f"{BASE}/{uid}/original").status_code == 404


def test_finance_cannot_use_admin_routes_and_revocation_is_live(library):
    client, _, _, uid, _, store = library
    login(client, "finance")
    for path in ["/api/v1.5/policies/list", "/api/v1.5/policies/upload",
                 f"/api/v1.5/publication/policies/{uid}/publish", *[
                     f"/api/v1.5/policies/{uid}/{action}"
                     for action in ("read", "original", "confirm", "ingest", "preview", "delete")]]:
        if path.endswith("/upload"):
            response = client.post(path, files={
                "file": ("policy.docx", b"not-read", "application/octet-stream")})
        else:
            response = client.post(path, json={})
        assert response.status_code == 403, path
    saved = client.cookies.get(COOKIE)
    actor = client.get("/api/v1.5/auth/me").json()["user"]["id"]
    with store.lock, store.db:
        store.db.execute("UPDATE enterprise_users SET enabled=0 WHERE id=?", (actor,))
    assert client.cookies.get(COOKIE) == saved
    assert client.get(BASE).status_code == 401
    assert client.get(f"{BASE}/{uid}/original").status_code == 401
    client.cookies.clear()
    assert login(client, "employee").status_code == 200
    assert client.get(BASE).status_code == 403
    assert client.get(f"{BASE}/{uid}/original").status_code == 403
    client.cookies.clear()
    assert client.get(BASE).status_code == 401
    assert client.get(f"{BASE}/{uid}/original").status_code == 401


def test_published_status_uses_events_and_original_hash_is_enforced(library):
    client, engine, draft, uid, _, _ = library
    metadata = {**META, "version": "2026-11-800", "effective_from": "2026-11-01"}
    draft.confirm(uid, ConfirmPolicy(token=TOKEN, confirmed=True, metadata=metadata))
    pid = "LIBRARY-" + uuid4().hex
    with Session(engine) as session, session.begin():
        source = session.get(PolicyUpload, uid)
        session.add(PolicyVersion(
            policy_version_id=pid, policy_key=pid, version="library-test-v1",
            title="合成已发布制度",
            status="ACTIVE", authority_level=100, legal_entity="DEMO-CN", category="HOTEL",
            city="深圳", effective_from=date(2026, 11, 1), effective_to=date(2027, 1, 1),
            published_at=datetime(2026, 10, 10, tzinfo=UTC),
            authoritative_rules={"upload_id": uid}, source_uri="fixture://library",
            content_sha256=source.sha256))
    publisher = client.app.state.publication_app.state.store
    assert client.get(BASE).json()["items"][0]["status"] == "SCHEDULED"
    publisher.clock = lambda: datetime(2026, 11, 2, tzinfo=UTC)
    item = client.get(BASE).json()["items"][0]
    assert item["status"] == "EFFECTIVE"
    assert item["version"] == metadata["version"]
    assert item["effective_from"] == metadata["effective_from"]
    with Session(engine) as session, session.begin():
        session.add(PolicyPublicationEvent(
            event_id=str(uuid4()), idempotency_key=str(uuid4()), request_sha256="a" * 64,
            kind="RETIRE", predecessor_id=pid, effective_on=date(2026, 11, 3),
            recorded_at=datetime(2026, 10, 10, tzinfo=UTC), actor="synthetic-admin",
            summary={}, result={}))
    assert client.get(BASE).json()["items"][0]["status"] == "EFFECTIVE"
    publisher.clock = lambda: datetime(2026, 11, 3, tzinfo=UTC)
    assert client.get(BASE).json()["items"][0]["status"] == "RETIRED"
    with Session(engine) as session, session.begin():
        source = session.get(PolicyUpload, uid)
        source.original = bytes(source.original) + b"tampered"
    response = client.get(f"{BASE}/{uid}/original")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "FILE_INTEGRITY"


def test_pre_portal_published_sources_require_provenance_and_cannot_override_an_owner(library):
    client, engine, draft, uid, _, store = library
    draft.confirm(uid, ConfirmPolicy(token=TOKEN, confirmed=True, metadata=META))
    with store.lock, store.db:
        store.db.execute("DELETE FROM enterprise_resources WHERE kind='policy' AND id=?", (uid,))
    login(client, "finance")
    # Same-company draft metadata alone is insufficient.
    assert client.get(BASE).json()["items"] == []
    assert client.get(f"{BASE}/{uid}/original").status_code == 404

    pid = "LEGACY-" + uuid4().hex
    with Session(engine) as session, session.begin():
        source = session.get(PolicyUpload, uid)
        session.add(PolicyVersion(
            policy_version_id=pid, policy_key=pid, version="legacy-v1", title="合成历史发布",
            status="ACTIVE", authority_level=100, legal_entity="DEMO-CN", category="HOTEL",
            city="深圳", effective_from=date(2026, 1, 1), effective_to=date(2027, 1, 1),
            published_at=datetime(2026, 10, 10, tzinfo=UTC),
            authoritative_rules={"upload_id": uid, "file_sha256": source.sha256},
            source_uri="fixture://legacy", content_sha256=source.sha256))
    # A version without a publication event is not enough.
    assert client.get(BASE).json()["items"] == []
    with Session(engine) as session, session.begin():
        session.add(PolicyPublicationEvent(
            event_id=str(uuid4()), idempotency_key=str(uuid4()), request_sha256="b" * 64,
            kind="PUBLISH", policy_version_id=pid, upload_id=uid,
            effective_on=date(2026, 1, 1), recorded_at=datetime(2026, 10, 10, tzinfo=UTC),
            actor="synthetic-admin", summary={}, result={}))
    before = pg_snapshot(engine), list(store.db.iterdump())
    assert [v["upload_id"] for v in client.get(BASE).json()["items"]] == [uid]
    assert client.get(f"{BASE}/{uid}/original").status_code == 200
    assert (pg_snapshot(engine), list(store.db.iterdump())) == before
    # An explicit different owner vetoes the inferred published-source access.
    with store.lock, store.db:
        store.db.execute(
            "INSERT INTO enterprise_resources(kind,id,company_id,owner_id,secret) "
            "VALUES('policy',?,'DEMO-B','synthetic-other-admin',NULL)", (uid,))
    assert client.get(BASE).json()["items"] == []
    assert client.get(f"{BASE}/{uid}/original").status_code == 404


def test_travel_publication_and_retirement_are_not_shown_as_draft(portal_policy):
    client, _, _ = portal_policy
    uid, base, body = prepare_travel(client)
    response = client.post(base + "/publish", json=body)
    assert response.status_code == 200, response.text
    publisher = client.app.state.publication_app.state.store
    publisher.clock = lambda: datetime(2026, 11, 2, tzinfo=UTC)
    item = next(v for v in client.get(BASE).json()["items"] if v["upload_id"] == uid)
    assert item["status"] == "EFFECTIVE"
    retired = client.post(base + "/retire", json=dict(
        expected_revision=1, effective_on="2026-12-01", reason="合成退休验证",
        idempotency_key=str(uuid4())))
    assert retired.status_code == 200, retired.text
    publisher.clock = lambda: datetime(2026, 12, 1, tzinfo=UTC)
    item = next(v for v in client.get(BASE).json()["items"] if v["upload_id"] == uid)
    assert item["status"] == "RETIRED"
