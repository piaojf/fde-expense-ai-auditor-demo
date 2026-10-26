"""Start real Uvicorn on a free loopback port, verify HTTP, always stop it."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    temporary = TemporaryDirectory(prefix="fde-public-smoke-")
    from scripts.public_tests import safe_environment

    environment = safe_environment()
    os.environ.clear()
    os.environ.update(environment)
    from apps.api.config import Settings

    config = Settings(
        _env_file=None,
        llm_provider="mock",
        policy_retriever="inmemory",
        mock_store_path=str(Path(temporary.name) / "mock.sqlite3"),
    )
    from apps.api.repository import MockClaimRepository

    repo = MockClaimRepository(store_path=config.mock_store_path)
    expected = len(repo.list())
    repo.close()
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
        env={
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "MOCK_STORE_PATH": config.mock_store_path,
            "LLM_PROVIDER": "mock",
            "POLICY_RETRIEVER": "inmemory",
            "POLICY_REMOTE_ENABLED": "false",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(process.communicate()[0])
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                    body = json.load(r)
                    assert r.status == 200
                    assert body["database"] == "not_checked"
                    assert body["milestone"] == "M0"
                    print("GET /health -> HTTP 200 " + json.dumps(body, separators=(",", ":")))
                    break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.1)
        else:
            raise RuntimeError("Uvicorn startup deadline exceeded")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/claims", timeout=3) as r:
            assert json.load(r)["total"] == expected
        print(f"UVICORN_START=PASS; MOCK_CLAIMS={expected}")
        return 0
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        temporary.cleanup()
        print("UVICORN_STOP=PASS")


if __name__ == "__main__":
    sys.exit(main())
