"""Real ASGI/SQLite/LangGraph acceptance for opt-in roles and tenant boundaries."""

from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.config import Settings
from apps.api.enterprise import COOKIE
from apps.api.main import create_app
from apps.api.repository import MockClaimRepository
from domain.enterprise import CreateUser
from integrations.erp.mock import MockERP
from integrations.faults import FailureMode
from integrations.retry import RetryPolicy
from tests.test_submission import payload

PASSWORD = "Synthetic-Test-Only-123"


def captcha(client):
    with patch("apps.api.enterprise_store.generate_code", return_value="2x4b"):
        response = client.get("/api/v1.5/auth/captcha")
    assert response.status_code == 200
    return {"captcha_id": response.json()["captcha_id"], "captcha_code": "2x4b"}


def login(client, username="employee", company="DEMO-CN", password=PASSWORD):
    return client.post(
        "/api/v1.5/auth/login",
        json={"company_id": company, "username": username, "password": password,
              **captcha(client)},
    )


def fields(**updates):
    result = payload(**updates)
    result.pop("idempotency_key")
    return result


@pytest.fixture
def portal(tmp_path):
    repo = MockClaimRepository(store_path=str(tmp_path / "portal.sqlite3"))
    app = create_app(
        Settings(
            app_env="test", enterprise_enabled=True, enterprise_cookie_secure=False, _env_file=None
        ),
        repo,
    )
    store = app.state.enterprise_app.state.store
    for company in ("DEMO-CN", "DEMO-B"):
        for username, role in (
            ("employee", "EMPLOYEE"),
            ("other", "EMPLOYEE"),
            ("finance", "FINANCE"),
            ("admin", "ADMIN"),
        ):
            store.create_user(
                company,
                CreateUser(
                    username=username,
                    password=PASSWORD,
                    name="新申请人",
                    department="华南销售部",
                    position="客户经理",
                    role=role,
                ),
            )
    with TestClient(app) as client:
        yield client, app, repo, store
    repo.close()


def create(client, **updates):
    body = {"fields": fields(**updates), "idempotency_key": str(uuid4())}
    response = client.post("/api/v1.5/claims", json=body)
    assert response.status_code == 202, response.text
    claim = client.get("/api/v1.5/claims/" + response.json()["claim_id"]).json()
    return claim, body


def action(client, claim, decision, reason="人工核对后决定", key=None):
    return client.post(
        f"/api/v1.5/claims/{claim['claim_id']}/finance-action",
        json={
            "action": decision,
            "reason": reason,
            "expected_revision": claim["revision"],
            "idempotency_key": key or str(uuid4()),
        },
    )


def test_session_password_hash_logout_disabled_and_ttl(portal):
    c, app, repo, store = portal
    assert c.get("/api/v1.5/claims").status_code == 401
    result = login(c)
    assert result.status_code == 200
    cookie = result.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/api/v1.5" in cookie
    uid = result.json()["user"]["id"]
    raw = store.db.execute(
        "SELECT password_hash FROM enterprise_users WHERE id=?", (uid,)
    ).fetchone()[0]
    assert PASSWORD not in raw and ":" in raw
    saved = c.cookies.get(COOKIE)
    assert saved not in str(store.db.execute("SELECT * FROM enterprise_sessions").fetchall())
    assert c.post("/api/v1.5/auth/logout").status_code == 200
    c.cookies.set(COOKIE, saved, path="/api/v1.5")
    assert c.get("/api/v1.5/auth/me").status_code == 401
    login(c)
    with store.db:
        store.db.execute("UPDATE enterprise_sessions SET expires=0")
    assert c.get("/api/v1.5/auth/me").status_code == 401


