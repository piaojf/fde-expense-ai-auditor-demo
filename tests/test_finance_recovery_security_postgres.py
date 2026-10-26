"""P4B requests queued before revocation cannot write facts or supersede a run."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from p4b_fix2_support import create, login
from test_p4a_postgres import hotel_portal as hotel_portal
from test_p4b_postgres import hybrid as hybrid
from test_policies_postgres import policy_pg as policy_pg

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="Disposable PostgreSQL required"),
]
BASE = "/api/v1.5/p4b/v1"


@pytest.mark.parametrize("route", ["verify", "reaudit"])
@pytest.mark.parametrize("mutation", ["disable", "role", "tenant", "role_restore"])
def test_p4b_queued_finance_request_revalidates_at_commit(hybrid, monkeypatch, route, mutation):
    env, _ = hybrid
    c, app, repo, store, users, _ = env
    claim = create(
        c,
        dict(
            kind="ALLOWANCE",
            amount="36.00",
            notes="合成公务日",
            facts=dict(full_days=1, lunches=1, dinners=1),
        ),
    )
    login(c, "finance")
    cid = claim["claim_id"]
    enterprise_app = app.state.enterprise_app
    catalog = enterprise_app.state.compliance
    before_facts = catalog.store.read("FACTS", cid)
    original_run = repo.get(cid).active_run_id
    before_run = repo.run_context(cid, original_run)
    before = c.get("/api/v1.5/claims/" + cid).json()
    queued, release = Event(), Event()
    real_lock = enterprise_app.state.work_lock

    class QueuedLock:
        async def __aenter__(self):
            queued.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
            return await real_lock.__aenter__()

        async def __aexit__(self, *args):
            return await real_lock.__aexit__(*args)

    monkeypatch.setattr(enterprise_app.state, "work_lock", QueuedLock())
    body = dict(expected_revision=before["revision"], idempotency_key=str(uuid4()))
    if route == "verify":
        body.update(
            expected_revision=before_facts[0],
            facts=dict(duplicate_clear=True),
            source_reference="SYNTHETIC-QUEUED-FACT",
            basis="合成排队撤权测试",
            confirmed=True,
        )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(c.post, BASE + f"/claims/{cid}/{route}", json=body)
        assert queued.wait(3), "request never reached async authorization window"
        with store.lock, store.db:
            uid = users["finance"]["id"]
            if mutation == "disable":
                store.set_enabled(users["admin"], uid, False)
            elif mutation in ("role", "role_restore"):
                store.db.execute("UPDATE enterprise_users SET role='EMPLOYEE' WHERE id=?", (uid,))
                if mutation == "role_restore":
                    store.db.execute(
                        "UPDATE enterprise_users SET role='FINANCE' WHERE id=?", (uid,)
                    )
            else:
                store.db.execute(
                    "UPDATE enterprise_users SET company_id='DEMO-B' WHERE id=?", (uid,)
                )
        release.set()
        response = future.result(timeout=15)
    assert response.status_code in (401, 403), response.text
    assert catalog.store.read("FACTS", cid) == before_facts
    assert repo.get(cid).active_run_id == original_run
    assert repo.run_context(cid, original_run) == before_run
    assert repo.erp_write_count(cid) == 0
    assert store.claim_row(cid, users["admin"])["version"] == before["version"]


@pytest.mark.parametrize("mutation", ["disable", "role", "tenant", "role_restore"])
def test_p4b_queued_scope_confirmation_revalidates_admin(hotel_portal, monkeypatch, mutation):
    from apps.api.compliance_api import install_compliance_api
    from apps.api.config import Settings

    c, app, repo, store, users, pid = hotel_portal
    enterprise_app = app.state.enterprise_app
    install_compliance_api(
        enterprise_app,
        Settings(
            app_env="test",
            enterprise_enabled=True,
            p4a_enabled=True,
            p4b_enabled=True,
            _env_file=None,
        ),
        repo,
        app.state.audit_service,
    )
    login(c, "admin")
    source = c.get(BASE + "/policies/" + pid).json()
    catalog = enterprise_app.state.compliance
    scope_key = users["admin"]["company_id"] + ":" + pid
    before = catalog.store.read("SCOPE", scope_key)
    assert before == (0, None)
    queued, release = Event(), Event()
    real_lock = enterprise_app.state.work_lock

    class QueuedLock:
        async def __aenter__(self):
            queued.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
            return await real_lock.__aenter__()

        async def __aexit__(self, *args):
            return await real_lock.__aexit__(*args)

    monkeypatch.setattr(enterprise_app.state, "work_lock", QueuedLock())
    body = dict(
        extraction_digest=source["rules"]["extraction_digest"],
        confirmed=True,
        reason="合成范围确认撤权测试",
        idempotency_key=str(uuid4()),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(c.post, BASE + f"/policies/{pid}/confirm-scope", json=body)
        assert queued.wait(3)
        with store.lock, store.db:
            uid = users["admin"]["id"]
            if mutation == "disable":
                store.db.execute("UPDATE enterprise_users SET enabled=0 WHERE id=?", (uid,))
            elif mutation in ("role", "role_restore"):
                store.db.execute("UPDATE enterprise_users SET role='EMPLOYEE' WHERE id=?", (uid,))
                if mutation == "role_restore":
                    store.db.execute("UPDATE enterprise_users SET role='ADMIN' WHERE id=?", (uid,))
            else:
                store.db.execute(
                    "UPDATE enterprise_users SET company_id='DEMO-B' WHERE id=?", (uid,)
                )
        release.set()
        response = future.result(timeout=15)
    assert response.status_code in (401, 403), response.text
    assert catalog.store.read("SCOPE", scope_key) == before
    assert not any(e["kind"] == "P4B_SCOPE_CONFIRMED" for e in store.events(users["admin"]))
