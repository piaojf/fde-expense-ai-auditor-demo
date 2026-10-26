"""Real default-environment Demo Stability gate; no business/contract changes."""

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from smoke_m1 import open_loopback, request, start, stop

ROOT = Path(__file__).resolve().parents[1]
C4 = "CLM-2026-00004"
ORIGINS = [
    "http://localhost:3000", "http://localhost:5173",
    "http://127.0.0.1:3000", "http://127.0.0.1:5173",
]


def check_default_http(base):
    report = {}
    code, health = request(base, "/health")
    assert code == 200
    report["health"] = health
    _, claim = request(base, f"/claims/{C4}")
    values = [claim["employee"]["name"], claim["employee"]["department"], claim["expense"]["city"]]
    assert values == ["李娜", "华南市场部", "深圳"]
    assert claim["status"] == "NEW"
    report["chinese_values"] = values
    report["cors"] = {}
    for origin in ORIGINS:
        preflight = urllib.request.Request(
            base + f"/claims/{C4}/audit", method="OPTIONS", headers={
                "Origin": origin, "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
        with open_loopback(preflight, timeout=5) as response:
            assert response.status == 200
            assert response.headers["Access-Control-Allow-Origin"] == origin
        report["cors"][origin] = "PASS"
    _, schema = request(base, "/openapi.json")
    assert schema == json.loads((ROOT / "docs/openapi.json").read_text(encoding="utf-8"))
    report["contract_unchanged"] = True
    return report


def check_backend_flow(base):
    _, m1 = request(base, "/claims/CLM-2026-00001/audit", {"idempotency_key": "gate-m1"})
    assert m1["status"] == "COMPLETED" and m1["decision_result"]["decision"] == "APPROVE"
    _, pre = request(base, f"/claims/{C4}/audit", {"idempotency_key": "gate-m2"})
    result = pre["decision_result"]
    assert pre["status"] == "PENDING_HUMAN"
    assert result["hard_gate_passed"] and result["decision"] == "HUMAN_REVIEW"
    assert result["risk_level"] == "MEDIUM"
    assert all(r["outcome"] == "PASS" for r in result["rule_results"])
    assert [p["version"] for p in result["policy_evidence"]] == ["2026"]
    _, review = request(base, f"/claims/{C4}/human-review", {
        "run_id": pre["run_id"], "expected_revision": pre["revision"], "decision": "APPROVE",
        "reviewer_id": "finance_reviewer_01", "comment": "航班取消证明有效，批准特殊报销。",
        "idempotency_key": "gate-review",
    })
    _, final = request(base, f"/claims/{C4}/resume", {
        "run_id": pre["run_id"], "expected_revision": review["revision"],
        "idempotency_key": "gate-resume",
    })
    _, detail = request(base, f"/claims/{C4}")
    _, log = request(base, f"/claims/{C4}/audit-log")
    assert final["status"] == "COMPLETED"
    assert final["decision_result"]["decision"] == "APPROVE"
    assert final["decision_result"]["risk_level"] == "LOW"
    assert detail["erp_status"] == "SUCCEEDED" and log["total"] == 16
    _, block = request(base, "/claims/CLM-2026-001/audit", {"idempotency_key": "gate-block"})
    rules = block["decision_result"]["rule_results"]
    # Stock fixture is 800; the exact original 780 scenario is covered by test_m2.
    assert any(r["actual"] == "800.00" and r["limit"] == "700.00"
               and r["outcome"] == "FAIL" and r["severity"] == "BLOCK" for r in rules)
    try:
        request(base, "/claims/CLM-2026-001/human-review", {
            "run_id": block["run_id"], "expected_revision": block["revision"],
            "decision": "APPROVE", "reviewer_id": "finance_reviewer_01",
            "comment": "Attempt hard block override", "idempotency_key": "gate-override",
        })
    except urllib.error.HTTPError as exc:
        assert exc.code == 409
    else:
        raise AssertionError("Hard BLOCK was overridden")
    print("M1=PASS; M2=PASS; GOVERNANCE=HTTP409; POLICY=2026; AUDIT_EVENTS=16")
    return {"m1": m1, "m2_pre": pre, "m2_final": final, "detail": detail,
            "audit_log": log, "governance": {"amount": "800.00", "limit": "700.00", "http": 409}}


def browser_gate(temp, environment, output):
    # The unchanged script writes screenshots to .workbuddy relative to itself.
    # Execute an exact copy in a disposable checkout, never in the protected workspace.
    sandbox = temp / "browser"
    web = sandbox / "apps/web"
    shutil.copytree(ROOT / "apps/web", web)
    script = web / "scripts/e2e-real-api.mjs"
    original = ROOT / "apps/web/scripts/e2e-real-api.mjs"
    assert script.read_bytes() == original.read_bytes()
    for port in (8000, 5173):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    backend_log = (output.parent / "browser-backend.log").open("wb")
    vite_log = (output.parent / "browser-vite.log").open("wb")
    backend = vite = None
    try:
        backend = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "apps.api.main:app", "--host", "127.0.0.1",
             "--port", "8000"], cwd=sandbox, env=environment,
            stdout=backend_log, stderr=subprocess.STDOUT,
        )
        vite = subprocess.Popen(
            ["node", "node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", "5173",
             "--strictPort"], cwd=web, env=environment, stdout=vite_log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            assert backend.poll() is None and vite.poll() is None, "Server exited during startup"
            try:
                request("http://127.0.0.1:8000", "/health")
                with open_loopback("http://localhost:5173", timeout=1) as response:
                    assert response.status == 200
                break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.1)
        else:
            raise AssertionError("Browser server startup timed out")
        initial = check_default_http("http://127.0.0.1:8000")
        completed = subprocess.run(
            ["node", str(script)], cwd=sandbox, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            timeout=180,
        )
        (output.parent / "browser-e2e.log").write_text(completed.stdout, encoding="utf-8")
        assert completed.returncode == 0, completed.stdout
        assert "E2E PASS" in completed.stdout
        assert "控制台错误：无" in completed.stdout and "404 资源：无" in completed.stdout
        _, detail = request("http://127.0.0.1:8000", f"/claims/{C4}")
        _, log = request("http://127.0.0.1:8000", f"/claims/{C4}/audit-log")
        assert detail["status"] == "COMPLETED" and detail["erp_status"] == "SUCCEEDED"
        assert detail["decision_result"]["decision"] == "APPROVE"
        assert detail["decision_result"]["risk_level"] == "LOW" and log["total"] == 16
        shutil.copytree(sandbox / ".workbuddy/verify/e2e", output.parent / "browser-screenshots",
                        dirs_exist_ok=True)
        print("REAL_BROWSER_E2E=PASS; Console Errors=0; CORS Errors=0; 404 Errors=0")
        return {"initial": initial, "detail": detail, "audit_log": log,
                "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                "script_command": subprocess.list2cmdline(["node", str(script)]),
                "literal_output": completed.stdout, "exit_status": completed.returncode,
                "console_errors": 0, "cors_errors": 0, "http404": 0}
    finally:
        for process in (vite, backend):
            if process is not None:
                stop(process)
        backend_log.close()
        vite_log.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    for key in ("PYTHONUTF8", "CORS_ORIGINS", "MOCK_STORE_PATH", "APP_ENV", "LLM_PROVIDER"):
        environment.pop(key, None)
    environment["PYTHONPATH"] = str(ROOT)
    report = {"PYTHONUTF8": None, "CORS_ORIGINS": None, "MOCK_STORE_PATH": "default"}
    with tempfile.TemporaryDirectory(prefix="fde-demo-stability-") as directory:
        temp = Path(directory)
        # Default relative .m1-runtime path in a fresh working directory, same README command.
        process, base = start(temp, environment)
        try:
            report["default"] = check_default_http(base)
            report["backend"] = check_backend_flow(base)
        finally:
            stop(process)
        if args.browser:
            report["browser"] = browser_gate(temp, environment, output)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("DEFAULT_ENV=PASS; CHINESE_PERSISTENCE=PASS; CORS_DEFAULTS=PASS; CONTRACT_UNCHANGED=YES")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
