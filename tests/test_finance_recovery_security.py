"""Synthetic ASGI/LangGraph security checks; every database is disposable."""

import json
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.config import Settings
from apps.api.main import create_app
from apps.api.repository import MockClaimRepository
from domain.enterprise import CreateUser
from integrations.erp.mock import MockERP
from integrations.faults import FailureMode
from integrations.retry import RetryPolicy
from tests.test_enterprise import PASSWORD, action, create, login


@pytest.fixture
def secured(tmp_path):
    path = tmp_path / "security.sqlite3"
    repo = MockClaimRepository(store_path=str(path))
    erp = MockERP(repo)
    settings = Settings(
        app_env="test",
        enterprise_enabled=True,
        enterprise_cookie_secure=False,
        llm_provider="mock",
        policy_retriever="inmemory",
        _env_file=None,
    )
    app = create_app(settings, repo, erp=erp, retry_policies={"erp": RetryPolicy(max_attempts=1)})
    store = app.state.enterprise_app.state.store
    users = {}
    for name, role in (
        ("employee", "EMPLOYEE"),
        ("finance", "FINANCE"),
        ("finance2", "FINANCE"),
        ("admin", "ADMIN"),
    ):
        users[name] = store.create_user(
            "DEMO-CN",
            CreateUser(
                username=name,
                password=PASSWORD,
                name="新申请人",
                department="华南销售部",
                position="客户经理",
                role=role,
            ),
        )
    store.create_user(
        "DEMO-B",
        CreateUser(
            username="finance",
            password=PASSWORD,
            name="新申请人",
            department="华南销售部",
            position="客户经理",
            role="FINANCE",
        ),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, app, repo, store, users, erp, path
    repo.close()


def context(repo, claim):
    return repo.run_context(claim["claim_id"], repo.get(claim["claim_id"]).active_run_id)


def current(c, claim):
    return c.get("/api/v1.5/claims/" + claim["claim_id"]).json()


def retry(c, claim, key=None):
    return c.post(
        "/api/v1.5/claims/" + claim["claim_id"] + "/retry",
        json={"expected_revision": claim["revision"], "idempotency_key": key or str(uuid4())},
    )


def pending(secured, monkeypatch, point="human_review", **fields):
    c, app, repo, store, users, erp, path = secured
    assert login(c).status_code == 200
    claim, _ = create(c, **fields)
    assert login(c, "finance").status_code == 200
    service = app.state.audit_service
    original = getattr(service, point)

    async def crash(*args, **kwargs):
        raise RuntimeError("synthetic-process-fault-before-" + point)

    monkeypatch.setattr(service, point, crash)
    result = action(c, claim, "APPROVE")
    assert result.status_code == 500
    monkeypatch.setattr(service, point, original)
    claim = current(c, claim)
    assert claim["stage"] == "FINANCE_APPLYING"
    assert bool(context(repo, claim)["human_review"]) == (point == "resume")
    assert repo.erp_write_count(claim["claim_id"]) == 0
    return claim


def change_user(secured, sql, params):
    # A distinct connection also tests SQLite transaction/trigger enforcement.
    with sqlite3.connect(secured[-1]) as db:
        db.execute(sql, params)


def disable(secured):
    c, _, _, _, users, _, _ = secured
    assert login(c, "admin").status_code == 200
    response = c.post(
        "/api/v1.5/users/" + users["finance"]["id"] + "/enabled", json={"enabled": False}
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "mutation",
    [
        "disable",
        "delete",
        "role",
        "tenant",
        "reenable",
        "role_restore",
        "tenant_restore",
        "version",
        "intent",
    ],
)
def test_revoked_or_mismatched_intent_cannot_approve(secured, monkeypatch, mutation):
    c, app, repo, store, users, erp, path = secured
    claim = pending(secured, monkeypatch)
    uid = users["finance"]["id"]
    if mutation in ("disable", "reenable"):
        disable(secured)
        if mutation == "reenable":
            assert (
                c.post("/api/v1.5/users/" + uid + "/enabled", json={"enabled": True}).status_code
                == 200
            )
    elif mutation == "delete":
        change_user(secured, "DELETE FROM enterprise_users WHERE id=?", (uid,))
    elif mutation in ("role", "role_restore"):
        change_user(secured, "UPDATE enterprise_users SET role='EMPLOYEE' WHERE id=?", (uid,))
        if mutation == "role_restore":
            change_user(secured, "UPDATE enterprise_users SET role='FINANCE' WHERE id=?", (uid,))
    elif mutation in ("tenant", "tenant_restore"):
        change_user(secured, "UPDATE enterprise_users SET company_id='DEMO-C' WHERE id=?", (uid,))
        if mutation == "tenant_restore":
            change_user(
                secured, "UPDATE enterprise_users SET company_id='DEMO-CN' WHERE id=?", (uid,)
            )
    elif mutation == "version":
        change_user(
            secured,
            "UPDATE enterprise_claims SET version=version+1 WHERE claim_id=?",
            (claim["claim_id"],),
        )
    else:
        with store.lock, store.db:
            row = store.db.execute(
                "SELECT scope,key,result FROM enterprise_requests WHERE scope LIKE '%:finance:%'"
            ).fetchone()
            op = json.loads(row[2])
            op["authorization"]["actor_id"] = users["finance2"]["id"]
            store.db.execute(
                "UPDATE enterprise_requests SET result=? WHERE scope=? AND key=?",
                (json.dumps(op), row[0], row[1]),
            )
    assert login(c, "finance2").status_code == 200
    result = retry(c, claim)
    assert result.status_code in (403, 409), result.text
    assert current(c, claim)["stage"] == "MANUAL_RECOVERY_REQUIRED"
    assert context(repo, claim)["human_review"] is None
    assert repo.erp_write_count(claim["claim_id"]) == 0
    denied = store.events(users["finance2"], claim["claim_id"])[-1]
    assert denied["kind"] == "FINANCE_RECOVERY_DENIED"
    assert denied["actor_id"] == users["finance2"]["id"]
    assert denied["original_actor_id"] == uid
    assert denied["security_reason"] in ("FINANCE_AUTHORITY_REVOKED", "FINANCE_INTENT_INVALID")
    # The pending record is preserved and another explicit approval cannot inherit it.
    assert (
        store.db.execute(
            "SELECT count(*) FROM enterprise_requests WHERE scope LIKE '%:finance:%'"
        ).fetchone()[0]
        == 1
    )
    assert action(c, current(c, claim), "APPROVE").status_code == 409
    assert retry(c, current(c, claim)).status_code == 409


@pytest.mark.parametrize(
    "caller,expected", [("employee", 403), ("admin", 403), ("expired", 401), ("other_tenant", 404)]
)
def test_retry_requires_live_finance_caller(secured, monkeypatch, caller, expected):
    c, _, repo, store, users, _, _ = secured
    claim = pending(secured, monkeypatch)
    if caller == "expired":
        with store.lock, store.db:
            store.db.execute("DELETE FROM enterprise_sessions")
    elif caller == "other_tenant":
        login(c, "finance", company="DEMO-B")
    else:
        login(c, caller)
    assert retry(c, claim).status_code == expected
    assert repo.erp_write_count(claim["claim_id"]) == 0
    assert context(repo, claim)["human_review"] is None


@pytest.mark.parametrize("point", ["human_review", "resume"])
def test_valid_recovery_retains_reviewer_and_is_idempotent(secured, monkeypatch, point):
    c, _, repo, store, users, _, _ = secured
    claim = pending(secured, monkeypatch, point)
    login(c, "finance2")
    result = retry(c, claim)
    assert result.status_code == 200, result.text
    assert result.json()["stage"] == "APPROVED"
    assert context(repo, claim)["human_review"]["reviewer_id"] == users["finance"]["id"]
    assert repo.erp_write_count(claim["claim_id"]) == 1
    assert retry(c, current(c, claim)).status_code == 409
    assert (
        len([e for e in repo.events(claim["claim_id"]) if e.event_type == "HUMAN_REVIEW_SUBMITTED"])
        == 1
    )


def test_revocation_after_durable_review_preserves_history(secured, monkeypatch):
    c, _, repo, store, users, _, _ = secured
    claim = pending(secured, monkeypatch, "resume")
    before = context(repo, claim)["human_review"]
    disable(secured)
    login(c, "finance2")
    assert retry(c, claim).status_code == 403
    assert context(repo, claim)["human_review"] == before
    assert repo.erp_write_count(claim["claim_id"]) == 0


@pytest.mark.parametrize("boundary", ["review", "resume", "erp"])
def test_revoke_during_async_recovery_prevents_new_effect(secured, monkeypatch, boundary):
    c, app, repo, store, users, erp, _ = secured
    claim = pending(secured, monkeypatch)
    service = app.state.audit_service
    if boundary == "erp":
        original = erp.write_decision

        async def interleaved(*args, **kwargs):
            change_user(
                secured,
                "UPDATE enterprise_users SET enabled=0 WHERE id=?",
                (users["finance"]["id"],),
            )
            return await original(*args, **kwargs)

        monkeypatch.setattr(erp, "write_decision", interleaved)
    else:
        name = "human_review" if boundary == "review" else "resume"
        original = getattr(service, name)

        async def interleaved(*args, **kwargs):
            change_user(
                secured,
                "UPDATE enterprise_users SET enabled=0 WHERE id=?",
                (users["finance"]["id"],),
            )
            return await original(*args, **kwargs)

        monkeypatch.setattr(service, name, interleaved)
    login(c, "finance2")
    assert retry(c, claim).status_code == 403
    assert repo.erp_write_count(claim["claim_id"]) == 0
    assert current(c, claim)["stage"] == "MANUAL_RECOVERY_REQUIRED"
    if boundary == "review":
        assert context(repo, claim)["human_review"] is None


@pytest.mark.parametrize("lost", [False, True])
def test_pending_erp_revocation_and_completed_effect_reconciliation(secured, lost):
    c, _, repo, store, users, erp, _ = secured
    login(c)
    claim, _ = create(c)
    login(c, "finance")
    erp.faults.mode = FailureMode.SUCCESS_THEN_RESPONSE_LOST if lost else FailureMode.TIMEOUT_ALWAYS
    result = action(c, claim, "APPROVE")
    assert result.status_code == 200, result.text
    waiting = result.json()
    assert waiting["stage"] == "ERP_SYNC_PENDING"
    review = context(repo, claim)["human_review"]
    assert repo.erp_write_count(claim["claim_id"]) == int(lost)
    disable(secured)
    login(c, "finance2")
    erp.faults.mode = FailureMode.NONE
    result = retry(c, waiting)
    assert result.status_code == (200 if lost else 403), result.text
    assert context(repo, claim)["human_review"] == review
    assert repo.erp_write_count(claim["claim_id"]) == int(lost)
    if lost:
        assert result.json()["stage"] == "APPROVED"
        assert retry(c, waiting).status_code == 409  # fresh key + stale revision


def test_hard_gate_still_blocks_recovered_approve(secured, monkeypatch):
    c, _, repo, store, users, _, _ = secured
    claim = pending(secured, monkeypatch, amount="9999.00")
    login(c, "finance2")
    assert retry(c, claim).status_code == 409
    assert repo.erp_write_count(claim["claim_id"]) == 0
    assert context(repo, claim)["human_review"] is None


def test_internal_service_call_without_finance_capability_is_blocked(secured):
    from domain.schemas import HumanReviewRequest

    c, app, repo, store, users, _, _ = secured
    login(c)
    claim, _ = create(c)
    run = repo.get(claim["claim_id"]).active_run_id
    request = HumanReviewRequest(
        run_id=run,
        expected_revision=context(repo, claim)["revision"],
        decision="APPROVE",
        reviewer_id=users["finance"]["id"],
        comment="synthetic forged internal call",
        idempotency_key=str(uuid4()),
    )
    with pytest.raises(Exception, match="需要有效财务操作"):
        c.portal.call(app.state.audit_service.human_review, claim["claim_id"], request)
    assert context(repo, claim)["human_review"] is None
    assert repo.erp_write_count(claim["claim_id"]) == 0


def test_sqlite_writer_transaction_serializes_erp_commit_and_revocation(secured, monkeypatch):
    from threading import Event, Thread
    from time import sleep

    c, app, repo, store, users, erp, path = secured
    claim = pending(secured, monkeypatch)
    attempted, finished = Event(), Event()
    threads = []
    original = store.authorize_finance

    def guarded(*args, **kwargs):
        actor = original(*args, **kwargs)
        if kwargs.get("effect_key") and not threads:

            def revoke():
                with sqlite3.connect(path) as db:
                    attempted.set()
                    db.execute(
                        "UPDATE enterprise_users SET enabled=0 WHERE id=?",
                        (users["finance"]["id"],),
                    )
                finished.set()

            thread = Thread(target=revoke)
            threads.append(thread)
            thread.start()
            assert attempted.wait(1)
            sleep(0.05)
            # Authorization read + ERP mutation share the SQLite writer transaction.
            assert not finished.is_set()
        return actor

    async def ordered_write(*args, **kwargs):
        result = await write(*args, **kwargs)
        for thread in threads:
            thread.join(2)
        assert finished.is_set()
        return result

    write = erp.write_decision
    monkeypatch.setattr(store, "authorize_finance", guarded)
    monkeypatch.setattr(erp, "write_decision", ordered_write)
    login(c, "finance2")
    result = retry(c, claim)
    assert result.status_code == 200, result.text
    assert result.json()["stage"] == "APPROVED"
    assert store.user(users["finance"]["id"])["enabled"] == 0
    assert repo.erp_write_count(claim["claim_id"]) == 1
    assert context(repo, claim)["human_review"]["reviewer_id"] == users["finance"]["id"]
    events = store.events(users["finance2"], claim["claim_id"])
    reconciled = next(e for e in events if e["kind"] == "FINANCE_EFFECT_RECONCILED")
    assert reconciled["actor_id"] == users["finance2"]["id"]
    assert not any(e["kind"] == "FINANCE_APPROVE" for e in events)


@pytest.mark.parametrize("mutation", ["session", "role"])
def test_current_requester_revoked_while_waiting_cannot_write(secured, monkeypatch, mutation):
    c, app, repo, store, users, erp, _ = secured
    claim = pending(secured, monkeypatch)
    original = app.state.audit_service.human_review

    async def interleaved(*args, **kwargs):
        if mutation == "session":
            change_user(
                secured,
                "DELETE FROM enterprise_sessions WHERE user_id=?",
                (users["finance2"]["id"],),
            )
        else:
            change_user(
                secured,
                "UPDATE enterprise_users SET role='ADMIN' WHERE id=?",
                (users["finance2"]["id"],),
            )
        return await original(*args, **kwargs)

    monkeypatch.setattr(app.state.audit_service, "human_review", interleaved)
    login(c, "finance2")
    result = retry(c, claim)
    assert result.status_code == (401 if mutation == "session" else 403), result.text
    assert repo.erp_write_count(claim["claim_id"]) == 0
    assert context(repo, claim)["human_review"] is None


def test_unstamped_legacy_pending_intent_requires_manual_recovery(secured, monkeypatch):
    c, _, repo, store, users, _, _ = secured
    claim = pending(secured, monkeypatch)
    with store.lock, store.db:
        scope, key, raw = store.db.execute(
            "SELECT scope,key,result FROM enterprise_requests WHERE scope LIKE '%:finance:%'"
        ).fetchone()
        op = json.loads(raw)
        del op["authorization"]
        store.db.execute(
            "UPDATE enterprise_requests SET result=? WHERE scope=? AND key=?",
            (json.dumps(op), scope, key),
        )
    login(c, "finance2")
    assert retry(c, claim).status_code == 409
    assert current(c, claim)["stage"] == "MANUAL_RECOVERY_REQUIRED"
    assert repo.erp_write_count(claim["claim_id"]) == 0


def test_erp_effect_must_match_persisted_approval(secured, monkeypatch):
    c, app, repo, store, users, erp, _ = secured
    claim = pending(secured, monkeypatch)

    async def mismatched_write(result, *, idempotency_key):
        return repo.write_erp(result.claim_id, "REJECTED", key=idempotency_key)

    monkeypatch.setattr(erp, "write_decision", mismatched_write)
    login(c, "finance2")
    assert retry(c, claim).status_code == 409
    assert repo.erp_write_count(claim["claim_id"]) == 0
    assert current(c, claim)["stage"] == "MANUAL_RECOVERY_REQUIRED"
