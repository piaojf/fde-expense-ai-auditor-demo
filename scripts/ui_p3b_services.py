"""Start/restart/stop only manifest-owned acceptance processes, never shared services."""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from rag.embeddings.providers import DeterministicTestEmbedding
from rag.ingestion.postgres import PostgresPolicyIngestor, load_seed
from scripts.ui_p3b_checks import fresh_db

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/ui-p3b/browser"
MANIFEST = OUT / "processes.json"


def environment():
    env = os.environ.copy()
    env.update(
        APP_ENV="test",
        LLM_PROVIDER="mock",
        POLICY_RETRIEVER="inmemory",
        POLICY_DEMO_ENABLED="false",
        POLICY_REMOTE_ENABLED="false",
        POLICY_REMOTE_MAX_REQUESTS="0",
        MOCK_STORE_PATH=str(OUT / "import-safety.sqlite3"),
    )
    return env


def spawn(label, cmd, env):
    with (OUT / (label + ".log")).open("ab") as f:
        p = subprocess.Popen(
            cmd,
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return dict(pid=p.pid, command=cmd, cwd=str(ROOT), owned=True)


def ready(url):
    for _ in range(60):
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError("Owned acceptance service did not become ready")


def stop_owned(item):
    assert item["owned"] and item["cwd"] == str(ROOT)
    actual = subprocess.run(
        ["ps", "-p", str(item["pid"]), "-o", "command="],
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    if not actual.strip():
        return
    assert (
        "scripts.ui_p3b_runtime:app" in actual
        or "vite preview" in actual
        or ("npm" in actual and "preview" in actual)
    ), actual
    os.killpg(item["pid"], signal.SIGTERM)
    for _ in range(80):
        time.sleep(0.1)
        actual = subprocess.run(
            ["ps", "-p", str(item["pid"]), "-o", "stat="],
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
        if not actual.strip() or actual.strip().startswith("Z"):
            return
    raise RuntimeError("Owned process did not stop; no global process cleanup")


def main():
    action = sys.argv[1]
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT.chmod(0o700)
    if action == "start":
        for port in (3000, 8001):
            result = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True
            )
            assert result.returncode, "Acceptance port occupied; preserve existing process"
        url = fresh_db("browser")
        assert (make_url(url).database or "").startswith("fde_m0_test_ui_p3b_browser_")
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["database_url"] = url
        command.upgrade(config, "head")
        e = create_engine(url)
        PostgresPolicyIngestor(e, DeterministicTestEmbedding()).ingest(load_seed())
        e.dispose()
        for name, value in (("database-url.txt", url), ("demo-token.txt", os.urandom(32).hex())):
            path = OUT / name
            with path.open("x", encoding="utf-8") as f:
                f.write(value)
            path.chmod(0o600)
        (OUT / "synthetic-business-date.txt").write_text(
            datetime.now(UTC).astimezone(timezone(timedelta(hours=8))).date().isoformat(),
            encoding="utf-8",
        )
        backend = [
            str(ROOT / ".venv/bin/python"),
            "-m",
            "uvicorn",
            "scripts.ui_p3b_runtime:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8001",
            "--no-proxy-headers",
        ]
        front = [
            "npm",
            "--prefix",
            "apps/web",
            "run",
            "preview",
            "--",
            "--host",
            "127.0.0.1",
            "--port",
            "3000",
            "--strictPort",
        ]
        manifest = dict(backend=spawn("backend", backend, environment()))
        MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        ready("http://127.0.0.1:8001/health")
        manifest["frontend"] = spawn("frontend", front, os.environ.copy())
        MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        ready("http://127.0.0.1:3000/policies")
        print("Synthetic browser services ready; paid calls disabled")
    elif action == "restart":
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        stop_owned(manifest["backend"])
        manifest["previous_backend"] = manifest["backend"]
        manifest["backend"] = spawn(
            "backend-restarted", manifest["backend"]["command"], environment()
        )
        MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        ready("http://127.0.0.1:8001/health")
        print("Owned backend restarted")
    elif action == "stop":
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        for name in ("backend", "frontend"):
            if name in manifest:
                stop_owned(manifest[name])
        print("Owned acceptance services stopped")
    else:
        raise ValueError("Expected start/restart/stop")


if __name__ == "__main__":
    main()
