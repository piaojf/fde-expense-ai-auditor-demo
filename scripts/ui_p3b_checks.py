"""Fresh disposable PostgreSQL test runner; no demo migrations or paid providers."""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/ui-p3b"


def fresh_db(label):
    # Reuse an established local endpoint, never change its network configuration.
    url = make_url((OUT / "baseline-rerun-database-url.txt").read_text(encoding="utf-8"))
    assert url.host in ("localhost", "127.0.0.1") and url.port == 55432
    name = "fde_m0_test_ui_p3b_" + label + "_" + uuid4().hex[:10]
    engine = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with engine.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    engine.dispose()
    final = url.set(database=name).render_as_string(hide_password=False)
    path = OUT / (label + "-database-url.txt")
    path.write_text(final, encoding="utf-8")
    path.chmod(0o600)
    return final


def main():
    label = sys.argv[1]
    env = os.environ.copy()
    env.pop("APP_ENV", None)
    env.update(
        TEST_DATABASE_URL=fresh_db(label),
        LLM_PROVIDER="mock",
        POLICY_RETRIEVER="inmemory",
        POLICY_DEMO_ENABLED="false",
        POLICY_REMOTE_ENABLED="false",
        POLICY_REMOTE_MAX_REQUESTS="0",
    )
    log = OUT / ("pytest-" + label + ".txt")
    with log.open("w", encoding="utf-8") as f:
        result = subprocess.run(
            [str(ROOT / ".venv/bin/python"), "-m", "pytest", *sys.argv[2:], "-q"],
            cwd=ROOT,
            env=env,
            stdout=f,
            stderr=subprocess.STDOUT,
        )
    print("TEST_EXIT", result.returncode, flush=True)
    print(log.read_text(encoding="utf-8")[-6000:])
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
