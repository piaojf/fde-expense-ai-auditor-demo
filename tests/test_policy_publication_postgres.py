"""Real transaction, pgvector and formal workflow tests; no paid provider calls."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_policies import META, TOKEN, amend_docx
from test_policies_postgres import policy_pg as policy_pg
from test_submission import payload

from apps.api.config import Settings
from apps.api.errors import APIError
from apps.api.main import create_app
from apps.api.policy_publication_store import PolicyPublicationStore
from apps.api.repository import MockClaimRepository
from database.models import PolicyPublicationEvent, PolicyVersion
from domain.policies import ConfirmPolicy
from domain.policy_publication import (
    ChangeLifecycle,
    LifecyclePreview,
    PublicationPreview,
    PublishPolicy,
    RuleConfirmation,
)
from domain.schemas import Expense, Outcome
from rag.retrieval.pgvector import PgPolicyCatalog, PgVectorPolicyRetriever
from rules.travel_rules import hotel_city_limit

FUTURE_META = {**META, "version": "2026-11-800", "effective_from": "2026-11-01"}
NOW = datetime(2026, 10, 9, 6, tzinfo=UTC)


@pytest.fixture
def governance(policy_pg):
    engine, draft, embedding, url = policy_pg
    store = PolicyPublicationStore(draft, embedding, app_env="test", clock=lambda: NOW)
    yield engine, draft, store, embedding, url


def prepare(g, *, raw=None, metadata=None, confirm=True):
    engine, draft, store, embedding, url = g
    row = draft.upload(
        raw or Path("tests/fixtures/policies/publication-800.docx").read_bytes(), "future.docx"
    )
    uid = row["upload_id"]
    draft.confirm(uid, ConfirmPolicy(token=TOKEN, confirmed=True, metadata=metadata or FUTURE_META))
    if confirm:
        approve_rule(store, uid)
    draft.ingest(uid)
    return uid


def approve_rule(store, uid, **updates):
    data = store.read(uid)
    c = data["extraction"]["candidates"][0]
    b = dict(
        token=TOKEN,
        actor="demo-admin",
        reason="对照合成原文核验",
        confirmed=True,
        amount=c["amount"],
        clause_start=c["start"],
        clause_end=c["end"],
        expected_revision=data["reviews"][-1]["revision"] if data["reviews"] else 0,
    )
    return store.confirm(uid, RuleConfirmation(**{**b, **updates}))


def publish_body(store, uid, **updates):
    view = store.preview(uid, PublicationPreview(token=TOKEN, predecessor_id="POL-SZ-2026"))
    assert not view["blockers"], view
    return PublishPolicy(
        **{
            **dict(
                token=TOKEN,
                predecessor_id="POL-SZ-2026",
                actor="demo-admin",
                confirmed=True,
                preview_digest=view["preview_digest"],
                idempotency_key="pub-" + str(uuid4()),
            ),
            **updates,
        }
    )


def publish(g, uid):
    store = g[2]
    return store.publish(uid, publish_body(store, uid))


def resolve(engine, when, *, stamp=None, company="DEMO-CN", category="HOTEL"):
    return PgPolicyCatalog(engine).resolve(
        legal_entity=company,
        category=category,
        city="深圳",
        incurred_on=date.fromisoformat(when),
        audit_started_at=stamp or NOW,
    )


def snapshots(engine):
    with engine.connect() as c:
        return {
            t: c.execute(text(f"SELECT row_to_json(t)::text FROM {t} t ORDER BY 1")).scalars().all()
            for t in ("policy_versions", "policy_chunks", "policy_publication_events")
        }


def test_a_b_c_d_rule_and_rag_same_version_and_old_snapshot(governance):
    engine, draft, store, embedding, url = governance
    before = snapshots(engine)
    uid = prepare(governance)
    assert resolve(engine, "2026-11-02").hotel_limit == Decimal("700.00")

    def exp(day, amount):
        return Expense(category="HOTEL", city="深圳", incurred_on=day, amount=amount, nights=1)

    assert (
        hotel_city_limit(exp("2026-11-02", "780"), resolve(engine, "2026-11-02")).outcome
        == Outcome.FAIL
    )
    body = publish_body(store, uid)
    result = store.publish(uid, body)
    pid = result["policy"]["policy_version_id"]
    assert result == store.publish(uid, body)
    for day, amount, limit, outcome in [
        ("2026-10-20", "780", "700", Outcome.FAIL),
        ("2026-11-02", "780", "800", Outcome.PASS),
        ("2026-11-02", "850", "800", Outcome.FAIL),
    ]:
        p = resolve(engine, day)
        rule = hotel_city_limit(exp(day, amount), p)
        assert rule.limit == Decimal(limit) and rule.outcome == outcome
        evidence = asyncio.run(
            PgVectorPolicyRetriever(engine, embedding).retrieve(
                "深圳住宿限额",
                legal_entity="DEMO-CN",
                category="HOTEL",
                city="深圳",
                incurred_on=date.fromisoformat(day),
                audit_started_at=NOW,
                authoritative_version_id=p.document_id,
            )
        )
        assert evidence and all(e.policy_version_id == rule.policy_version_id for e in evidence)
        assert any(limit in e.quote for e in evidence)
    assert resolve(engine, "2026-10-09").document_id == "POL-SZ-2026"
    assert (
        resolve(engine, "2026-11-02", stamp=datetime(2026, 10, 8, tzinfo=UTC)).document_id
        == "POL-SZ-2026"
    )
    assert resolve(engine, "2026-11-02", company="OTHER") is None
    meal = Expense(category="MEAL", city="深圳", incurred_on="2026-11-02", amount="50", nights=1)
    assert (
        hotel_city_limit(meal, resolve(engine, "2026-11-02", category="MEAL")).outcome
        == Outcome.UNKNOWN
    )
    after = snapshots(engine)
    for table in ("policy_versions", "policy_chunks"):
        assert set(before[table]) <= set(after[table])
    with Session(engine) as s:
        assert s.get(PolicyVersion, pid).authoritative_rules["rule_review_id"]
        assert len(s.scalars(select(PolicyPublicationEvent)).all()) == 1
    with pytest.raises(APIError) as error:
        draft.delete(uid)
    assert error.value.code == "PUBLISHED_SOURCE_PROTECTED"
    assert (
        draft.original(uid)[0] == Path("tests/fixtures/policies/publication-800.docx").read_bytes()
    )


def test_review_revisions_preserve_raw_and_reject_fabricated_amount(governance):
    engine, draft, store, embedding, url = governance
    uid = prepare(governance)
    approve_rule(store, uid, reason="二次人工修正说明：仍以原文800为准")
    data = store.read(uid)
    assert [r["revision"] for r in data["reviews"]] == [1, 2]
    assert data["reviews"][0]["extraction"] == data["reviews"][1]["extraction"]
    for updates, code in [
        (dict(amount="900.00"), "RULE_SOURCE_MISMATCH"),
        (dict(clause_start=0, clause_end=10), "RULE_SOURCE_MISMATCH"),
        (dict(expected_revision=0), "REVIEW_STALE"),
    ]:
        with pytest.raises(APIError) as e:
            approve_rule(store, uid, **updates)
        assert e.value.code == code
    assert len(store.read(uid)["reviews"]) == 2


@pytest.mark.parametrize(
    "fault", ["file", "parse", "chunk", "source", "model", "dimension", "vector"]
)
def test_tamper_or_model_mismatch_blocks_without_release(governance, fault):
    engine, draft, store, embedding, url = governance
    uid = prepare(governance)
    with engine.begin() as c:
        if fault == "file":
            c.execute(
                text("UPDATE policy_uploads SET original='bad'::bytea WHERE upload_id=:u"),
                dict(u=uid),
            )
        elif fault == "parse":
            c.execute(
                text(
                    "UPDATE policy_uploads SET parsed=jsonb_set(parsed,'{text}','\"forged\"') "
                    "WHERE upload_id=:u"
                ),
                dict(u=uid),
            )
        else:
            updates = {
                "chunk": "content='forged'",
                "source": "source_metadata='{}'::jsonb",
                "model": (
                    "source_metadata=jsonb_set(source_metadata,"
                    "'{embedding_model}', '\"BAAI/bge-m3\"')"
                ),
                "dimension": (
                    "source_metadata=jsonb_set(source_metadata,'{embedding_dimension}', '3')"
                ),
                "vector": "embedding=NULL",
            }
            c.execute(
                text(
                    "UPDATE policy_chunks SET "
                    + updates[fault]
                    + " WHERE policy_version_id=(SELECT policy_version_id FROM "
                    "policy_uploads WHERE upload_id=:u)"
                ),
                dict(u=uid),
            )
    view = store.preview(uid, PublicationPreview(token=TOKEN, predecessor_id="POL-SZ-2026"))
    assert view["blockers"]
    assert not store.history()["events"]


def test_confirmation_missing_stale_preview_and_past_start(governance):
    engine, draft, store, embedding, url = governance
    uid = prepare(governance, confirm=False)
    assert store.preview(uid, PublicationPreview(token=TOKEN))["blockers"] == [
        "RULE_CONFIRM_REQUIRED"
    ]
    approve_rule(store, uid)
    body = publish_body(store, uid)
    approve_rule(store, uid)
    with pytest.raises(APIError) as e:
        store.publish(uid, body)
    assert e.value.code == "PREVIEW_STALE"
    store.clock = lambda: datetime(2026, 11, 1, tzinfo=UTC)
    assert store.preview(uid, PublicationPreview(token=TOKEN, predecessor_id="POL-SZ-2026"))[
        "blockers"
    ] == ["FUTURE_EFFECTIVE_REQUIRED"]


def test_ambiguous_authority_and_cross_scope_predecessor_reject(governance):
    engine, draft, store, embedding, url = governance
    uid = prepare(governance)
    for predecessor in (None, "POL-BEIJING-2026", "POL-SZ-2025"):
        assert store.preview(uid, PublicationPreview(token=TOKEN, predecessor_id=predecessor))[
            "blockers"
        ]
    with Session(engine) as s, s.begin():
        p = s.get(PolicyVersion, "POL-SZ-2026")
        s.add(
            PolicyVersion(
                **{
                    c.name: getattr(p, c.name)
                    for c in PolicyVersion.__table__.columns
                    if c.name not in {"policy_version_id", "policy_key"}
                },
                policy_version_id="AMBIGUOUS-700",
                policy_key="AMBIGUOUS",
            )
        )
    assert resolve(engine, "2026-10-20") is None
    assert store.preview(uid, PublicationPreview(token=TOKEN, predecessor_id="POL-SZ-2026"))[
        "blockers"
    ] == ["POLICY_CONFLICT"]


@pytest.mark.parametrize("point", ["chunks", "journal", "commit"])
def test_transaction_failure_all_or_nothing_and_retry(governance, point):
    engine, draft, store, embedding, url = governance
    uid = prepare(governance)
    body = publish_body(store, uid)
    before = snapshots(engine)

    def injected(c, cursor, sql, params, ctx, many):
        if (point == "chunks" and sql.startswith("INSERT INTO policy_chunks")) or (
            point == "journal" and sql.startswith("INSERT INTO policy_publication_events")
        ):
            raise SQLAlchemyError("injected failure")

    def commit(c):
        raise SQLAlchemyError("commit interrupted")

    event.listen(engine, "commit", commit) if point == "commit" else event.listen(
        engine, "before_cursor_execute", injected
    )
    try:
        with pytest.raises(SQLAlchemyError):
            store.publish(uid, body)
    finally:
        event.remove(engine, "commit", commit) if point == "commit" else event.remove(
            engine, "before_cursor_execute", injected
        )
    assert snapshots(engine) == before
    new_store = PolicyPublicationStore(draft, embedding, app_env="test", clock=lambda: NOW)
    r = new_store.publish(uid, body)
    assert r == new_store.publish(uid, body)
    assert len(new_store.history()["events"]) == 1


def test_concurrent_duplicate_different_key_conflict_and_idempotency(governance):
    engine, draft, store, embedding, url = governance
    uid = prepare(governance)
    body = publish_body(store, uid)

    def invoke(_):
        return PolicyPublicationStore(draft, embedding, app_env="test", clock=lambda: NOW).publish(
            uid, body
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(invoke, range(3)))
    assert results[0] == results[1] == results[2]
    with pytest.raises(APIError) as e:
        store.publish(uid, body.model_copy(update={"actor": "different"}))
    assert e.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(APIError) as e:
        store.publish(uid, body.model_copy(update={"idempotency_key": "another-publish-key"}))
    assert e.value.code == "ALREADY_PUBLISHED"
    assert len(store.history()["events"]) == 1


def lifecycle(store, pid, kind="ROLLBACK", when="2026-12-01"):
    b = dict(
        token=TOKEN,
        kind=kind,
        effective_on=when,
        restore_id="POL-SZ-2026" if kind == "ROLLBACK" else None,
    )
    view = store.lifecycle_preview(pid, LifecyclePreview(**b))
    assert not view["blockers"], view
    return ChangeLifecycle(
        **b,
        actor="local-admin",
        reason="受控未来回退或退休",
        confirmed=True,
        preview_digest=view["preview_digest"],
        idempotency_key="lifecycle-" + str(uuid4()),
    )


def test_future_rollback_and_retire_preserve_history_and_no_faq_fallback(governance):
    engine, draft, store, embedding, url = governance
    uid = prepare(governance)
    r = publish(governance, uid)
    pid = r["policy"]["policy_version_id"]
    original = snapshots(engine)
    b = lifecycle(store, pid)
    rolled = store.change_lifecycle(pid, b)
    assert rolled == store.change_lifecycle(pid, b)
    rollback_id = rolled["policy"]["policy_version_id"]
    assert resolve(engine, "2026-11-02").hotel_limit == Decimal("800.00")
    assert resolve(engine, "2026-12-02").hotel_limit == Decimal("700.00")
    retired = store.change_lifecycle(
        rollback_id, lifecycle(store, rollback_id, "RETIRE", "2026-12-15")
    )
    assert retired["policy"] is None
    assert resolve(engine, "2026-12-16") is None
    assert resolve(engine, "2026-10-20").hotel_limit == Decimal("700.00")
    assert resolve(engine, "2026-11-02").document_id == pid
    after = snapshots(engine)
    for t in ("policy_versions", "policy_chunks"):
        assert set(original[t]) <= set(after[t])
    assert len(store.history()["events"]) == 3


def test_database_enforces_history_and_source_immutability(governance):
    engine, draft, store, embedding, url = governance
    uid = prepare(governance)
    pid = publish(governance, uid)["policy"]["policy_version_id"]
    for query, params in [
        ("DELETE FROM policy_publication_events", {}),
        ("DELETE FROM policy_rule_reviews", {}),
        ("UPDATE policy_uploads SET original=NULL WHERE upload_id=:id", dict(id=uid)),
        (
            "UPDATE policy_versions SET authoritative_rules='{}'::jsonb "
            "WHERE policy_version_id=:id",
            dict(id=pid),
        ),
        ("DELETE FROM policy_chunks WHERE policy_version_id=:id", dict(id="POL-SZ-2026")),
    ]:
        with pytest.raises(SQLAlchemyError), engine.begin() as c:
            c.execute(text(query), params)


def test_runtime_provider_mismatch_never_calls_provider(governance):
    engine, draft, store, embedding, url = governance
    uid = prepare(governance)

    class RealModel:
        model_name = "BAAI/bge-m3"
        dimension = 1024

        def embed_query(self, *a):
            raise AssertionError("No HTTP authorized")

    store.embedding = RealModel()
    assert store.preview(uid, PublicationPreview(token=TOKEN, predecessor_id="POL-SZ-2026"))[
        "blockers"
    ] == ["EMBEDDING_MISMATCH"]


def test_formal_workflow_dates_hard_gates_history_and_pending_human_pin(
    governance, tmp_path, monkeypatch
):
    engine, draft, store, embedding, url = governance
    settings = Settings(
        app_env="test",
        policy_retriever="pgvector",
        embedding_provider="deterministic",
        database_url=url.render_as_string(hide_password=False),
        policy_demo_enabled=True,
        policy_demo_token=TOKEN,
        policy_draft_embedding="deterministic",
        _env_file=None,
    )
    repo = MockClaimRepository(store_path=tmp_path / "claims.sqlite3")
    with TestClient(
        create_app(
            settings, repo, policy_store=draft, business_date_provider=lambda: date(2026, 11, 3)
        ),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 5000),
    ) as c:
        # Governance test clock matches actual run time enough for formal catalog visibility.
        store.clock = lambda: datetime.now(UTC)

        def claim(day, amount, notes=""):
            response = c.post(
                "/api/v1.1/claims",
                json=payload(
                    idempotency_key=str(uuid4()),
                    incurred_on=day,
                    trip_start=day,
                    trip_end="2026-11-03",
                    amount=amount,
                    notes=notes,
                ),
            )
            assert response.status_code == 202, response.text
            return response.json()["claim"]

        old = claim("2026-09-20", "680.00", "需要人工确认")
        assert old["decision_result"]["decision"] == "HUMAN_REVIEW"
        before = repo.get(old["claim_id"]).model_dump(mode="json")
        uid = prepare(governance)
        unpub = claim("2026-09-20", "780.00")
        assert unpub["decision_result"]["decision"] == "REJECT"
        publish(governance, uid)

        class FutureBusinessClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 11, 3, 6, tzinfo=UTC).astimezone(tz)

        monkeypatch.setattr("domain.submission.datetime", FutureBusinessClock)
        for day, amount, limit, outcome in [
            ("2026-10-20", "780.00", "700.00", "FAIL"),
            ("2026-11-02", "780.00", "800.00", "PASS"),
            ("2026-11-02", "850.00", "800.00", "FAIL"),
        ]:
            ch = claim(day, amount)
            r = ch["decision_result"]["rule_results"][0]
            assert r["limit"] == limit and r["outcome"] == outcome
            assert all(
                e["policy_version_id"] == r["policy_version_id"]
                for e in ch["decision_result"]["policy_evidence"]
            )
            expected = "APPROVE" if outcome == "PASS" else "REJECT"
            assert ch["decision_result"]["decision"] == expected
            assert repo.erp_write_count(ch["claim_id"]) == (1 if expected == "APPROVE" else 0)
        assert repo.get(old["claim_id"]).model_dump(mode="json") == before
        # Recheck original authority using durable run timestamp rather than current policy.
        catalog = PgPolicyCatalog(engine)
        stamp = datetime.now(UTC)
        assert catalog.resolve_pinned(
            policy_version_id="POL-SZ-2026",
            audit_started_at=stamp,
            legal_entity="DEMO-CN",
            category="HOTEL",
            city="深圳",
            incurred_on=date(2026, 10, 20),
        ).hotel_limit == Decimal("700")
        assert c.post("/api/v1.4/policies/history", json={"token": "wrong"}).status_code == 403
        assert (
            c.post(
                "/api/v1.4/policies/history",
                json={"token": TOKEN},
                headers={"X-Forwarded-For": "127.0.0.1"},
            ).status_code
            == 403
        )
        assert c.post("/api/v1.4/policies/history", json={"token": TOKEN}).status_code == 200
    repo.close()


def test_concurrent_distinct_releases_cannot_split_scope(governance):
    engine, draft, store, embedding, url = governance
    first = prepare(governance)
    import zipfile

    with zipfile.ZipFile("tests/fixtures/policies/publication-800.docx") as z:
        raw = amend_docx(
            "word/document.xml",
            z.read("word/document.xml").replace(b"2026-11-800", b"2026-other-800"),
        )
    second = prepare(governance, raw=raw, metadata={**FUTURE_META, "version": "2026-other-800"})
    bodies = [publish_body(store, uid) for uid in (first, second)]

    def invoke(args):
        uid, body = args
        try:
            return PolicyPublicationStore(
                draft, embedding, app_env="test", clock=lambda: NOW
            ).publish(uid, body)
        except APIError as e:
            return e.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(invoke, zip((first, second), bodies, strict=True)))
    assert sum(isinstance(r, dict) for r in result) == 1
    assert sum(isinstance(r, str) for r in result) == 1
    assert len(store.history()["events"]) == 1
    assert resolve(engine, "2026-11-02").hotel_limit == Decimal("800")


def test_metadata_correction_record_cannot_forge_company_provenance(governance):
    uid = prepare(governance, metadata={**FUTURE_META, "legal_entity": "OTHER"})
    store = governance[2]
    r = store.read(uid)["reviews"][0]
    assert r["confirmed_rule"]["metadata_changes"]["legal_entity"] == {
        "original": "DEMO-CN",
        "confirmed": "OTHER",
    }
    assert store.preview(uid, PublicationPreview(token=TOKEN))["blockers"] == [
        "METADATA_SOURCE_MISMATCH"
    ]
    assert not store.history()["events"]


def test_owned_process_exit_before_commit_is_durable_rollback(governance, tmp_path):
    import json
    import os
    import subprocess
    import sys
    import time

    engine, draft, store, embedding, url = governance
    uid = prepare(governance)
    body = publish_body(store, uid)
    before = snapshots(engine)
    marker = tmp_path / "transaction-open"
    code = """
