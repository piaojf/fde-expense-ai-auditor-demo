"""Genuine policy upload/review/publication through role/tenant guarded endpoints."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_policies import META, TOKEN
from test_policies_postgres import policy_pg as policy_pg

from apps.api.config import Settings
from apps.api.main import create_app
from apps.api.repository import MockClaimRepository
from domain.enterprise import CreateUser
from tests.test_enterprise import PASSWORD, login

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("TEST_DATABASE_URL"), reason="Disposable PostgreSQL gate not configured"
    ),
]


def test_policy_company_ownership_actual_publication_actor_and_history(policy_pg, tmp_path):
    engine, draft, embedding, url = policy_pg
    repo = MockClaimRepository(store_path=str(tmp_path / "enterprise.sqlite3"))
    settings = Settings(
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
    )
    app = create_app(settings, repo, policy_store=draft)
    store = app.state.enterprise_app.state.store
    for company in ("DEMO-CN", "DEMO-B"):
        for username, role in (
            ("admin", "ADMIN"),
            ("finance", "FINANCE"),
            ("employee", "EMPLOYEE"),
        ):
            store.create_user(
                company,
                CreateUser(
                    username=username,
                    password=PASSWORD,
                    name="合成制度管理员",
                    department="管理部",
                    position="专员",
                    role=role,
                ),
            )
    with TestClient(app) as c:
        login(c, "employee")
        files = {
            "file": (
                "enterprise-policy.docx",
                Path("tests/fixtures/policies/publication-800.docx").read_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        }
        assert c.post("/api/v1.5/policies/upload", files=files).status_code == 403
        login(c, "admin")
        upload = c.post("/api/v1.5/policies/upload", files=files)
        assert upload.status_code == 200, upload.text
        uid = upload.json()["upload_id"]
        assert TOKEN not in upload.text
        assert len(c.post("/api/v1.5/policies/list", json={}).json()["items"]) == 1
        login(c, "admin", "DEMO-B")
        assert c.post("/api/v1.5/policies/list", json={}).json()["items"] == []
        for action in ("read", "original", "delete", "confirm", "ingest"):
            assert c.post(f"/api/v1.5/policies/{uid}/{action}", json={}).status_code == 404
        assert c.post(f"/api/v1.5/publication/policies/{uid}/rules", json={}).status_code == 404
        assert (
            c.post(
                "/api/v1.5/publication/policies/POL-SZ-2026/lifecycle-preview",
                json=dict(kind="RETIRE", effective_on="2026-12-01"),
            ).status_code
            == 404
        )
        assert c.post("/api/v1.3/policies/list", json={"token": TOKEN}).status_code == 403
        login(c, "admin")
        metadata = {**META, "version": "2026-11-800", "effective_from": "2026-11-01"}
        assert (
            c.post(
                f"/api/v1.5/policies/{uid}/confirm",
                json={"confirmed": True, "metadata": {**metadata, "legal_entity": "DEMO-B"}},
            ).status_code
            == 403
        )
        result = c.post(
            f"/api/v1.5/policies/{uid}/confirm", json={"confirmed": True, "metadata": metadata}
        )
        assert result.status_code == 200, result.text
        assert (
            c.post(f"/api/v1.5/policies/{uid}/ingest", json={"remote_authorized": True}).status_code
            == 403
        )
        assert c.post(f"/api/v1.5/policies/{uid}/ingest", json={}).status_code == 200
        base = f"/api/v1.5/publication/policies/{uid}"
        candidate = c.post(base + "/rules", json={}).json()["extraction"]["candidates"][0]
        reviewed = c.post(
            base + "/confirm-rule",
            json={
                "actor": "伪造身份无效",
                "reason": "对照合成原文核对",
                "confirmed": True,
                "amount": candidate["amount"],
                "clause_start": candidate["start"],
                "clause_end": candidate["end"],
                "expected_revision": 0,
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        actor = c.get("/api/v1.5/auth/me").json()["user"]["id"]
        assert reviewed.json()["actor"] == actor
        view = c.post(base + "/publication-preview", json={"predecessor_id": "POL-SZ-2026"}).json()
        assert not view["blockers"], view
        body = {
            "actor": "伪造身份无效",
            "confirmed": True,
            "predecessor_id": "POL-SZ-2026",
            "preview_digest": view["preview_digest"],
            "idempotency_key": str(uuid4()),
        }
        published = c.post(base + "/publish", json=body)
        assert published.status_code == 200, published.text
        assert published.json()["actor"] == actor
        assert (
            c.post(base + "/publish", json=body).json()["event_id"] == published.json()["event_id"]
        )
        assert len(c.post("/api/v1.5/publication/policies/history", json={}).json()["events"]) == 1
        login(c, "admin", "DEMO-B")
        history = c.post("/api/v1.5/publication/policies/history", json={}).json()
        assert history["events"] == [] and history["versions"] == []
        assert c.post(base + "/publish", json=body).status_code == 404
        assert (
            c.post(
                f"/api/v1.5/publication/policies/{published.json()['policy']['policy_version_id']}/lifecycle-preview",
                json=dict(kind="RETIRE", effective_on="2026-12-01"),
            ).status_code
            == 404
        )
    repo.close()
