"""Protect runtime binding and exercise authored V4 over isolated real workflows."""

import json
import sqlite3
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_current_data_advisory import current_data as current_data
from test_employee_hotel import declaration
from test_employee_hotel import employee_portal as employee_portal
from test_p4a_postgres import hotel_portal as hotel_portal
from test_policies_postgres import policy_pg as policy_pg

from apps.api.compliance_api import install_compliance_api
from apps.api.config import Settings
from apps.api.main import create_app
from apps.api.repository import MockClaimRepository
from domain.enterprise import CreateUser
from rag.retrieval.pgvector import PgPolicyCatalog, PgVectorPolicyRetriever
from scripts.p0_runtime import OfflineV4, binding, install_identity, local_settings, preflight
from tests.p4b_fix2_support import invoice, login
from tests.test_enterprise import PASSWORD
from tests.test_enterprise import login as enterprise_login
from tests.test_policies import META


@pytest.fixture
def runtime_binding(tmp_path):
    gen = tmp_path / "generation"
    gen.mkdir()
    (gen / "fixture-summary.json").write_text("{}", encoding="utf-8")
    (gen / "database-url.txt").write_text(
        "postgresql+psycopg://expense@127.0.0.1:55432/fde_m0_test_finance_browser_p0",
        encoding="utf-8",
    )
    (gen / "p0-rehearsal.json").write_text('{"isolated":true}', encoding="utf-8")
    with sqlite3.connect(gen / "portal.sqlite3") as db:
        db.execute("CREATE TABLE enterprise_claims(stage TEXT)")
    tess = tmp_path / "tessdata"
    tess.mkdir()
    for lang in ("chi_sim", "eng"):
        (tess / (lang + ".traineddata")).touch()
    data = dict(mode="rehearsal", generation=str(gen), ocr_tessdata=str(tess))
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, gen, data


def test_binding_requires_an_explicit_isolated_marker(runtime_binding):
    path, gen, _ = runtime_binding
    (gen / "p0-rehearsal.json").unlink()
    with pytest.raises(ValueError, match="isolated rehearsal"):
        binding(path)


def test_business_date_is_binding_specific_and_keeps_existing_rehearsal_default(runtime_binding):
    path, _, data = runtime_binding
    assert binding(path)[0]["business_date"] == "2027-01-05"
    data["business_date"] = "2026-11-03"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert binding(path)[0]["business_date"] == "2026-11-03"


@pytest.mark.parametrize("value", ["2026-02-30", "20261103", "2026-11-03T00:00:00", None])
def test_binding_rejects_invalid_business_date(runtime_binding, value):
    path, _, data = runtime_binding
    data["business_date"] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        binding(path)


def test_rehearsal_rejects_a_live_profile(runtime_binding):
    path, gen, _ = runtime_binding
    (gen / "deepseek-live.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="must not contain a live"):
        binding(path)


def test_preserve_cannot_silently_fall_back_to_mock(runtime_binding):
    path, _, data = runtime_binding
    data["mode"] = "preserve"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="no Mock fallback"):
        binding(path)


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://expense@remote:55432/fde_m0_test_finance_browser_p0",
    "postgresql+psycopg://expense@localhost:55432/production",
])
def test_binding_rejects_unapproved_database_targets(runtime_binding, url):
    path, gen, _ = runtime_binding
    (gen / "database-url.txt").write_text(url, encoding="utf-8")
    with pytest.raises(ValueError, match="Allowlisted loopback"):
        binding(path)


@pytest.mark.parametrize("stage", ["AI_AUDITING", "FINANCE_APPLYING", "ERP_SYNC_PENDING"])
def test_startup_refuses_automatic_operation_recovery(
    runtime_binding, tmp_path, monkeypatch, stage,
):
    import scripts.p0_runtime as runtime

    path, gen, _ = runtime_binding
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    with sqlite3.connect(gen / "portal.sqlite3") as db:
        db.execute("INSERT INTO enterprise_claims VALUES(?)", (stage,))
    with pytest.raises(ValueError, match="Pending operations"):
        preflight(path)


def test_startup_launches_both_services_without_requesting_a_protected_version(
    runtime_binding, tmp_path, monkeypatch,
):
    import httpx

    import scripts.p0_runtime as runtime

    path, _, data = runtime_binding
    data["business_date"] = "2026-11-03"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    monkeypatch.setattr(runtime, "OUT", tmp_path / "output")
    requests, processes, environments = [], [], []

    def get(url, **kwargs):
        requests.append(url)
        return SimpleNamespace(status_code=401 if url.endswith("p0-runtime") else 200)

    def spawn(command, **kwargs):
        processes.append(command)
        environments.append(kwargs["env"].copy())
        return SimpleNamespace(pid=1000 + len(processes), poll=lambda: None)

    class FreePort:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def connect_ex(self, address):
            return 111

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    monkeypatch.setattr(runtime.socket, "socket", FreePort)
    runtime.start(path)
    assert requests == [runtime.API + "/health", runtime.WEB]
    assert len(processes) == 2
    assert environments[1]["VITE_M6_BUSINESS_DATE"] == "2026-11-03"


def test_public_health_does_not_make_runtime_identity_public(current_data):
    env, _ = current_data
    client, app = env[:2]
    expected = dict(source_sha="synthetic-frozen-source", mode="rehearsal")
    install_identity(app, expected)
    assert client.post("/api/v1.5/auth/logout").status_code == 200
    assert client.get("/health").status_code == 200
    denied = client.get("/api/v1.5/p0-runtime")
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "LOGIN_REQUIRED"
    login(client, "finance")
    assert client.get("/api/v1.5/auth/me").status_code == 200
    response = client.get("/api/v1.5/p0-runtime")
    assert response.status_code == 200
    assert response.json() == expected


