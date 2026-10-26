"""Byte-preserved contracts and baseline-generated OpenAPI equivalence."""

import hashlib
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from sqlalchemy import create_engine, inspect, text

from apps.api.config import Settings

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/ui-p3b"
BASELINE = "2611ce19ebc0e1efc47c5d75efc2aca3c4ee8353"
FROZEN = [
    "docs/openapi.json",
    "docs/api-contract.md",
    "docs/api-contract-v1.1.md",
    "docs/api-contract-v1.2.md",
    "docs/api-contract-v1.3.md",
    "domain/schemas.py",
    "domain/submission.py",
    "domain/invoices.py",
    "domain/policies.py",
    "apps/api/submission.py",
    "apps/api/invoices.py",
    "apps/api/policies.py",
]


def schemas(directory):
    code = """
import json
from fastapi.testclient import TestClient
from apps.api.config import Settings
from apps.api.main import create_app
app=create_app(Settings(app_env="test",_env_file=None))
with TestClient(app) as c:
    paths=("/openapi.json","/api/v1.1/openapi.json","/api/v1.2/openapi.json")
    results={p:c.get(p).json() for p in paths}
    results["v1.3-internal-schema"]=app.state.policy_app.openapi()
print(json.dumps(results,sort_keys=True,ensure_ascii=False))
"""
    env = os.environ.copy()
    env.update(
        APP_ENV="test",
        LLM_PROVIDER="mock",
        POLICY_RETRIEVER="inmemory",
        POLICY_DEMO_ENABLED="false",
        POLICY_REMOTE_ENABLED="false",
        POLICY_REMOTE_MAX_REQUESTS="0",
    )
    env.pop("PYTHONPATH", None)
    r = subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-c", code],
        cwd=directory,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(r.stdout)


def main():
    for path in FROZEN:
        original = subprocess.check_output(["git", "show", BASELINE + ":" + path], cwd=ROOT)
        assert (ROOT / path).read_bytes() == original, path
    historical = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", BASELINE, "evals/datasets", "docs"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
    ).splitlines()
    for path in historical:
        assert (ROOT / path).read_bytes() == subprocess.check_output(
            ["git", "show", BASELINE + ":" + path], cwd=ROOT
        ), path
    archive = subprocess.check_output(["git", "archive", BASELINE], cwd=ROOT)
    with TemporaryDirectory(prefix="ui-p3b-contract-") as d:
        with tarfile.open(fileobj=io.BytesIO(archive), encoding="utf-8") as t:
            t.extractall(d, filter="data")
        old = schemas(d)
    current = schemas(ROOT)
    assert current == old
    for path in ("/openapi.json", "/api/v1.1/openapi.json", "/api/v1.2/openapi.json"):
        assert httpx.get("http://127.0.0.1:8001" + path).json() == old[path]
    preservation = json.loads((OUT / "protected-before.json").read_text(encoding="utf-8"))
    changed = [
        p
        for p, h in preservation["files"].items()
        if hashlib.sha256((ROOT / p).read_bytes()).hexdigest() != h
    ]
    assert not changed, changed
    engine = create_engine(Settings().database_url)
    with engine.connect() as c:
        after = {}
        for t in inspect(c).get_table_names():
            rows = c.execute(text('SELECT row_to_json(t)::text FROM "' + t + '" t')).scalars().all()
            after[t] = hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest()
    engine.dispose()
    assert after == preservation["demo_pg"]
    report = dict(
        status="PASS",
        frozen_files=FROZEN,
        historical_files=len(historical),
        schema_digests={
            p: hashlib.sha256(json.dumps(s, sort_keys=True).encode()).hexdigest()
            for p, s in old.items()
        },
        actual_http_equivalent=True,
        protected_file_count=len(preservation["files"]),
        demo_database_unchanged=True,
    )
    (OUT / "contracts-and-preservation.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print("API_CONTRACTS_UNCHANGED=PASS; protected files/demo DB preserved")


if __name__ == "__main__":
    main()