def test_login_error_generic_rate_limit_and_untrusted_origin(portal):
    c, _, _, _ = portal
    errors = [
        login(c, username="unknown").json()["error"]["code"],
        login(c, password="bad").json()["error"]["code"],
    ]
    assert errors == ["LOGIN_FAILED", "LOGIN_FAILED"]
    for _ in range(7):
        login(c, password="bad")
    assert login(c).status_code == 429
    assert (
        c.post(
            "/api/v1.5/auth/login",
            headers={"Origin": "https://untrusted.invalid"},
            json={"company_id": "DEMO-CN", "username": "admin", "password": PASSWORD},
        ).status_code
        == 403
    )


def test_policy_management_requires_admin_session_even_with_capability(portal):
    c, _, _, _ = portal
    uid = "00000000-0000-0000-0000-000000000000"
    paths = [
        "/policies/list",
        *(f"/policies/{uid}/{action}" for action in
          ("read", "original", "delete", "confirm", "ingest")),
        "/publication/policies/history",
        *(f"/publication/policies/{uid}/{action}" for action in
          ("rules", "confirm-rule", "publication-preview", "publish", "lifecycle")),
    ]
    # A legacy capability cannot replace a session; role checks precede store/parser calls.
    for username, expected in ((None, 401), ("employee", 403), ("finance", 403)):
        if username:
            assert login(c, username).status_code == 200
        for path in paths:
            response = c.post("/api/v1.5" + path, json={"token": "ignored-legacy-capability"})
            assert response.status_code == expected, (path, response.text)
        response = c.post(
            "/api/v1.5/policies/upload",
            files={"file": ("policy.docx", b"must-not-be-parsed")},
            data={"token": "ignored-legacy-capability"},
        )
        assert response.status_code == expected


@pytest.mark.parametrize(
    "path",
    [
        "/claims",
        "/dashboard/metrics",
        "/claims/CLM-2026-1024",
        "/api/v1.1/claims/CLM-2026-1024",
        "/api/v1.2/claims/a",
    ],
)
def test_old_business_routes_closed_in_enterprise_mode(portal, path):
    c, _, _, _ = portal
    login(c)
    assert c.get(path).status_code == 403
    assert c.get("/health").status_code == 200


def test_employee_only_own_claims_tenant_and_role_checks(portal):
    c, _, repo, store = portal
    login(c)
    claim, _ = create(c)
    assert (
        claim["claim"]["employee"]["employee_id"] == c.get("/api/v1.5/auth/me").json()["user"]["id"]
    )
    for path in ("/users", "/dashboard", "/audit"):
        assert c.get("/api/v1.5" + path).status_code == 403
    assert action(c, claim, "APPROVE").status_code == 403
    login(c, "other")
    assert c.get("/api/v1.5/claims").json()["items"] == []
    assert c.get("/api/v1.5/claims/" + claim["claim_id"]).status_code == 404
    login(c, "finance", "DEMO-B")
    assert c.get("/api/v1.5/claims").json()["items"] == []
    assert action(c, claim, "REJECT").status_code == 404
    login(c, "admin")
    assert len(c.get("/api/v1.5/claims").json()["items"]) == 1
    assert action(c, claim, "APPROVE").status_code == 403
    assert (
        c.post("/api/v1.5/policies/00000000-0000-0000-0000-000000000000/read", json={}).status_code
        == 404
    )


def test_create_identity_is_not_client_controlled(portal):
    c, _, _, _ = portal
    login(c)
    assert (
        c.post(
            "/api/v1.5/claims",
            json={"fields": fields(applicant_name="另一员工"), "idempotency_key": str(uuid4())},
        ).status_code
        == 403
    )
    login(c, "finance")
    assert (
        c.post(
            "/api/v1.5/claims", json={"fields": fields(), "idempotency_key": str(uuid4())}
        ).status_code
        == 403
    )


