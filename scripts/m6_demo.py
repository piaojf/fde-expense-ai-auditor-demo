"""Isolated M6 lifecycle. Network use requires the documented human consent.

Reset rotates generations; it never deletes/downgrades an existing database.
Runtime secrets stay in ignored 0700 directories and 0600 files.
"""

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import time
from datetime import date
from pathlib import Path
from uuid import uuid4

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from apps.api.config import Settings
from rag.embeddings.providers import DeterministicTestEmbedding
from rag.ingestion.postgres import PostgresPolicyIngestor, load_seed

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/m6"
CURRENT = OUT / "current.json"
PYTHON = str(ROOT / ".venv/bin/python")


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        os.chmod(temp, 0o600)
        json.dump(value, f, ensure_ascii=False, indent=2)
    temp.replace(path)


def active():
    value = json.loads(CURRENT.read_text(encoding="utf-8"))
    path = Path(value["directory"]).resolve()
    assert path.parent == (OUT / "generations").resolve(), "Refuse external runtime"
    assert not Path(value["directory"]).is_symlink(), "Refuse symlink runtime"
    return path


def base_url():
    url = make_url(Settings(_env_file=ROOT / ".env").database_url)
    assert url.host in ("localhost", "127.0.0.1") and url.port == 55432
    return url


def new_database(label):
    assert label in ("demo", "regression")
    name = "fde_m0_test_m6_" + label + "_" + uuid4().hex
    url = base_url()
    engine = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()
    return url.set(database=name).render_as_string(hide_password=False)


def safe_environment():
    env = os.environ.copy()
    for key in (
        "DEEPSEEK_API_KEY",
        "EMBEDDING_API_KEY",
        "RUN_LIVE_LLM_ACCEPTANCE",
        "RUN_LIVE_LLM_TESTS",
    ):
        env.pop(key, None)
    env.update(
        APP_ENV="test",
        LLM_PROVIDER="mock",
        POLICY_RETRIEVER="inmemory",
        POLICY_DEMO_ENABLED="false",
        POLICY_REMOTE_ENABLED="false",
        POLICY_REMOTE_MAX_REQUESTS="0",
        ENTERPRISE_ENABLED="false",
        MOCK_STORE_PATH=str(OUT / "import.sqlite3"),
    )
    return env


def init(reset_key=None):
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT.chmod(0o700)
    if CURRENT.exists():
        old = json.loads(CURRENT.read_text(encoding="utf-8"))
        if reset_key is None or old.get("reset_key") == reset_key:
            print("Existing generation reused; no reset")
            return active()
        manifest = active() / "processes.json"
        if manifest.exists():
            assert all(
                not alive(i) for i in json.loads(manifest.read_text(encoding="utf-8")).values()
            ), "Stop owned processes before reset"
    index = OUT / "reset-keys.json"
    keys = json.loads(index.read_text(encoding="utf-8")) if index.exists() else {}
    if reset_key in keys:
        raise ValueError("Old reset key already used; refuse generation rollback")
    directory = OUT / "generations" / uuid4().hex
    directory.mkdir(parents=True, mode=0o700)
    url = new_database("demo")
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = url
    command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        PostgresPolicyIngestor(engine, DeterministicTestEmbedding()).ingest(load_seed())
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM policy_versions")) == 8
            assert conn.scalar(text("SELECT count(*) FROM policy_publication_events")) == 0
    finally:
        engine.dispose()
    for name, value in (
        ("database-url.txt", url),
        ("demo-token.txt", os.urandom(32).hex()),
        ("synthetic-business-date.txt", "2026-11-03"),
        ("erp-fault.txt", "NONE"),
    ):
        target = directory / name
        with target.open("x", encoding="utf-8") as f:
            os.chmod(target, 0o600)
            f.write(value)
    value = dict(
        directory=str(directory),
        reset_key=reset_key,
        database=make_url(url).database,
        synthetic=True,
    )
    if reset_key:
        keys[reset_key] = value
        private_json(index, keys)
    private_json(CURRENT, value)
    print("Fresh synthetic generation initialized; previous generations preserved")
    return directory