import json, os, signal
from datetime import UTC, datetime
from pathlib import Path
from sqlalchemy import create_engine, event
from apps.api.policy_store import PolicyDraftStore
from apps.api.policy_publication_store import PolicyPublicationStore
from domain.policy_publication import PublishPolicy
from integrations.policies.parser import PolicyParser
from integrations.ocr.local import LocalOCRProvider
from rag.embeddings.providers import DeterministicTestEmbedding
engine=create_engine(os.environ['UI_P3B_CRASH_DATABASE_URL'])
draft=PolicyDraftStore(engine,PolicyParser(LocalOCRProvider()),
    lambda _:DeterministicTestEmbedding())
store=PolicyPublicationStore(draft,DeterministicTestEmbedding(),app_env='test',
    clock=lambda:datetime(2026,10,9,6,tzinfo=UTC))
def pause_before_commit(connection):
    Path(os.environ['UI_P3B_CRASH_MARKER']).write_text('transaction not committed',encoding='utf-8')
    signal.pause()
event.listen(engine,'commit',pause_before_commit)
store.publish(os.environ['UI_P3B_CRASH_UID'],PublishPolicy(**json.loads(os.environ['UI_P3B_CRASH_BODY'])))
"""
    env = os.environ.copy()
    env.update(
        UI_P3B_CRASH_DATABASE_URL=url.render_as_string(hide_password=False),
        UI_P3B_CRASH_MARKER=str(marker),
        UI_P3B_CRASH_UID=uid,
        UI_P3B_CRASH_BODY=body.model_dump_json(exclude={"token"}),
    )
    # The DTO capability is test-only; store authorization was already covered at the HTTP gate.
    env["UI_P3B_CRASH_BODY"] = json.dumps({**json.loads(env["UI_P3B_CRASH_BODY"]), "token": TOKEN})
    with subprocess.Popen(
        [sys.executable, "-c", code], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ) as child:
        deadline = time.monotonic() + 20
        while not marker.exists() and time.monotonic() < deadline and child.poll() is None:
            time.sleep(0.05)
        assert marker.exists(), "Owned child must reach the uncommitted transaction"
        child.terminate()
        child.wait(timeout=5)
    assert snapshots(engine) == before
    assert store.publish(uid, body) == store.publish(uid, body)


def test_pending_future_claim_rechecks_pinned_old_policy_after_publish(
    governance, tmp_path, monkeypatch
):
    engine, draft, store, embedding, url = governance

    class FutureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 3, 6, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr("domain.submission.datetime", FutureClock)
    settings = Settings(
        app_env="test",
        policy_retriever="pgvector",
        embedding_provider="deterministic",
        database_url=url.render_as_string(hide_password=False),
        _env_file=None,
    )
    repo = MockClaimRepository(store_path=tmp_path / "pending.sqlite3")
    with TestClient(
        create_app(settings, repo, business_date_provider=lambda: date(2026, 11, 3))
    ) as c:
        created = c.post(
            "/api/v1.1/claims",
            json=payload(
                idempotency_key="pinned-future-claim",
                incurred_on="2026-11-02",
                trip_start="2026-11-02",
                trip_end="2026-11-03",
                amount="680.00",
                notes="需要人工核验",
            ),
        ).json()
        assert created["claim"]["decision_result"]["decision"] == "HUMAN_REVIEW"
        op = created["operation"]
        cid = created["claim"]["claim_id"]
        before = repo.get(cid).model_dump(mode="json")
        old_events = [e.model_dump(mode="json") for e in repo.events(cid)]
        store.clock = lambda: datetime.now(UTC)
        pid = publish(governance, prepare(governance))["policy"]["policy_version_id"]
        assert resolve(engine, "2026-11-02", stamp=datetime.now(UTC)).document_id == pid
        assert repo.get(cid).model_dump(mode="json") == before
        assert [e.model_dump(mode="json") for e in repo.events(cid)] == old_events
        review = c.post(
            f"/claims/{cid}/human-review",
            json=dict(
                run_id=op["run_id"],
                expected_revision=op["revision"],
                decision="APPROVE",
                reviewer_id="local-reviewer",
                comment="按最初700元制度核验，无版本静默切换",
                idempotency_key="pin-review",
            ),
        )
        assert review.status_code == 200, review.text
        resumed = c.post(
            f"/claims/{cid}/resume",
            json=dict(
                run_id=op["run_id"],
                expected_revision=review.json()["revision"],
                idempotency_key="pin-resume",
            ),
        )
        assert resumed.status_code == 202, resumed.text
        final = repo.get(cid)
        assert final.decision_result.rule_results[0].policy_version_id == "POL-SZ-2026"
        assert final.decision_result.rule_results[0].limit == Decimal("700")
        assert all(
            e.policy_version_id == "POL-SZ-2026" for e in final.decision_result.policy_evidence
        )
        assert repo.erp_write_count(cid) == 1
    repo.close()


def test_rollback_failure_and_invalid_history_scope_preserved(governance):
    engine, draft, store, embedding, url = governance
    pid = publish(governance, prepare(governance))["policy"]["policy_version_id"]
    b = lifecycle(store, pid)
    before = snapshots(engine)

    def injected(c, cursor, sql, params, ctx, many):
        if sql.startswith("INSERT INTO policy_publication_events"):
            raise SQLAlchemyError("rollback journal failed")

    event.listen(engine, "before_cursor_execute", injected)
    try:
        with pytest.raises(SQLAlchemyError):
            store.change_lifecycle(pid, b)
    finally:
        event.remove(engine, "before_cursor_execute", injected)
    assert snapshots(engine) == before
    for changes in (
        dict(effective_on="2026-10-08"),
        dict(restore_id="POL-BEIJING-2026"),
        dict(effective_on="2026-11-01"),
        dict(restore_id=pid),
    ):
        body = LifecyclePreview(
            token=TOKEN,
            kind="ROLLBACK",
            **{**dict(effective_on="2026-12-01", restore_id="POL-SZ-2026"), **changes},
        )
        assert store.lifecycle_preview(pid, body)["blockers"]
    assert store.change_lifecycle(pid, b) == store.change_lifecycle(pid, b)


def test_expired_release_never_falls_back_to_lower_authority_faq(governance):
    import zipfile

    with zipfile.ZipFile("tests/fixtures/policies/publication-800.docx") as z:
        raw = amend_docx(
            "word/document.xml", z.read("word/document.xml").replace(b"2027-01-01", b"2026-12-01")
        )
    uid = prepare(governance, raw=raw, metadata={**FUTURE_META, "effective_to": "2026-12-01"})
    publish(governance, uid)
    assert resolve(governance[0], "2026-11-02").hotel_limit == Decimal("800")
    assert resolve(governance[0], "2026-12-01") is None
    assert resolve(governance[0], "2026-10-20").hotel_limit == Decimal("700")
