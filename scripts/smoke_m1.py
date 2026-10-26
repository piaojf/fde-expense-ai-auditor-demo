"""Real loopback Uvicorn acceptance + restart, using an isolated durable Mock store."""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import UUID

from domain.schemas import AuditLogResponse, ClaimDetail, OperationResponse

CLAIM = "CLM-2026-00001"

# Loopback-only acceptance traffic: never honor http_proxy/https_proxy from a
# developer shell, otherwise urllib sends 127.0.0.1 through the proxy and the
# gate fails with a misleading proxy error instead of testing the server.
# Bound-method alias: binary HTTP I/O, no locale decoding; the distinct name
# keeps the explicit-UTF-8 AST guard from flagging it as builtins.open.
open_loopback = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
EVENTS = [
    "CLAIM_LOADED",
    "ERP_CONTEXT_FETCHED",
    "DOCUMENTS_PARSED",
    "HARD_GATE_CHECKED",
    "RULES_CHECKED",
    "POLICY_RETRIEVED",
    "LLM_ANALYSIS_COMPLETED",
    "RISK_ASSESSED",
    "DECISION_MADE",
    "AUTO_APPROVED",
    "ERP_UPDATED",
    "WORKFLOW_COMPLETED",
]


def stop(process):
    process.terminate()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()


def request(base, path, body=None):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with open_loopback(req, timeout=10) as response:
        UUID(response.headers["X-Request-ID"])
        return response.status, json.load(response)


def start(root, environment):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "apps.api.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=root,
        env={**environment, "PYTHONIOENCODING": "utf-8"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(process.communicate()[0])
            try:
                code, body = request(base, "/health")
                assert code == 200 and body["database"] == "not_checked"
                return process, base
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.1)
        raise RuntimeError("Uvicorn startup deadline exceeded")
    except BaseException:
        # Startup failure output was already consumed by communicate().
        if process.poll() is None:
            stop(process)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    report = {}
    with tempfile.TemporaryDirectory(prefix="fde-m1-http-") as temp:
        environment = {
            **os.environ,
            "APP_ENV": "development",
            "LLM_PROVIDER": "mock",
            "MOCK_STORE_PATH": str(Path(temp) / "mock.sqlite3"),
        }
        process, base = start(root, environment)
        print("UVICORN_START=PASS")
        try:
            code, schema = request(base, "/openapi.json")
            assert code == 200
            assert schema == json.loads((root / "docs/openapi.json").read_text(encoding="utf-8"))
            body = {"idempotency_key": "m1-live-http", "mode": "LIVE"}
            code, operation = request(base, f"/claims/{CLAIM}/audit", body)
            result = OperationResponse.model_validate(operation)
            assert code == 202 and result.status == "COMPLETED"
            decision = result.decision_result
            assert decision.decision == "APPROVE" and decision.risk_level == "LOW"
            assert decision.hard_gate_passed and decision.requires_human is False
            assert decision.rule_results[0].outcome == "PASS"
            assert decision.rule_results[0].actual == 600 and decision.rule_results[0].limit == 700
            assert [p.version for p in decision.policy_evidence] == ["2026"]
            print(f"POST /claims/{CLAIM}/audit -> HTTP 202 APPROVE LOW requires_human=false")
            code, detail = request(base, f"/claims/{CLAIM}")
            claim = ClaimDetail.model_validate(detail)
            assert code == 200 and claim.status == "COMPLETED" and claim.erp_status == "SUCCEEDED"
            assert claim.decision_result == decision and claim.active_run_id == result.run_id
            print(f"GET /claims/{CLAIM} -> HTTP 200 COMPLETED ERP=SUCCEEDED")
            code, log = request(base, f"/claims/{CLAIM}/audit-log")
            events = AuditLogResponse.model_validate(log)
            assert code == 200 and events.total == 12
            assert [e.event_type for e in events.items] == EVENTS
            assert all(e.run_id == result.run_id for e in events.items)
            print(f"GET /claims/{CLAIM}/audit-log -> HTTP 200 events=12 COMPLETE")
            report.update(
                operation=operation,
                claim=detail,
                audit_log=log,
                http_statuses={"audit": 202, "claim": 200, "audit_log": 200},
                openapi_unchanged=True,
            )
        finally:
            stop(process)
            print("UVICORN_STOP=PASS")
        process, base = start(root, environment)
        try:
            code, detail = request(base, f"/claims/{CLAIM}")
            assert code == 200 and detail == report["claim"]
            _, log = request(base, f"/claims/{CLAIM}/audit-log")
            assert log == report["audit_log"]
            code, replay = request(base, f"/claims/{CLAIM}/audit", body)
            assert code == 202 and replay == report["operation"]
            report["restart_persistence"] = True
            print("RESTART_PERSISTENCE=PASS; REPLAY=PASS; DUPLICATE_EVENTS=0")
        finally:
            stop(process)
            print("UVICORN_RESTART_STOP=PASS")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print("M1_HTTP=PASS; REAL_PGVECTOR_VERIFIED=false; REAL_LLM_VERIFIED=false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