def test_low_risk_waits_for_finance_preserves_ai_and_idempotent_approval(portal):
    c, _, repo, _ = portal
    login(c)
    claim, body = create(c)
    assert claim["stage"] == "FINANCE_REVIEW" and claim["ai_result"]["decision"] == "APPROVE"
    assert repo.erp_write_count(claim["claim_id"]) == 0
    assert c.post("/api/v1.5/claims", json=body).json()["claim_id"] == claim["claim_id"]
    body["fields"]["amount"] = "610.00"
    assert c.post("/api/v1.5/claims", json=body).status_code == 409
    login(c, "finance")
    key = str(uuid4())
    approved = action(c, claim, "APPROVE", key=key)
    assert approved.status_code == 200, approved.text
    assert approved.json()["stage"] == "APPROVED"
    assert approved.json()["ai_result"] == claim["ai_result"]
    assert action(c, claim, "APPROVE", key=key).status_code == 200
    assert repo.erp_write_count(claim["claim_id"]) == 1
    assert action(c, claim, "RETURN").status_code == 409


def test_hard_block_cannot_be_approved_but_can_be_rejected(portal):
    c, _, repo, _ = portal
    login(c)
    claim, _ = create(c, amount="900.00")
    assert claim["stage"] == "FINANCE_REVIEW" and claim["ai_result"]["decision"] == "REJECT"
    login(c, "finance")
    assert action(c, claim, "APPROVE").status_code == 409
    response = action(c, claim, "REJECT", reason="住宿超限，请按企业制度申报")
    assert response.status_code == 200, response.text
    assert response.json()["stage"] == "REJECTED"
    assert response.json()["reason"] == "住宿超限，请按企业制度申报"
    assert repo.erp_write_count(claim["claim_id"]) == 0


def test_return_same_business_id_new_run_version_and_old_resume_blocked(portal):
    c, app, repo, store = portal
    login(c)
    claim, _ = create(c)
    old_run = claim["claim"]["active_run_id"]
    login(c, "finance")
    returned = action(c, claim, "RETURN", reason="请补充报销事项").json()
    assert returned["stage"] == "RETURNED"
    assert action(c, returned, "APPROVE").status_code == 409
    login(c)
    body = {
        "fields": fields(purpose="拜访合成客户，补齐报销事项"),
        "idempotency_key": str(uuid4()),
        "expected_revision": returned["revision"],
    }
    result = c.post(f"/api/v1.5/claims/{claim['claim_id']}/resubmit", json=body)
    assert result.status_code == 202, result.text
    fresh = c.get("/api/v1.5/claims/" + claim["claim_id"]).json()
    assert fresh["claim_id"] == claim["claim_id"] and fresh["version"] == 2
    assert fresh["stage"] == "FINANCE_REVIEW" and fresh["claim"]["active_run_id"] != old_run
    assert fresh["history"][0]["finance_reason"] == "请补充报销事项"
    assert fresh["history"][0]["fields"]["purpose"] != fresh["history"][1]["fields"]["purpose"]
    assert (
        store.db.execute(
            "SELECT count(*) FROM runs WHERE claim_id=?", (claim["claim_id"],)
        ).fetchone()[0]
        == 2
    )
    assert repo.run_context(claim["claim_id"], old_run)["superseded"]
    assert c.post(f"/api/v1.5/claims/{claim['claim_id']}/resubmit", json=body).status_code == 202
    login(c, "finance")
    assert action(c, claim, "APPROVE").status_code == 409
    assert action(c, fresh, "APPROVE").json()["stage"] == "APPROVED"
    assert repo.erp_write_count(claim["claim_id"]) == 1


@pytest.mark.parametrize("decision", ["RETURN", "REJECT"])
def test_negative_actions_require_visible_reasons(portal, decision):
    c, _, _, _ = portal
    login(c)
    claim, _ = create(c)
    login(c, "finance")
    assert action(c, claim, decision, reason="   ").status_code == 422


