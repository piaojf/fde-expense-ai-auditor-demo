"""Real PostgreSQL/pgvector governance with offline test vectors, isolated schemas."""

import asyncio
import hashlib
import os
from datetime import date
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from test_policies import FIXTURES, META, TOKEN, amend_docx

from apps.api.config import Settings
from apps.api.errors import APIError
from apps.api.main import create_app
from apps.api.policy_store import PolicyDraftStore
from apps.api.repository import MockClaimRepository
from domain.policies import ConfirmPolicy, PreviewPolicy
from integrations.ocr.local import LocalOCRProvider
from integrations.policies.parser import PolicyParser
from rag.embeddings.providers import DeterministicTestEmbedding
from rag.ingestion.postgres import PostgresPolicyIngestor, load_seed
from rag.retrieval.pgvector import PgPolicyCatalog, PgVectorPolicyRetriever

URL = os.getenv("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL, reason="Disposable PostgreSQL gate not configured"),
]


@pytest.fixture
def policy_pg():
    # Modules importing this fixture do not inherit this file's pytestmark;
    # guard here so a missing gate is a skip everywhere, never an error.
    if not URL:
        pytest.skip("Disposable PostgreSQL gate not configured")
    url = make_url(URL)
    assert (url.database or "").startswith("fde_m0_test_")
    schema = "ui_p3a_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = url.update_query_dict({"options": f"-csearch_path={schema},public"})
    engine = create_engine(scoped)
    try:
        with engine.begin() as c:
            # Keep revision bookkeeping inside the fixture schema; do not inherit
            # a public alembic_version table from another regression harness.
            c.execute(
                text(
                    f'CREATE TABLE "{schema}".alembic_version '
                    "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                )
            )
            config = Config("alembic.ini")
            config.attributes["connection"] = c
            command.upgrade(config, "head")
        embedding = DeterministicTestEmbedding()
        PostgresPolicyIngestor(engine, embedding).ingest(load_seed())
        store = PolicyDraftStore(engine, PolicyParser(LocalOCRProvider()), lambda a: embedding)
        yield engine, store, embedding, scoped
    finally:
        engine.dispose()
        with admin.begin() as c:
            c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def upload_confirm(store, *, raw=None, metadata=None):
    row = store.upload(raw or (FIXTURES / "draft-800.docx").read_bytes(), "policy.docx")
    store.confirm(
        row["upload_id"], ConfirmPolicy(token=TOKEN, confirmed=True, metadata=metadata or META)
    )
    return row


def preview(metadata=None, **changes):
    metadata = metadata or META
    return PreviewPolicy(
        token=TOKEN,
        query="2026 深圳住宿 800",
        legal_entity=metadata["legal_entity"],
        region=metadata["region"],
        expense_type=metadata["expense_type"],
        version=metadata["version"],
        incurred_on="2026-09-20",
        **changes,
    )


def active_snapshot(engine):
    with engine.connect() as c:
        return c.execute(
            text(
                "SELECT row_to_json(p)::text FROM policy_versions p WHERE "
                "status='ACTIVE' ORDER BY policy_version_id"
            )
        ).scalars().all(), c.execute(
            text(
                "SELECT row_to_json(c)::text FROM policy_chunks c JOIN policy_versions p "
                "USING(policy_version_id) WHERE p.status='ACTIVE' ORDER BY chunk_id"
            ),
        ).scalars().all()


def test_800_draft_real_sql_and_700_authority_780_hard_block(policy_pg):
    engine, store, embedding, url = policy_pg
    before = active_snapshot(engine)
    row = upload_confirm(store)
    uid, pid = row["upload_id"], row["policy_version_id"]
    assert row["state"] == "PARSED" and row["status"] == "DRAFT"
    assert store.ingest(uid)["state"] == "INDEXED"
    statements = []

    def capture(c, cursor, sql, p, ctx, many):
        statements.append(sql)

    event.listen(engine, "before_cursor_execute", capture)
    result = store.preview(uid, preview())
    assert result["model"] == "deterministic-test-only-v1"
    assert any("800" in r["quote"] for r in result["results"])
    assert all(
        r["policy_version_id"] == pid and r["source"]["upload_id"] == uid for r in result["results"]
    )
    assert any("<=>" in sql and "policy_versions.status" in sql for sql in statements)
    with engine.connect() as c:
        assert (
            c.scalar(
                text(
                    "SELECT authoritative_rules FROM policy_versions WHERE policy_version_id=:pid"
                ),
                dict(pid=pid),
            )
            == {}
        )
        assert (
            c.scalar(
                text(
                    "SELECT count(*) FROM policy_versions WHERE status='DRAFT' AND "
                    "policy_version_id=:pid"
                ),
                dict(pid=pid),
            )
            == 1
        )
    catalog = PgPolicyCatalog(engine)
    selected = catalog.resolve(
        legal_entity="DEMO-CN", category="HOTEL", city="深圳", incurred_on=date(2026, 9, 20)
    )
    assert selected.document_id == "POL-SZ-2026" and str(selected.hotel_limit) == "700.00"
    from datetime import UTC, datetime

    evidence = asyncio.run(
        PgVectorPolicyRetriever(engine, embedding).retrieve(
            "2026 深圳住宿 800",
            legal_entity="DEMO-CN",
            category="HOTEL",
            city="深圳",
            incurred_on=date(2026, 9, 20),
            audit_started_at=datetime.now(UTC),
            authoritative_version_id="POL-SZ-2026",
        )
    )
    assert evidence and all(
        e.policy_version_id == "POL-SZ-2026" and "800" not in e.quote for e in evidence
    )
    settings = Settings(
        app_env="test",
        policy_retriever="pgvector",
        embedding_provider="deterministic",
        database_url=url.render_as_string(hide_password=False),
        _env_file=None,
    )
    repo = MockClaimRepository()
    try:
        with TestClient(create_app(settings, repo)) as client:
            body = dict(
                applicant_name="UI-P3A synthetic 780",
                department="Demo",
                position="Demo",
                category="HOTEL",
                city="深圳",
                amount="780.00",
                nights=1,
                incurred_on="2026-09-20",
                purpose="制度草稿隔离治理测试",
                trip_start="2026-09-20",
                trip_end="2026-09-21",
                seller="Synthetic Hotel",
                idempotency_key="ui-p3a-governance",
            )
            response = client.post("/api/v1.1/claims", json=body)
            assert response.status_code == 202, response.text
            result = response.json()
            claim = result["claim"]
            decision = claim["decision_result"]
            assert decision["decision"] == "REJECT" and decision["risk_level"] == "HIGH"
            assert decision["rule_results"][0]["limit"] == "700.00"
            assert decision["rule_results"][0]["actual"] == "780.00"
            cid = claim["claim_id"]
            assert repo.erp_write_count(cid) == 0
            op = result["operation"]
            override = client.post(
                f"/claims/{cid}/human-review",
                json=dict(
                    run_id=op["run_id"],
                    expected_revision=op["revision"],
                    decision="APPROVE",
                    reviewer_id="finance_reviewer_01",
                    comment="attempted 800 draft override",
                    idempotency_key="ui-p3a-no-override",
                ),
            )
            assert override.status_code == 409
    finally:
        repo.close()
    store.delete(uid)
    assert store.read(uid)["state"] == "DELETED"
    assert active_snapshot(engine) == before
    assert (
        PgPolicyCatalog(engine)
        .resolve(
            legal_entity="DEMO-CN", category="HOTEL", city="深圳", incurred_on=date(2026, 9, 20)
        )
        .hotel_limit
        == 700
    )


def test_idempotence_sources_and_restart_no_reembedding(policy_pg):
    engine, store, embedding, _ = policy_pg
    row = upload_confirm(store)
    uid = row["upload_id"]
    raw = (FIXTURES / "draft-800.docx").read_bytes()
    assert store.upload(raw, "policy.docx")["upload_id"] == uid
    store.ingest(uid)
    before = store.read(uid)
    source = before["clauses"][0]["source"]
    assert source["file_sha256"] == hashlib.sha256(raw).hexdigest()
    for clause in before["clauses"]:
        assert (
            before["parsed"]["text"][clause["source"]["start"] : clause["source"]["end"]]
            == clause["quote"]
        )

    def no_provider(*a):
        pytest.fail("Indexed replay must not invoke Embedding")

    restarted = PolicyDraftStore(engine, None, no_provider)
    assert restarted.ingest(uid) == store.ingest(uid)
    assert restarted.read(uid) == before
    assert restarted.original(uid)[0] == raw
    restarted.delete(uid)
    assert restarted.delete(uid)["state"] == "DELETED"
    assert not restarted.read(uid)["parsed"]
    with pytest.raises(APIError):
        restarted.original(uid)
    replacement = store.upload(raw, "policy.docx")
    assert replacement["upload_id"] != uid


def test_failure_rolls_back_and_retry_delete_preserves_active(policy_pg):
    engine, store, embedding, _ = policy_pg
    before = active_snapshot(engine)
    row = upload_confirm(store)
    from database.models import PolicyChunk

    fired = []

    def fail(mapper, connection, target):
        fired.append(target.chunk_id)
        raise RuntimeError("injected chunk insertion failure after version flush")

    event.listen(PolicyChunk, "before_insert", fail)
    try:
        with pytest.raises(APIError) as error:
            store.ingest(row["upload_id"])
        assert error.value.code == "DRAFT_IMPORT_FAILED" and fired
    finally:
        event.remove(PolicyChunk, "before_insert", fail)
    assert store.read(row["upload_id"])["state"] == "FAILED"
    with engine.connect() as c:
        assert (
            c.scalar(
                text("SELECT count(*) FROM policy_versions WHERE policy_version_id=:p"),
                dict(p=row["policy_version_id"]),
            )
            == 0
        )
        assert (
            c.scalar(
                text("SELECT count(*) FROM policy_chunks WHERE policy_version_id=:p"),
                dict(p=row["policy_version_id"]),
            )
            == 0
        )
    assert active_snapshot(engine) == before
    assert store.ingest(row["upload_id"])["state"] == "INDEXED"
    store.delete(row["upload_id"])
    assert active_snapshot(engine) == before


@pytest.mark.parametrize("fault", ["dimension", "zero", "nan", "count", "query-model"])
def test_embedding_validation_fail_closed(policy_pg, fault):
    engine, store, embedding, _ = policy_pg
    row = upload_confirm(store)
    if fault == "query-model":
        store.ingest(row["upload_id"])
        embedding.model_name = "other-model"
        with pytest.raises(APIError) as error:
            store.preview(row["upload_id"], preview())
        assert error.value.code == "EMBEDDING_MISMATCH"
    else:
        if fault == "dimension":
            embedding.dimension = 3
        elif fault == "zero":
            embedding.embed_documents = lambda texts: [[0.0] * 1024 for _ in texts]
        elif fault == "nan":
            embedding.embed_documents = lambda texts: [[float("nan")] * 1024 for _ in texts]
        elif fault == "count":
            embedding.embed_documents = lambda texts: []
        with pytest.raises(APIError):
            store.ingest(row["upload_id"])
        with engine.connect() as c:
            assert (
                c.scalar(
                    text("SELECT count(*) FROM policy_versions WHERE policy_version_id=:p"),
                    dict(p=row["policy_version_id"]),
                )
                == 0
            )


def test_same_title_distinct_company_version_and_date_are_isolated(policy_pg):
    _, store, _, _ = policy_pg
    a = upload_confirm(store)
    with zipfile_data() as original:
        doc = original.read("word/document.xml")
    b_raw = amend_docx(
        "word/document.xml",
        doc.replace(b"DEMO-CN", b"OTHER-CN").replace(b"800 per night", b"999 per night"),
    )
    b_meta = META | dict(legal_entity="OTHER-CN")
    b = upload_confirm(store, raw=b_raw, metadata=b_meta)
    c_raw = amend_docx(
        "word/document.xml",
        doc.replace(b"2026-draft-800", b"2027-next").replace(b"800 per night", b"888 per night"),
    )
    c_meta = META | dict(
        version="2027-next", effective_from="2027-01-01", effective_to="2028-01-01"
    )
    c = upload_confirm(store, raw=c_raw, metadata=c_meta)
    for row in (a, b, c):
        store.ingest(row["upload_id"])
    result = store.preview(a["upload_id"], preview())
    assert all("999" not in r["quote"] and "888" not in r["quote"] for r in result["results"])
    assert "999" in store.preview(b["upload_id"], preview(b_meta))["results"][0]["quote"]
    for change in [
        dict(legal_entity="OTHER-CN"),
        dict(version="2027-next"),
        dict(region="北京"),
        dict(expense_type="MEAL"),
        dict(incurred_on="2027-01-01"),
    ]:
        body = preview().model_copy(update=change)
        if isinstance(body.incurred_on, str):
            body.incurred_on = date.fromisoformat(body.incurred_on)
        with pytest.raises(APIError):
            store.preview(a["upload_id"], body)
    with pytest.raises(APIError) as error:
        store.confirm(a["upload_id"], ConfirmPolicy(token=TOKEN, confirmed=True, metadata=b_meta))
    assert error.value.code == "METADATA_FROZEN"
    with pytest.raises(APIError):
        store.preview("00000000-0000-0000-0000-000000000000", preview())


def zipfile_data():
    import zipfile

    return zipfile.ZipFile(FIXTURES / "draft-800.docx")


def test_active_delete_and_preview_forbidden(policy_pg):
    engine, store, _, _ = policy_pg
    before = active_snapshot(engine)
    row = upload_confirm(store)
    store.ingest(row["upload_id"])
    from database.models import PolicyVersion

    with Session(engine) as session, session.begin():
        p = session.get(PolicyVersion, row["policy_version_id"])
        p.status = "ACTIVE"  # Simulate future publish/corrupt lifecycle; P3A has no publish route.
    with pytest.raises(APIError):
        store.delete(row["upload_id"])
    with pytest.raises(APIError):
        store.preview(row["upload_id"], preview())
    after = active_snapshot(engine)
    assert set(before[0]).issubset(after[0]) and before[1] == after[1][:0] + [
        row for row in after[1] if row in before[1]
    ]
    with engine.connect() as c:
        assert (
            c.scalar(
                text(
                    "SELECT authoritative_rules->>'hotel_limit_per_night' FROM "
                    "policy_versions WHERE policy_version_id='POL-SZ-2026'"
                )
            )
            == "700.00"
        )


def test_remote_requires_both_server_and_document_authorization(policy_pg, monkeypatch):
    engine, _, _, url = policy_pg
    calls = []
    monkeypatch.setattr(
        "apps.api.policies.RemoteEmbeddingProvider",
        lambda **kw: calls.append(kw) or pytest.fail("Unapproved remote"),
    )
    settings = Settings(
        app_env="test",
        policy_demo_enabled=True,
        policy_demo_token=TOKEN,
        policy_draft_embedding="remote",
        policy_remote_enabled=False,
        database_url=url.render_as_string(hide_password=False),
        _env_file=None,
    )
    with TestClient(
        create_app(settings), base_url="http://127.0.0.1", client=("127.0.0.1", 10000)
    ) as client:
        auth = dict(token=TOKEN)
        row = client.post(
            "/api/v1.3/policies/upload",
            data=auth,
            files={"file": ("policy.docx", (FIXTURES / "draft-800.docx").read_bytes())},
        )
        assert row.status_code == 200
        uid = row.json()["upload_id"]
        assert (
            client.post(
                f"/api/v1.3/policies/{uid}/confirm", json=auth | dict(metadata=META, confirmed=True)
            ).status_code
            == 200
        )
        for authorized in (False, True):
            r = client.post(
                f"/api/v1.3/policies/{uid}/ingest", json=auth | dict(remote_authorized=authorized)
            )
            assert r.status_code == 403 and r.json()["error"]["code"] == "REMOTE_CONSENT_REQUIRED"
        assert calls == []


def test_concurrent_upload_ingest_and_delete_do_not_duplicate_or_damage_active(policy_pg):
    from concurrent.futures import ThreadPoolExecutor

    engine, store, embedding, _ = policy_pg
    before = active_snapshot(engine)
    raw = (FIXTURES / "draft-800.docx").read_bytes()
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda _: store.upload(raw, "../../恶意路径/policy.docx"), range(2)))
    assert rows[0]["upload_id"] == rows[1]["upload_id"]
    assert rows[0]["filename"] == "policy.docx"
    uid = rows[0]["upload_id"]
    store.confirm(uid, ConfirmPolicy(token=TOKEN, confirmed=True, metadata=META))
    calls = []
    original = embedding.embed_documents

    def count(texts):
        calls.append(len(texts))
        return original(texts)

    embedding.embed_documents = count
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda _: store.ingest(uid), range(2)))
    assert all(r["state"] == "INDEXED" for r in result) and calls == [1]
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda _: store.delete(uid), range(2)))
    assert all(r["state"] == "DELETED" for r in result)
    assert active_snapshot(engine) == before


