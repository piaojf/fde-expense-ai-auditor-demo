"""Demo stability only: explicit text boundaries and unchanged M1/M2 behavior."""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.config import Settings
from apps.api.main import create_app
from scripts.smoke_m1 import start

ROOT = Path(__file__).resolve().parents[1]
ORIGINS = [
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
]


@pytest.mark.parametrize("origin", ORIGINS)
def test_default_cors_preflight_and_response(monkeypatch, origin):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    with TestClient(create_app(Settings(app_env="test", _env_file=None))) as client:
        response = client.options(
            "/claims/CLM-2026-00004/audit",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        response = client.get("/health", headers={"Origin": origin})
        assert response.headers["access-control-allow-origin"] == origin
        assert "access-control-allow-credentials" not in response.headers


def test_cors_environment_override_replaces_defaults(monkeypatch):
    override = "https://expense.example.test"
    monkeypatch.setenv("CORS_ORIGINS", json.dumps([override]))
    settings = Settings(app_env="test", _env_file=None)
    assert settings.cors_origins == [override]
    with TestClient(create_app(settings)) as client:
        for origin, code in [(override, 200), *[(o, 400) for o in ORIGINS]]:
            response = client.options(
                "/claims/CLM-2026-00004/audit",
                headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
            )
            assert response.status_code == code
            if code == 400:
                assert "access-control-allow-origin" not in response.headers


def test_utf8_subprocess_startup_diagnostics_without_pythonutf8(tmp_path):
    module = tmp_path / "apps/api/main.py"
    module.parent.mkdir(parents=True)
    for path in [tmp_path / "apps/__init__.py", module.parent / "__init__.py"]:
        path.write_text("", encoding="utf-8")
    module.write_text('raise RuntimeError("深圳：启动诊断")\n', encoding="utf-8")
    environment = os.environ.copy()
    environment.pop("PYTHONUTF8", None)
    environment.pop("PYTHONPATH", None)
    with pytest.raises(RuntimeError, match="深圳：启动诊断"):
        start(tmp_path, environment)


def test_project_text_io_has_explicit_utf8():
    """Prevent silent dependence on Windows ANSI file/pipe decoding."""
    violations = []
    folders = ["agent", "apps/api", "database", "domain", "evals", "integrations",
               "rag", "rules", "scripts", "tests"]
    for folder in folders:
        for path in (ROOT / folder).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                keywords = {item.arg: item.value for item in node.keywords}
                mode = keywords.get("mode")
                if mode is None and name == "open":
                    position = 0 if isinstance(node.func, ast.Attribute) else 1
                    if len(node.args) > position:
                        mode = node.args[position]
                binary = isinstance(mode, ast.Constant) and "b" in str(mode.value)
                text_pipe = any(
                    isinstance(keywords.get(k), ast.Constant) and keywords[k].value is True
                    for k in ("text", "universal_newlines")
                )
                if name in ("read_text", "write_text") or (name == "open" and not binary) \
                        or (name in ("Popen", "run", "check_output") and text_pipe):
                    encoding = keywords.get("encoding")
                    if not isinstance(encoding, ast.Constant) or encoding.value != "utf-8":
                        violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert violations == []
    assert Settings.model_config["env_file_encoding"] == "utf-8"


def test_non_utf8_child_seed_persistence_policy_and_m2(tmp_path):
    # ASCII source passed as argv, never Chinese source piped through a shell.
    # The worker uses the real UTF-8 fixture loader, SQLite storage and M2 service.
    worker = r'''
import json, locale, os, sys
from pathlib import Path
from fastapi.testclient import TestClient
from apps.api.config import Settings
from apps.api.main import create_app
from apps.api.repository import MockClaimRepository
from rag.retrieval.memory import PolicyCatalog
assert "PYTHONUTF8" not in os.environ
assert sys.flags.utf8_mode == 0
expected = ("\u674e\u5a1c", "\u534e\u5357\u5e02\u573a\u90e8", "\u6df1\u5733")
store = str(Path(sys.argv[1]) / "seed.sqlite3")
repo = MockClaimRepository(store_path=store)
claim = repo.get("CLM-2026-00004")
assert (claim.employee.name, claim.employee.department, claim.expense.city) == expected
repo.close()
repo = MockClaimRepository(store_path=store)
claim = repo.get("CLM-2026-00004")
assert (claim.employee.name, claim.employee.department, claim.expense.city) == expected
policy = PolicyCatalog().resolve(legal_entity=claim.employee.legal_entity,
    category=claim.expense.category, city=claim.expense.city,
    incurred_on=claim.expense.incurred_on)
assert policy.version == "2026" and policy.region == expected[2]
repo.close()
settings = Settings(app_env="development", mock_store_path=store, _env_file=None)
with TestClient(create_app(settings)) as client:
    op = client.post("/claims/CLM-2026-00004/audit",
        json={"idempotency_key":"utf8-audit"}).json()
    result = op["decision_result"]
    assert op["status"] == "PENDING_HUMAN"
    assert result["hard_gate_passed"] and result["decision"] == "HUMAN_REVIEW"
    assert result["risk_level"] == "MEDIUM"
    assert all(r["outcome"] == "PASS" for r in result["rule_results"])
    assert [p["version"] for p in result["policy_evidence"]] == ["2026"]
    review = client.post("/claims/CLM-2026-00004/human-review", json={
        "run_id":op["run_id"], "expected_revision":op["revision"],
        "decision":"APPROVE", "reviewer_id":"finance_reviewer_01",
        "comment":"\u822a\u73ed\u53d6\u6d88\u6750\u6599\u6709\u6548",
        "idempotency_key":"utf8-review"})
    assert review.status_code == 200
    final = client.post("/claims/CLM-2026-00004/resume", json={
        "run_id":op["run_id"], "expected_revision":review.json()["revision"],
        "idempotency_key":"utf8-resume"}).json()
    assert final["status"] == "COMPLETED"
    assert final["decision_result"]["decision"] == "APPROVE"
    assert final["decision_result"]["risk_level"] == "LOW"
with TestClient(create_app(settings)) as client:
    detail = client.get("/claims/CLM-2026-00004").json()
    assert (detail["employee"]["name"], detail["employee"]["department"],
            detail["expense"]["city"]) == expected
    assert detail["erp_status"] == "SUCCEEDED"
    assert client.get("/claims/CLM-2026-00004/audit-log").json()["total"] == 16
env_file = Path(sys.argv[1]) / ".env"
env_file.write_text("MOCK_STORE_PATH="+expected[2]+"/"+expected[0]+".sqlite3\n",
                    encoding="utf-8")
assert Settings(_env_file=env_file).mock_store_path == expected[2]+"/"+expected[0]+".sqlite3"
print(json.dumps({"utf8_mode":sys.flags.utf8_mode, "locale":locale.getencoding(),
    "values":expected, "policy":"2026", "status":"COMPLETED", "events":16}))
'''
    environment = os.environ.copy()
    for key in ("PYTHONUTF8", "CORS_ORIGINS", "MOCK_STORE_PATH"):
        environment.pop(key, None)
    completed = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", worker, str(tmp_path)],
        cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["values"] == ["李娜", "华南市场部", "深圳"]
    if sys.platform == "win32":
        assert report["locale"].lower() != "utf-8"