def test_admin_creates_company_bound_account_disables_revokes_sessions(portal):
    c, _, _, store = portal
    login(c, "admin")
    body = dict(
        username="new.employee",
        password=PASSWORD,
        name="新增合成员工",
        department="研发",
        position="工程师",
        role="EMPLOYEE",
    )
    created = c.post("/api/v1.5/users", json=body)
    assert created.status_code == 201 and created.json()["company_id"] == "DEMO-CN"
    uid = created.json()["id"]
    assert c.post("/api/v1.5/users", json={**body, "company_id": "DEMO-B"}).status_code == 422
    assert c.post("/api/v1.5/users", json=body).status_code == 409
    login(c, "new.employee")
    token = c.cookies.get(COOKIE)
    login(c, "admin")
    assert c.post(f"/api/v1.5/users/{uid}/enabled", json={"enabled": False}).status_code == 200
    c.cookies.clear()
    c.cookies.set(COOKIE, token, path="/api/v1.5")
    assert c.get("/api/v1.5/auth/me").status_code == 401
    assert login(c, "new.employee").status_code == 401


def test_invoice_draft_ownership_without_shared_capability(portal):
    c, _, _, store = portal
    login(c)
    draft_id = str(uuid4())
    result = c.post("/api/v1.5/invoices/drafts", json={"draft_id": draft_id})
    assert result.status_code == 200
    assert "token" not in result.json()
    assert c.post(f"/api/v1.5/invoices/drafts/{draft_id}/read", json={}).status_code == 200
    login(c, "other")
    assert c.post(f"/api/v1.5/invoices/drafts/{draft_id}/read", json={}).status_code == 404
    assert c.post("/api/v1.5/invoices/drafts", json={"draft_id": draft_id}).status_code == 404
    login(c, "admin")
    assert c.post(f"/api/v1.5/invoices/drafts/{draft_id}/read", json={}).status_code == 403


def test_invoice_ocr_retry_keeps_owner_and_role_boundaries(portal):
    from pathlib import Path

    c, _, _, _ = portal
    login(c)
    did = str(uuid4())
    base = f"/api/v1.5/invoices/drafts/{did}"
    assert c.post("/api/v1.5/invoices/drafts", json={"draft_id": did}).status_code == 200
    uploaded = c.post(base + "/attachments", files={"file": (
        "synthetic.pdf", Path("tests/fixtures/invoices/text-invoice.pdf").read_bytes(),
        "application/pdf")})
    aid = uploaded.json()["attachment_id"]
    retry = base + f"/attachments/{aid}/ocr-retry"
    assert c.post(retry, json={}).status_code == 200
    for username, company, expected in (("other", "DEMO-CN", 404),
                                        ("employee", "DEMO-B", 404),
                                        ("finance", "DEMO-CN", 403)):
        login(c, username, company)
        assert c.post(retry, json={"token": str(uuid4())}).status_code == expected


def test_erp_recovery_is_finance_only_and_idempotent(tmp_path):
    repo = MockClaimRepository(store_path=str(tmp_path / "erp.sqlite3"))
    erp = MockERP(repo, failure_mode=FailureMode.SUCCESS_THEN_RESPONSE_LOST)
    app = create_app(
        Settings(
            app_env="test", enterprise_enabled=True, enterprise_cookie_secure=False, _env_file=None
        ),
        repo,
        erp=erp,
        retry_policies={"erp": RetryPolicy(max_attempts=1)},
    )
    store = app.state.enterprise_app.state.store
    for username, role in (("employee", "EMPLOYEE"), ("finance", "FINANCE")):
        store.create_user(
            "DEMO-CN",
            CreateUser(
                username=username,
                password=PASSWORD,
                name="新申请人",
                department="华南销售部",
                position="客户经理",
                role=role,
            ),
        )
    with TestClient(app) as c:
        login(c)
        claim, _ = create(c)
        login(c, "finance")
        result = action(c, claim, "APPROVE")
        assert result.status_code == 200, result.text
        waiting = result.json()
        assert waiting["stage"] == "ERP_SYNC_PENDING"
        assert repo.erp_write_count(claim["claim_id"]) == 1
        login(c)
        request = dict(expected_revision=waiting["revision"], idempotency_key=str(uuid4()))
        path = f"/api/v1.5/claims/{claim['claim_id']}/retry"
        assert c.post(path, json=request).status_code == 403
        erp.faults.mode = FailureMode.NONE
        login(c, "finance")
        assert c.post(path, json=request).json()["stage"] == "APPROVED"
        assert c.post(path, json=request).status_code == 200
        assert repo.erp_write_count(claim["claim_id"]) == 1
    repo.close()