@pytest.mark.parametrize(
    "enabled,budget,authorization",
    [
        (False, 11, True),
        (True, 0, True),
        (True, 11, False),
    ],
)
def test_no_remote_construction_when_any_consent_gate_missing(
    policy_pg, monkeypatch, enabled, budget, authorization
):
    _, _, _, url = policy_pg
    monkeypatch.setattr(
        "apps.api.policies.RemoteEmbeddingProvider",
        lambda **kw: pytest.fail("Unapproved provider construction"),
    )
    settings = Settings(
        app_env="test",
        policy_demo_enabled=True,
        policy_demo_token=TOKEN,
        policy_draft_embedding="remote",
        policy_remote_enabled=enabled,
        policy_remote_max_requests=budget,
        database_url=url.render_as_string(hide_password=False),
        _env_file=None,
    )
    with TestClient(
        create_app(settings), base_url="http://127.0.0.1", client=("127.0.0.1", 10000)
    ) as client:
        store = client.app.state.policy_app.state.store
        row = upload_confirm(store)
        assert (
            client.post(
                f"/api/v1.3/policies/{row['upload_id']}/ingest",
                json=dict(token=TOKEN, remote_authorized=authorization),
            ).status_code
            == 403
        )
        assert store.read(row["upload_id"])["state"] == "CONFIRMED"


def test_duplicate_spoof_does_not_bypass_type_checks_and_tampering_blocks_ingest(policy_pg):
    engine, store, _, _ = policy_pg
    row = upload_confirm(store)
    raw = (FIXTURES / "draft-800.docx").read_bytes()
    with pytest.raises(APIError):
        store.upload(raw, "disguised.pdf")
    from database.models import PolicyUpload

    with Session(engine) as session, session.begin():
        upload = session.get(PolicyUpload, row["upload_id"])
        upload.original = b"tampered"
    for operation in (
        lambda: store.original(row["upload_id"]),
        lambda: store.ingest(row["upload_id"]),
    ):
        with pytest.raises(APIError) as error:
            operation()
        assert error.value.code == "FILE_INTEGRITY"
    with engine.connect() as c:
        assert (
            c.scalar(
                text("SELECT count(*) FROM policy_versions WHERE policy_version_id=:p"),
                dict(p=row["policy_version_id"]),
            )
            == 0
        )
