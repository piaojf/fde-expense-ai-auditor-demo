"""Run the unchanged browser E2E in a copy without stopping existing dev servers."""

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from smoke_demo_stability import check_default_http
from smoke_m3 import ROOT, request, start, stop


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fde-m3-browser-") as directory:
        temp = Path(directory)
        web = temp / "apps/web"
        shutil.copytree(ROOT / "apps/web", web)
        # Both ports are already in the frozen default CORS allowlist.
        for port in (5173, 3000):
            try:
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", port))
                break
            except OSError:
                continue
        else:
            raise AssertionError("No free default-CORS frontend port")
        backend, backend_log, base = start(temp, temp / "mock.sqlite3", "NONE")
        vite = None
        vite_log = (output.parent / "m3-browser-vite.log").open("wb")
        try:
            default = check_default_http(base)
            environment = {**os.environ, "VITE_USE_MOCK_API": "false", "VITE_API_BASE": base,
                           "WEB_BASE": f"http://localhost:{port}"}
            vite = subprocess.Popen(
                ["node", "node_modules/vite/bin/vite.js", "--host", "127.0.0.1",
                 "--port", str(port), "--strictPort"], cwd=web, env=environment,
                stdout=vite_log, stderr=subprocess.STDOUT,
            )
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                assert vite.poll() is None
                try:
                    with urllib.request.urlopen(environment["WEB_BASE"], timeout=1) as response:
                        assert response.status == 200
                    break
                except (urllib.error.URLError, TimeoutError):
                    time.sleep(0.1)
            else:
                raise AssertionError("Vite startup timed out")
            script = web / "scripts/e2e-real-api.mjs"
            original = ROOT / "apps/web/scripts/e2e-real-api.mjs"
            assert script.read_bytes() == original.read_bytes()
            cmd = ["node", str(script)]
            completed = subprocess.run(cmd, cwd=temp, env=environment, capture_output=True,
                                       text=True, encoding="utf-8", timeout=180)
            (output.parent / "browser-e2e.log").write_text(completed.stdout + completed.stderr,
                                                         encoding="utf-8")
            assert completed.returncode == 0, completed.stdout + completed.stderr
            assert "E2E PASS" in completed.stdout
            assert "控制台错误：无" in completed.stdout and "404 资源：无" in completed.stdout
            _, detail = request(base, "/claims/CLM-2026-00004")
            _, audit = request(base, "/claims/CLM-2026-00004/audit-log")
            assert detail["status"] == "COMPLETED" and detail["erp_status"] == "SUCCEEDED"
            assert detail["decision_result"]["decision"] == "APPROVE" and audit["total"] == 16
            report = {"default": default, "detail": detail, "audit": audit,
                      "command": subprocess.list2cmdline(cmd), "input": environment["WEB_BASE"],
                      "output": completed.stdout + completed.stderr,
                      "exit_status": completed.returncode,
                      "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                      "existing_servers_untouched": True}
            shutil.copytree(temp / ".workbuddy/verify/e2e", output.parent / "browser-screenshots",
                            dirs_exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
            print("REAL_BROWSER_E2E=PASS; CONSOLE_ERRORS=0; CORS_ERRORS=0; HTTP404=0")
            print("FRONTEND_SCRIPT_UNCHANGED=YES; EXISTING_DEV_SERVERS_UNTOUCHED=YES")
        finally:
            if vite is not None:
                stop(vite, vite_log)
            else:
                vite_log.close()
            stop(backend, backend_log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