def test_two_finance_actions_cannot_decide_twice(portal):
    from concurrent.futures import ThreadPoolExecutor

    c, app, repo, _ = portal
    login(c)
    claim, _ = create(c)
    login(c, "finance")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: action(c, claim, "APPROVE"), range(2)))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert repo.erp_write_count(claim["claim_id"]) == 1


def test_durable_pending_finance_intent_survives_process_restart(portal):
    c, app, repo, store = portal
    login(c)
    claim, _ = create(c)
    login(c, "finance")
    original = app.state.audit_service.resume

    async def interrupted(*args, **kwargs):
        raise RuntimeError("synthetic-process-interruption")

    app.state.audit_service.resume = interrupted
    with TestClient(app, raise_server_exceptions=False) as failure_client:
        login(failure_client, "finance")
        assert action(failure_client, claim, "APPROVE").status_code == 500
    app.state.audit_service.resume = original
    waiting = c.get("/api/v1.5/claims/" + claim["claim_id"]).json()
    assert waiting["stage"] == "FINANCE_APPLYING"
    assert repo.erp_write_count(claim["claim_id"]) == 0
    recreated = create_app(
        Settings(
            app_env="test", enterprise_enabled=True, enterprise_cookie_secure=False, _env_file=None
        ),
        repo,
    )
    with TestClient(recreated) as restarted:
        login(restarted, "finance")
        result = restarted.post(
            "/api/v1.5/claims/" + claim["claim_id"] + "/retry",
            json={"expected_revision": waiting["revision"], "idempotency_key": str(uuid4())},
        )
        assert result.status_code == 200, result.text
        assert result.json()["stage"] == "APPROVED"
        assert repo.erp_write_count(claim["claim_id"]) == 1


def test_invoice_real_text_ocr_return_rechecks_and_preserves_prior_confirmation(portal):
    from pathlib import Path

    from test_invoices import FIELDS

    c, _, repo, _ = portal
    login(c)
    draft_id = str(uuid4())
    base = "/api/v1.5/invoices/drafts/" + draft_id
    assert c.post("/api/v1.5/invoices/drafts", json={"draft_id": draft_id}).status_code == 200
    attachment = c.post(
        base + "/attachments",
        files={
            "file": (
                "synthetic.pdf",
                Path("tests/fixtures/invoices/text-invoice.pdf").read_bytes(),
                "application/pdf",
            )
        },
    )
    assert attachment.status_code == 200, attachment.text
    aid = attachment.json()["attachment_id"]
    ocr = c.post(base + "/attachments/" + aid + "/ocr", json={})
    assert ocr.status_code == 200, ocr.text
    assert ocr.json()["ocr"]["provider"] != "mock-test"
    invoice = dict(
        draft_id=draft_id,
        attachment_id=aid,
        confirmed=True,
        confirmed_fields=FIELDS,
        discrepancy_reason="合成发票本人核对",
    )
    request = dict(
        fields=fields(seller=FIELDS["seller_name"]), invoice=invoice, idempotency_key=str(uuid4())
    )
    result = c.post("/api/v1.5/claims", json=request)
    assert result.status_code == 202, result.text
    cid = result.json()["claim_id"]
    claim = c.get("/api/v1.5/claims/" + cid).json()
    original = claim["invoice_evidence"]
    for username, company in (("other", "DEMO-CN"), ("finance", "DEMO-B")):
        login(c, username, company)
        for suffix in ("invoice-preview", "invoice-original"):
            assert c.get(f"/api/v1.5/claims/{cid}/{suffix}").status_code == 404
    login(c, "finance")
    preview = c.get(f"/api/v1.5/claims/{cid}/invoice-preview")
    assert preview.status_code == 200 and preview.content.startswith(b"\x89PNG")
    raw = c.get(f"/api/v1.5/claims/{cid}/invoice-original")
    assert raw.status_code == 200
    assert raw.content == Path("tests/fixtures/invoices/text-invoice.pdf").read_bytes()
    assert "attachment" in raw.headers["content-disposition"]
    returned = action(c, claim, "RETURN", reason="请核对报销金额").json()
    login(c)
    edit = dict(
        fields=fields(amount="610.00", seller=FIELDS["seller_name"]),
        invoice={**invoice, "discrepancy_reason": ""},
        expected_revision=returned["revision"],
        idempotency_key=str(uuid4()),
    )
    assert c.post("/api/v1.5/claims/" + cid + "/resubmit", json=edit).status_code == 422
    edit["invoice"]["discrepancy_reason"] = "本人确认申报包含额外费用，供财务核实"
    assert c.post("/api/v1.5/claims/" + cid + "/resubmit", json=edit).status_code == 202
    revised = c.get("/api/v1.5/claims/" + cid).json()
    assert "AMOUNT_MISMATCH" in {w["code"] for w in revised["invoice_evidence"]["warnings"]}
    assert revised["history"][0]["invoice_evidence"] == original
    assert revised["invoice_evidence"]["attachment"]["ocr"] == original["attachment"]["ocr"]
    assert revised["stage"] == "FINANCE_REVIEW"
    assert repo.erp_write_count(cid) == 0
    assert c.get(f"/api/v1.5/claims/{cid}/invoice-original?version=1").content == raw.content


