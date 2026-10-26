"""Local mock-only M3 hard gate: real FastAPI processes, HTTP and durable checkpoints.

--serve is an explicit test fixture entry point, not a production fault configuration.
"""

import argparse
import asyncio
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

ROOT = Path(__file__).resolve().parents[1]
C6 = "CLM-2026-00006"


def request(base, path, body=None):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def start(temp, store, mode):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    log = (temp / f"server-{mode}.log").open("wb")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    environment["PYTHONIOENCODING"] = "utf-8"
    for key in ("MOCK_STORE_PATH", "APP_ENV", "LLM_PROVIDER", "CORS_ORIGINS"):
        environment.pop(key, None)
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--serve", "--store", str(store),
         "--mode", mode, "--port", str(port)],
        cwd=temp, env=environment, stdout=log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                log.flush()
                raise AssertionError((temp / f"server-{mode}.log").read_text(encoding="utf-8"))
            try:
                if request(base, "/health")[0] == 200:
                    return process, log, base
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.05)
        raise AssertionError("FastAPI startup timed out")
    except BaseException:
        stop(process, log)
        raise


def stop(process, log):
    if process.poll() is None:
        if sys.platform == "win32":
            # venv python.exe is a launcher: stop its owned child interpreter too.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True)
        else:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    log.close()


def observe(store, op):
    from agent.state import ClaimState
    from apps.api.repository import MockClaimRepository
    from apps.api.workflow import AuditService

    repo = MockClaimRepository(store_path=str(store))

    async def checkpoint():
        service = AuditService(repo)
        context = repo.run_context(C6, op["run_id"])
        async with service.graph_session() as graph:
            snap = await graph.aget_state({"configurable": {
                "thread_id": context["thread_id"], "checkpoint_id": context["checkpoint_id"],
            }})
            state = ClaimState.model_validate(snap.values)
            return {"context": context, "next": list(snap.next),
                    "erp_attempts": state.audit_context["erp_attempts"],
                    "interrupts": sum(len(t.interrupts) for t in snap.tasks)}

    try:
        return {**asyncio.run(checkpoint()), "actual_write_count": repo.erp_write_count(C6),
                "effects": repo.erp_effects(C6),
                "events": [e.model_dump(mode="json") for e in repo.events(C6)]}
    finally:
        repo.close()


def resume_body(op, key="recovery-resume"):
    return {"run_id": op["run_id"], "expected_revision": op["revision"],
            "idempotency_key": key}


def run_gates():
    report = {}
    for mode in ("TIMEOUT_ONCE", "SUCCESS_THEN_RESPONSE_LOST", "TIMEOUT_ALWAYS",
                 "LOST_RESPONSE_ALWAYS"):
        with tempfile.TemporaryDirectory(prefix="fde-m3-") as directory:
            temp = Path(directory)
            store = temp / "mock.sqlite3"
            process, log, base = start(temp, store, mode)
            try:
                code, op = request(base, f"/claims/{C6}/audit", {"idempotency_key": "m3-gate"})
                assert code == 202
                assert op["decision_result"]["decision"] == "APPROVE"
                before = observe(store, op)
                case = {"audit": op, "before": before, "initial_pid": process.pid}
                if mode in ("TIMEOUT_ONCE", "SUCCESS_THEN_RESPONSE_LOST"):
                    assert op["status"] == "COMPLETED"
                    assert before["erp_attempts"] == 2 and before["actual_write_count"] == 1
                    if mode == "SUCCESS_THEN_RESPONSE_LOST":
                        assert any(e["event_type"] == "IDEMPOTENT_REPLAY"
                                   for e in before["events"])
                    case["final"] = op
                else:
                    assert op["status"] == "PENDING_ERP"
                    assert before["erp_attempts"] == 3 and before["next"] == ["erp_recovery"]
                    assert before["interrupts"] == 1
                    assert before["context"]["recovery_status"] == "RECOVERY_PENDING"
                    assert before["context"]["recoverable"]
            finally:
                stop(process, log)
            if mode in ("TIMEOUT_ALWAYS", "LOST_RESPONSE_ALWAYS"):
                # New interpreter, new FastAPI, new adapters, same durable store/checkpoint.
                process, log, base = start(temp, store, "NONE")
                try:
                    case["restart_pid"] = process.pid
                    durable = observe(store, op)
                    assert durable["context"] == before["context"]
                    assert durable["next"] == ["erp_recovery"]
                    wrong = {**resume_body(op), "expected_revision": op["revision"] + 1}
                    assert request(base, f"/claims/{C6}/resume", wrong)[0] == 409
                    body = resume_body(op)
                    code, final = request(base, f"/claims/{C6}/resume", body)
                    assert code == 202 and final["status"] == "COMPLETED"
                    assert final["decision_result"] == op["decision_result"]
                    assert request(base, f"/claims/{C6}/resume", body) == (202, final)
                    assert request(base, f"/claims/{C6}/resume",
                                   resume_body(final, "new-key"))[0] == 409
                    after = observe(store, final)
                    assert after["actual_write_count"] == 1 and not after["next"]
                    assert after["erp_attempts"] == 4
                    assert not after["context"]["recoverable"]
                    for kind in ("CLAIM_LOADED", "RULES_CHECKED", "POLICY_RETRIEVED",
                                 "LLM_ANALYSIS_COMPLETED", "RISK_ASSESSED", "DECISION_MADE"):
                        assert sum(e["event_type"] == kind for e in after["events"]) == 1
                    assert after["events"][:len(before["events"])] == before["events"]
                    if mode == "LOST_RESPONSE_ALWAYS":
                        assert any(e["event_type"] == "IDEMPOTENT_REPLAY"
                                   for e in after["events"])
                    case.update(final=final, after=after, process_restart="PASS",
                                duplicate_resume="PASS", upstream_reexecution=0)
                finally:
                    stop(process, log)
            case["status"] = "PASS"
            report[mode] = case
            print(f"{mode}=PASS; ATTEMPTS={before['erp_attempts']}; ERP_ACTUAL_WRITES=1")
    print("PROCESS_RESTART=PASS; RECOVERY_RESUME=PASS; DUPLICATE_RESUME=PASS; UPSTREAM_RERUNS=0")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--store", type=Path)
    parser.add_argument("--mode", default="NONE")
    parser.add_argument("--port", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.serve:
        import uvicorn

        from apps.api.config import Settings
        from apps.api.main import create_app
        from apps.api.repository import MockClaimRepository
        from integrations.erp.mock import MockERP
        from integrations.faults import FailureMode

        repo = MockClaimRepository(store_path=str(args.store))
        mode = args.mode
        if mode == "LOST_RESPONSE_ALWAYS":
            class LostEveryResponse(MockERP):
                async def write_decision(self, result, **kwargs):
                    await super().write_decision(result, **kwargs)
                    raise TimeoutError("Mock acknowledgement lost")
            erp = LostEveryResponse(repo)
        else:
            erp = MockERP(repo, FailureMode(mode))
        app = create_app(Settings(app_env="test", _env_file=None), repo, erp=erp)
        try:
            uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
        finally:
            repo.close()
    else:
        report = run_gates()
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