def test_runtime_policy_draft_read_confirm_ingest_and_access_boundaries(policy_pg, tmp_path):
    from fastapi.testclient import TestClient

    engine, draft, embedding, _ = policy_pg
    settings = local_settings(str(tmp_path))
    repo = MockClaimRepository(store_path=str(tmp_path / "runtime-policy.sqlite3"))
    app = create_app(
        settings, repo, policy_store=draft, catalog=PgPolicyCatalog(engine),
        retriever=PgVectorPolicyRetriever(engine, embedding), compliance_provider=OfflineV4(),
    )
    store = app.state.enterprise_app.state.store
    for company, username, role in [
        ("DEMO-CN", "admin", "ADMIN"), ("DEMO-CN", "employee", "EMPLOYEE"),
        ("DEMO-CN", "finance", "FINANCE"), ("DEMO-B", "admin", "ADMIN"),
    ]:
        store.create_user(company, CreateUser(
            username=username, password=PASSWORD, name="合成测试人员", department="测试部",
            position="测试员", role=role,
        ))
    try:
        with TestClient(app) as client:
            assert client.post("/api/v1.5/policies/list", json={}).status_code == 401
            login(client, "admin")
            uploaded = client.post("/api/v1.5/policies/upload", files={"file": (
                "runtime-policy.docx",
                Path("tests/fixtures/policies/draft-800.docx").read_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )})
            assert uploaded.status_code == 200, uploaded.text
            uid = uploaded.json()["upload_id"]
            base = f"/api/v1.5/policies/{uid}"
            read = client.post(base + "/read", json={})
            assert read.status_code == 200, read.text
            assert read.json()["state"] == "PARSED"
            confirmed = client.post(base + "/confirm", json={
                "confirmed": True, "metadata": META,
            })
            assert confirmed.status_code == 200, confirmed.text
            assert confirmed.json()["state"] == "CONFIRMED"
            assert client.post(base + "/ingest", json={
                "remote_authorized": True,
            }).status_code == 403
            indexed = client.post(base + "/ingest", json={"remote_authorized": False})
            assert indexed.status_code == 200, indexed.text
            assert indexed.json()["state"] == "INDEXED"
            secret = settings.policy_demo_token.get_secret_value()
            assert secret not in uploaded.text + read.text + confirmed.text + indexed.text
            assert client.post("/api/v1.3/policies/list", json={
                "token": secret,
            }).status_code == 403
            for role in ("employee", "finance"):
                login(client, role)
                assert client.post(base + "/read", json={}).status_code == 403
            client.cookies.clear()
            other_admin = enterprise_login(client, "admin", "DEMO-B")
            assert other_admin.status_code == 200, other_admin.text
            assert other_admin.json()["user"]["company_id"] == "DEMO-B"
            assert client.post(base + "/read", json={}).status_code == 404
    finally:
        repo.close()


@pytest.mark.parametrize("amount,notes,status,decision", [
    ("680.00", "合成正常申请", "SUCCEEDED", "PASS"),
    ("1200.00", "合成超限申请", "SUCCEEDED", "FAIL"),
    ("680.00", "SYNTHETIC_P0_AI_FAILURE 合成非法JSON", "FAILED", "PASS"),
])
def test_rehearsal_v4_runs_actual_workflow_without_external_calls(
    current_data, amount, notes, status, decision,
):
    env, _ = current_data
    client, app, repo = env[:3]
    install_compliance_api(
        app.state.enterprise_app,
        Settings(_env_file=None, app_env="test", enterprise_enabled=True, p4a_enabled=True,
                 p4b_enabled=True, p4b_advisory_enabled=True,
                 p4b_advisory_protocol="p4b.advisory.4"),
        repo, app.state.audit_service, OfflineV4(), business_clock=lambda: date(2027, 1, 5),
    )
    body = declaration()
    body.update(amount=amount, notes=notes, check_out="2026-11-03",
                seller="Synthetic Shenzhen Hotel", invoice=invoice(client, dict(amount=amount)))
    created = client.post("/api/v1.5/p4a/v2/claims", json=body)
    assert created.status_code == 202, created.text
    login(client, "finance")
    detail = client.get("/api/v1.5/claims/" + created.json()["claim_id"]).json()
    report = detail["advisory_analysis"]
    assert detail["stage"] == "FINANCE_REVIEW"
    assert report["ai_advisory_status"] == status, report["validation_errors"]
    assert report["declared_amount_check"]["decision"] == decision
    assert report["declared_amount_check"]["total_limit"] == "800.00"
    assert report["model"] == "mock-authored-advisory-v4"
    assert len(report["evidence_selector_attempts"]) == 1
    selector = report["evidence_selector"]
    assert report["evidence_selector_attempts"][0] == selector
    assert len(report["provider_calls"]) == 1
    call = report["provider_calls"][0]
    assert call["evidence_snapshot_id"] == selector["evidence_snapshot_id"]
    if status == "FAILED":
        assert report["original_output"] is None
        assert call["error_code"] == "INVALID_COMPLIANCE_OUTPUT"
        assert call["validation_errors"][0]["type"] == "json_invalid"
        assert call["validation_errors"][0]["loc"] == ["content"]
        assert all(e["code"] != "EVIDENCE_SELECTOR_NOT_PINNED"
                   for e in report["validation_errors"])
        assert json.loads(report["raw_responses"][0])["choices"][0]["message"][
            "content"] == "{invalid-json"
    else:
        assert report["original_output"]["evidence_snapshot_id"] == selector[
            "evidence_snapshot_id"]
        assert report["validation_errors"] == []
    assert repo.erp_write_count(detail["claim_id"]) == 0