def alive(item):
    result = subprocess.run(
        ["ps", "-p", str(item["pid"]), "-o", "stat=,lstart=,command="],
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    if not result:
        return False
    stat, result = result.split(maxsplit=1)
    # One atomic ps snapshot avoids a PID disappearing between two queries.
    if "Z" in stat or "E" in stat:
        return False
    assert result == item["identity"], "PID identity changed; refuse process action"
    return True


def stop():
    manifest = active() / "processes.json"
    if not manifest.exists():
        return
    for item in json.loads(manifest.read_text(encoding="utf-8")).values():
        if alive(item):
            assert os.getpgid(item["pid"]) == item["pid"], "Refuse unrelated process group"
            os.killpg(item["pid"], signal.SIGTERM)
            for _ in range(100):
                if not alive(item):
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError("Owned process did not stop; preserve all other services")
    print("Only owned M6 services stopped")


def launch(label, cmd, env):
    directory = active()
    with (directory / (label + ".log")).open("ab") as log:
        p = subprocess.Popen(
            cmd,
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    identity = subprocess.check_output(
        ["ps", "-p", str(p.pid), "-o", "lstart=,command="], text=True, encoding="utf-8"
    ).strip()
    return dict(pid=p.pid, identity=identity)


def start():
    directory = active()
    manifest = directory / "processes.json"
    if manifest.exists() and any(
        alive(i) for i in json.loads(manifest.read_text(encoding="utf-8")).values()
    ):
        raise RuntimeError("M6 already running; use status or restart")
    for port in (3000, 8001):
        with socket.socket() as sock:
            # A closed service can leave TIME_WAIT sockets. This does not permit
            # binding over an existing listener (SO_REUSEPORT is never enabled).
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
    env = safe_environment()
    env.update(
        VITE_USE_MOCK_API="false",
        VITE_API_BASE="http://127.0.0.1:8001",
        VITE_M6_DEMO="true",
        VITE_ENTERPRISE_MODE=(
            "true" if (directory / "enterprise-enabled.json").exists() else "false"
        ),
        VITE_M6_BUSINESS_DATE=date.fromisoformat(
            (directory / "synthetic-business-date.txt").read_text(encoding="utf-8").strip()
        ).isoformat(),
    )
    build = OUT / "web"
    with (OUT / "build.txt").open("w", encoding="utf-8") as log:
        subprocess.run(
            ["npm", "--prefix", "apps/web", "run", "build", "--", "--outDir", str(build)],
            env=env,
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    items = {}
    try:
        items["backend"] = launch(
            "backend",
            [
                PYTHON,
                "-m",
                "uvicorn",
                "scripts.m6_runtime:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8001",
                "--no-proxy-headers",
            ],
            env,
        )
        private_json(manifest, items)
        wait_ready("http://127.0.0.1:8001/health")
        items["frontend"] = launch(
            "frontend",
            [
                "node",
                str(ROOT / "apps/web/node_modules/vite/bin/vite.js"),
                "preview",
                "--outDir",
                str(build),
                "--host",
                "127.0.0.1",
                "--port",
                "3000",
                "--strictPort",
            ],
            env,
        )
        private_json(manifest, items)
        wait_ready("http://127.0.0.1:3000/policies")
        for item in items.values():
            item["identity"] = subprocess.check_output(
                ["ps", "-p", str(item["pid"]), "-o", "lstart=,command="],
                text=True,
                encoding="utf-8",
            ).strip()
        private_json(manifest, items)
    except Exception:
        stop()
        raise
    print("M6 ready: http://127.0.0.1:3000 ; LLM mode shown in portal ; Mock ERP ; offline vectors")


def wait_ready(url):
    for _ in range(80):
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError("M6 readiness failed; inspect owned logs")


def status():
    directory = active()
    items = json.loads((directory / "processes.json").read_text(encoding="utf-8"))
    print(
        json.dumps(
            dict(generation=directory.name, owned_processes={k: alive(v) for k, v in items.items()})
        )
    )


def preserve():
    files = {}
    for pattern in (".env", "apps/web/.env*", "**/*.sqlite3", "**/*.db"):
        for p in ROOT.glob(pattern):
            if p.is_file() and not str(p.relative_to(ROOT)).startswith("output/m6/"):
                files[str(p.relative_to(ROOT))] = hashlib.sha256(p.read_bytes()).hexdigest()
    engine = create_engine(base_url())
    try:
        with engine.connect() as conn:
            tables = {
                t: hashlib.sha256(
                    json.dumps(
                        sorted(
                            conn.execute(text('SELECT row_to_json(t)::text FROM "' + t + '" t'))
                            .scalars()
                            .all()
                        )
                    ).encode()
                ).hexdigest()
                for t in inspect(conn).get_table_names()
            }
            version = conn.scalar(text("SHOW server_version"))
            vector = conn.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
    finally:
        engine.dispose()
    return dict(files=files, demo_pg=tables, postgres_version=version, vector_version=vector)


def regression():
    env = safe_environment()
    env.pop("APP_ENV", None)
    env["TEST_DATABASE_URL"] = new_database("regression")
    private_json(OUT / "regression-db.json", {"database_url": env["TEST_DATABASE_URL"]})
    with (OUT / "pytest.txt").open("w", encoding="utf-8") as log:
        result = subprocess.run(
            [PYTHON, "-m", "pytest", "-q"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT
        )
    print((OUT / "pytest.txt").read_text(encoding="utf-8")[-2500:])
    raise SystemExit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "init",
            "start",
            "stop",
            "restart",
            "status",
            "regression",
            "preserve",
            "verify-preservation",
            "clock",
            "fault",
        ],
    )
    parser.add_argument("--reset-key")
    parser.add_argument("--business-date")
    parser.add_argument("--mode", choices=["NONE", "TIMEOUT_ALWAYS", "SUCCESS_THEN_RESPONSE_LOST"])
    args = parser.parse_args()
    if args.action == "init":
        init(args.reset_key)
    elif args.action == "start":
        start()
    elif args.action == "stop":
        stop()
    elif args.action == "restart":
        stop()
        start()
    elif args.action == "status":
        status()
    elif args.action == "regression":
        regression()
    elif args.action in ("clock", "fault"):
        if args.action == "clock":
            value = date.fromisoformat(args.business_date)
            assert date(2026, 9, 1) <= value <= date(2026, 12, 31), "M6 synthetic date range"
            target, value = "synthetic-business-date.txt", value.isoformat()
        else:
            assert args.mode, "Explicit fault mode required"
            target, value = "erp-fault.txt", args.mode
        path = active() / target
        temp = path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as f:
            os.chmod(temp, 0o600)
            f.write(value)
        temp.replace(path)
        print("Only test-only M6 runtime updated:", target, value)
    elif args.action == "preserve":
        path = OUT / "protected-before.json"
        assert not path.exists(), "Do not replace baseline evidence"
        private_json(path, preserve())
        print("Read-only baseline captured")
    else:
        assert preserve() == json.loads((OUT / "protected-before.json").read_text(encoding="utf-8"))
        print("Existing configuration, SQLite and acceptance PostgreSQL unchanged")


if __name__ == "__main__":
    main()
