"""Broad document intake releases only its source-verified hotel matrix."""

import hashlib
import os
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_policies_postgres import policy_pg as policy_pg

from apps.api.errors import APIError
from apps.api.hotel_adapter import HotelCatalog
from apps.api.hotel_facts import HotelFacts
from apps.api.hotel_publication import HotelPublication
from apps.api.main import create_app
from apps.api.repository import MockClaimRepository
from database.models import (
    PolicyChunk,
    PolicyPublicationEvent,
    PolicyRuleReview,
    PolicyUpload,
    PolicyVersion,
)
from domain.enterprise import CreateUser
from domain.enterprise_policies import EnterpriseConfirmPolicy
from domain.hotel_matrix import MatrixConfirm, MatrixPreview
from rag.retrieval.pgvector import PgPolicyCatalog, PgVectorPolicyRetriever
from scripts.p0_runtime import OfflineV4, local_settings
from tests.test_enterprise import PASSWORD, login
from tests.test_p4a_matrix import SOURCE, edit, fields

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="Disposable PostgreSQL required"),
]


@pytest.mark.parametrize("expense_type", ["ALL", "HOTEL"])
@pytest.mark.parametrize("region", ["*", "深圳"])
def test_matrix_release_keeps_document_scope_and_limits_execution_to_hotel(
    policy_pg, tmp_path, monkeypatch, expense_type, region,
):
    engine, draft, embedding, _ = policy_pg
    repo = MockClaimRepository(store_path=str(tmp_path / "matrix.sqlite3"))
    app = create_app(
        local_settings(str(tmp_path)), repo, policy_store=draft,
        catalog=PgPolicyCatalog(engine), retriever=PgVectorPolicyRetriever(engine, embedding),
        compliance_provider=OfflineV4(),
    )
    store = app.state.enterprise_app.state.store
    for company, username, role in [
        ("DEMO-CN", "admin", "ADMIN"), ("DEMO-CN", "finance", "FINANCE"),
        ("DEMO-CN", "employee", "EMPLOYEE"), ("DEMO-B", "admin", "ADMIN"),
    ]:
        store.create_user(company, CreateUser(
            username=username, password=PASSWORD, name="合成人员", department="测试部",
            position="测试员", role=role,
        ))
    monkeypatch.setattr(HotelPublication, "today", lambda _: date(2026, 10, 22))
    try:
        with TestClient(app) as client:
            assert login(client, "admin").status_code == 200
            raw = SOURCE.read_bytes()
            uploaded = client.post("/api/v1.5/policies/upload", files={
                "file": ("comprehensive.docx", raw,
                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            })
            assert uploaded.status_code == 200, uploaded.text
            row = uploaded.json()
            uid = row["upload_id"]
            meta = {k: v["value"] for k, v in row["parsed"]["extracted_metadata"].items()}
            meta.update(expense_type=expense_type, region=region)
            intake = f"/api/v1.5/policies/{uid}"
            confirmed = client.post(intake + "/confirm", json={
                "metadata": meta, "confirmed": True,
            })
            assert confirmed.status_code == 200, confirmed.text
            assert client.post(intake + "/ingest", json={}).status_code == 200
            base = f"/api/v1.5/p4a/v1/policies/{uid}"
            source = client.post(base + "/read", json={}).json()
            assert len(source["extraction"]["rules"]) == 16
            checked = client.post(base + "/confirm", json={
                "expected_revision": 0, "confirmed": True, "reason": "合成逐格核对",
                "extraction_digest": source["extraction_digest"],
            })
            assert checked.status_code == 200, checked.text
            assert checked.json()["confirmed_rule"]["metadata"] == meta
            predecessors = ["POL-SZ-2026", "POL-BEIJING-2026"]
            preview = client.post(base + "/preview", json={"predecessor_ids": predecessors})
            assert preview.status_code == 200, preview.text
            view = preview.json()
            assert not view["blockers"], view
            assert view["new"]["expense_type"] == "HOTEL"
            request = dict(
                predecessor_ids=predecessors, preview_digest=view["preview_digest"],
                confirmed=True, reason="合成住宿矩阵发布", idempotency_key=str(uuid4()),
            )
            published = client.post(base + "/publish", json=request)
            assert published.status_code == 200, published.text
            assert client.post(base + "/publish", json=request).json() == published.json()
            result = published.json()["policy"]
            assert result["expense_type"] == "HOTEL"
            assert len(result["rules"]["cells"]) == 16
            with Session(engine) as session:
                original = session.get(PolicyUpload, uid)
                assert original.confirmed_metadata == meta
                assert original.original == raw
                assert original.sha256 == hashlib.sha256(raw).hexdigest()
                indexed = session.get(PolicyVersion, row["policy_version_id"])
                assert (indexed.status, indexed.authority_level) == ("DRAFT", 0)
                assert (indexed.city, indexed.category) == (region, expense_type)
                active = session.get(PolicyVersion, result["policy_version_id"])
                assert (active.status, active.city, active.category) == ("ACTIVE", "*", "HOTEL")
                assert all(c.source_metadata["metadata"] == meta for c in session.scalars(
                    select(PolicyChunk).where(
                        PolicyChunk.policy_version_id == active.policy_version_id,
                    )
                ))
                catalog = HotelCatalog(PgPolicyCatalog(engine), HotelFacts(repo), engine)
                selected = catalog.policy(session, fields(), "DEMO-CN", datetime.now(UTC))
                assert selected.policy_version_id == active.policy_version_id
            for username in ("finance", "employee"):
                assert login(client, username).status_code == 200
                for action in ("read", "confirm", "preview", "publish"):
                    body = {"read": {}, "confirm": checked.json(), "preview": {},
                            "publish": request}[action]
                    if action == "confirm":
                        body = dict(expected_revision=1, confirmed=True, reason="合成越权",
                                    extraction_digest=source["extraction_digest"])
                    assert client.post(base + "/" + action, json=body).status_code == 403
            assert login(client, "admin", "DEMO-B").status_code == 200
            assert client.post(base + "/read", json={}).status_code == 404
    finally:
        repo.close()


def prepare_document(policy_pg, **changes):
    _, draft, embedding, _ = policy_pg
    row = draft.upload(SOURCE.read_bytes(), "comprehensive.docx")
    metadata = {k: v["value"] for k, v in row["parsed"]["extracted_metadata"].items()}
    metadata.update({"expense_type": "ALL", "region": "*", **changes})
    draft.confirm(row["upload_id"], EnterpriseConfirmPolicy(
        token="synthetic-local-policy-test-capability-00000000", confirmed=True, metadata=metadata,
    ))
    draft.ingest(row["upload_id"])
    publisher = HotelPublication(draft, embedding, app_env="test", clock=lambda: datetime(
        2026, 10, 22, tzinfo=UTC,
    ))
    return publisher, row["upload_id"]


@pytest.mark.parametrize("expense_type", ["MEAL", "TRANSPORT", "OTHER"])
def test_document_excluding_hotels_cannot_confirm_matrix(policy_pg, expense_type):
    publisher, uid = prepare_document(policy_pg, expense_type=expense_type)
    source = publisher.read(uid)
    with pytest.raises(APIError) as exc:
        publisher.confirm_matrix(uid, MatrixConfirm(
            expected_revision=0, confirmed=True, reason="合成范围校验",
            extraction_digest=source["extraction_digest"],
        ), "admin")
    assert exc.value.code == "HOTEL_METADATA_REQUIRED"
    with Session(policy_pg[0]) as session:
        assert not session.scalars(select(PolicyRuleReview)).all()


@pytest.mark.parametrize("change", [
    {"effective_to": "2027-10-01"}, {"effective_from": "2026-11-02"},
    {"version": "different-version"}, {"legal_entity": "DEMO-B"}, {"region": "杭州"},
])
def test_broad_document_cannot_bypass_source_metadata_checks(policy_pg, change):
    publisher, uid = prepare_document(policy_pg, **change)
    source = publisher.read(uid)
    publisher.confirm_matrix(uid, MatrixConfirm(
        expected_revision=0, confirmed=True, reason="合成矩阵核对",
        extraction_digest=source["extraction_digest"],
    ), "admin")
    preview = publisher.preview(uid, MatrixPreview(
        predecessor_ids=["POL-SZ-2026", "POL-BEIJING-2026"],
    ))
    assert preview["blockers"] == ["METADATA_SOURCE_MISMATCH"]
    with Session(policy_pg[0]) as session:
        assert not session.scalars(select(PolicyPublicationEvent)).all()


def test_comprehensive_document_still_requires_complete_source_matrix(policy_pg):
    _, draft, embedding, _ = policy_pg
    row = draft.upload(edit("1,200", "12,00"), "incomplete.docx")
    metadata = {k: v["value"] for k, v in row["parsed"]["extracted_metadata"].items()}
    metadata.update(expense_type="ALL", region="*")
    draft.confirm(row["upload_id"], EnterpriseConfirmPolicy(
        token="synthetic-local-policy-test-capability-00000000", confirmed=True, metadata=metadata,
    ))
    publisher = HotelPublication(draft, embedding, app_env="test")
    with pytest.raises(APIError) as exc:
        publisher.read(row["upload_id"])
    assert exc.value.code == "MATRIX_EXTRACTION_BLOCKED"