def test_restart_reconciles_unfinished_ai_without_claiming_approval(portal):
    c, app, repo, store = portal
    login(c)
    claim, _ = create(c)
    cid = claim["claim_id"]
    with store.db:
        store.db.execute(
            "UPDATE enterprise_claims SET stage='AI_AUDITING' WHERE claim_id=?", (cid,)
        )
    recreated = create_app(
        Settings(
            app_env="test", enterprise_enabled=True, enterprise_cookie_secure=False, _env_file=None
        ),
        repo,
    )
    with TestClient(recreated) as restarted:
        login(restarted)
        result = restarted.get("/api/v1.5/claims/" + cid).json()
        assert result["stage"] == "FINANCE_REVIEW"
        assert repo.erp_write_count(cid) == 0


@pytest.mark.parametrize(
    "origin,fetch_site,expected",
    [
        ("http://testserver", "same-origin", 200),
        ("http://testserver.evil.invalid", "same-origin", 403),
        ("https://untrusted.invalid", "cross-site", 403),
        ("http://testserver", "cross-site", 403),
        ("null", "same-origin", 403),
    ],
)
def test_same_origin_login_without_changing_cors_allowlist(portal, origin, fetch_site, expected):
    c = portal[0]
    response = c.post(
        "/api/v1.5/auth/login",
        headers={"Origin": origin, "Sec-Fetch-Site": fetch_site},
        json={"company_id": "DEMO-CN", "username": "employee", "password": PASSWORD,
              **captcha(c)},
    )
    assert response.status_code == expected


def test_enterprise_cookie_cors_login_and_session(portal):
    c, _, _, _ = portal
    origin = "http://127.0.0.1:3000"
    for path in ("/api/v1.5/auth/login", "/api/v1.5/policies/list"):
        response = c.options(
            path,
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        assert response.headers["access-control-allow-credentials"] == "true"
    response = c.post(
        "/api/v1.5/auth/login",
        headers={"Origin": origin},
        json={"company_id": "DEMO-CN", "username": "admin", "password": PASSWORD,
              **captcha(c)},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"
    response = c.get("/api/v1.5/auth/me", headers={"Origin": origin})
    assert response.status_code == 200 and response.json()["user"]["role"] == "ADMIN"
    assert response.headers["access-control-allow-credentials"] == "true"
    untrusted = "https://untrusted.invalid"
    response = c.options(
        "/api/v1.5/policies/list",
        headers={"Origin": untrusted, "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
    response = c.post("/api/v1.5/policies/list", headers={"Origin": untrusted}, json={})
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers
