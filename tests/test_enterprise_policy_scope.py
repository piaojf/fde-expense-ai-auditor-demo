"""Broad document drafts must never broaden executable hotel rules."""

import os
from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_policies import META, TOKEN
from test_policies_postgres import policy_pg as policy_pg

from apps.api.config import Settings
from apps.api.main import create_app
from apps.api.repository import MockClaimRepository
from database.models import PolicyRuleReview, PolicyVersion
from domain.enterprise import CreateUser
from domain.policies import ConfirmPolicy, PreviewPolicy
from rag.retrieval.pgvector import PgPolicyCatalog
from tests.test_enterprise import PASSWORD, login

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="Disposable PostgreSQL required"),
]

DOCUMENT = {
    **META,
    "version": "2026-11-800",
    "effective_from": "2026-11-01",
    "region": "*",
    "expense_type": "ALL",
}


@pytest.fixture
def portal_policy(policy_pg, tmp_path):
    engine, draft, embedding, url = policy_pg
    repo = MockClaimRepository(store_path=str(tmp_path / "scope.sqlite3"))
    app = create_app(
        Settings(
            app_env="test",
            enterprise_enabled=True,
            enterprise_cookie_secure=False,
            database_url=url.render_as_string(hide_password=False),
            policy_retriever="pgvector",
            embedding_provider="deterministic",
            policy_demo_enabled=True,
            policy_demo_token=TOKEN,
            policy_draft_embedding="deterministic",
            _env_file=None,
        ),
        repo,
        policy_store=draft,
    )
    app.state.publication_app.state.store.clock = lambda: datetime(2026, 10, 11, tzinfo=UTC)
    store = app.state.enterprise_app.state.store
    for company, name, role in [
        ("DEMO-CN", "admin", "ADMIN"),
        ("DEMO-CN", "employee", "EMPLOYEE"),
        ("DEMO-B", "admin", "ADMIN"),
    ]:
        store.create_user(
            company,
            CreateUser(
                username=name,
                password=PASSWORD,
                name="合成测试人员",
                department="管理部",
                position="专员",
                role=role,
            ),
        )
    with TestClient(app) as client:
        login(client, "admin")
        yield client, engine, draft
    repo.close()


def upload(client, raw=None):
    result = client.post(
        "/api/v1.5/policies/upload",
        files={
            "file": (
                "scope.docx",
                raw or Path("tests/fixtures/policies/publication-800.docx").read_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert result.status_code == 200, result.text
    return result.json()["upload_id"]


def confirm_document(client, uid, metadata=DOCUMENT):
    result = client.post(
        f"/api/v1.5/policies/{uid}/confirm", json={"confirmed": True, "metadata": metadata}
    )
    assert result.status_code == 200, result.text
    return result.json()


def confirm_rule(client, uid):
    base = f"/api/v1.5/publication/policies/{uid}"
    candidate = client.post(base + "/rules", json={}).json()["extraction"]["candidates"][0]
    return client.post(
        base + "/confirm-rule",
        json={
            "actor": "forged-actor",
            "reason": "合成原文范围核对",
            "confirmed": True,
            "amount": candidate["amount"],
            "clause_start": candidate["start"],
            "clause_end": candidate["end"],
            "expected_revision": 0,
        },
    )


def test_all_document_persists_retrieves_and_publishes_only_source_hotel_scope(portal_policy):
    client, engine, _ = portal_policy
    uid = upload(client)
    row = confirm_document(client, uid)
    assert row["metadata"]["region"] == "*" and row["metadata"]["expense_type"] == "ALL"
    base = f"/api/v1.5/policies/{uid}"
    assert client.post(base + "/ingest", json={}).status_code == 200
    preview_body = {
        "query": "深圳住宿限额",
        "incurred_on": "2026-11-02",
        **{k: DOCUMENT[k] for k in ("legal_entity", "region", "expense_type", "version")},
    }
    preview = client.post(base + "/preview", json=preview_body)
    assert preview.status_code == 200 and preview.json()["results"], preview.text
    assert (
        client.post(base + "/preview", json={**preview_body, "region": "上海"}).status_code == 409
    )
    assert (
        client.post(
            base + "/confirm", json={"confirmed": True, "metadata": {**DOCUMENT, "region": "深圳"}}
        ).status_code
        == 409
    )
    review = confirm_rule(client, uid)
    assert review.status_code == 200, review.text
    assert review.json()["confirmed_rule"]["rule_scope"] == {
        "region": "深圳",
        "expense_type": "HOTEL",
    }
    actor = client.get("/api/v1.5/auth/me").json()["user"]["id"]
    assert review.json()["actor"] == actor
    release_base = f"/api/v1.5/publication/policies/{uid}"
    view = client.post(
        release_base + "/publication-preview", json={"predecessor_id": "POL-SZ-2026"}
    ).json()
    assert not view["blockers"], view
    assert view["new"]["region"] == "深圳" and view["new"]["expense_type"] == "HOTEL"
    body = {
        "confirmed": True,
        "predecessor_id": "POL-SZ-2026",
        "preview_digest": view["preview_digest"],
        "idempotency_key": str(uuid4()),
    }
    published = client.post(release_base + "/publish", json=body)
    assert published.status_code == 200, published.text
    assert client.post(release_base + "/publish", json=body).json() == published.json()
    pid = published.json()["policy"]["policy_version_id"]
    with Session(engine) as session:
        draft = session.get(PolicyVersion, row["policy_version_id"])
        assert (draft.status, draft.authority_level, draft.city, draft.category) == (
            "DRAFT",
            0,
            "*",
            "ALL",
        )
        actual = session.get(PolicyVersion, pid)
        assert actual.city == "深圳" and actual.category == "HOTEL"
    catalog = PgPolicyCatalog(engine)

    def resolve(city, category):
        return catalog.resolve(
            legal_entity="DEMO-CN",
            city=city,
            category=category,
            incurred_on=date(2026, 11, 2),
            audit_started_at=datetime(2026, 10, 12, tzinfo=UTC),
        )

    assert resolve("深圳", "HOTEL").hotel_limit == Decimal("800.00")
    for city, category in [("上海", "HOTEL"), ("深圳", "MEAL")]:
        other = resolve(city, category)
        assert other is None or other.document_id != pid
    login(client, "admin", "DEMO-B")
    assert client.post(base + "/read", json={}).status_code == 404
    assert client.post(release_base + "/publication-preview", json={}).status_code == 404


@pytest.mark.parametrize(
    "region,expense", [("UNKNOWN", "HOTEL"), ("*", "HOTEL"), ("深圳", "UNKNOWN"), ("深圳", "MEAL")]
)
def test_all_document_cannot_invent_rule_scope(portal_policy, region, expense):
    client, engine, _ = portal_policy
    buffer = BytesIO()
    with (
        ZipFile("tests/fixtures/policies/publication-800.docx") as source,
        ZipFile(buffer, "w") as target,
    ):
        for entry in source.infolist():
            content = source.read(entry)
            if entry.filename == "word/document.xml":
                content = (
                    content.decode("utf-8")
                    .replace("Region: 深圳", f"Region: {region}")
                    .replace("Expense Type: HOTEL", f"Expense Type: {expense}")
                    .encode("utf-8")
                )
            target.writestr(entry, content)
    raw = buffer.getvalue()
    uid = upload(client, raw)
    confirm_document(client, uid)
    result = confirm_rule(client, uid)
    assert (
        result.status_code == 409 and result.json()["error"]["code"] == "RULE_SCOPE_SOURCE_REQUIRED"
    )
    with Session(engine) as session:
        assert (
            session.scalar(select(PolicyRuleReview).where(PolicyRuleReview.upload_id == uid))
            is None
        )


def test_scope_validation_company_and_legacy_contracts(portal_policy):
    client, _, _ = portal_policy
    uid = upload(client)
    base = f"/api/v1.5/policies/{uid}/confirm"
    assert (
        client.post(
            base, json={"confirmed": True, "metadata": {**DOCUMENT, "legal_entity": "DEMO-B"}}
        ).status_code
        == 403
    )
    for update in ({"expense_type": "INVALID"}, {"region": "UNKNOWN"}):
        assert (
            client.post(
                base, json={"confirmed": True, "metadata": {**DOCUMENT, **update}}
            ).status_code
            == 422
        )
    login(client, "employee")
    assert client.post(base, json={"confirmed": True, "metadata": DOCUMENT}).status_code == 403
    with pytest.raises(ValidationError):
        ConfirmPolicy(token=TOKEN, confirmed=True, metadata=DOCUMENT)
    with pytest.raises(ValidationError):
        PreviewPolicy(
            token=TOKEN,
            query="test",
            incurred_on="2026-11-02",
            **{k: DOCUMENT[k] for k in ("legal_entity", "region", "expense_type", "version")},
        )
