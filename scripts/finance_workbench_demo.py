"""Local V3 demo; real DeepSeek requires a separate explicit consent profile.

Uses the existing controlled publication helpers in a NEW database and SQLite file.
Reset rotates generations, never deletes any previous data or shared database.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
OUT = (ROOT / "output/p4b/finance-workbench-browser-e2e").resolve()
CURRENT = OUT / "current.json"
DB_PREFIX = "fde_m0_test_finance_browser_"
API = "http://127.0.0.1:8014"
WEB = "http://127.0.0.1:5173"
CHILDREN = {}


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)


def active():
    p = Path(json.loads(CURRENT.read_text(encoding="utf-8"))["directory"])
    assert p.parent == OUT / "generations" and not p.is_symlink()
    return p


def safe_env():
    env = os.environ.copy()
    for key in (
        "DEEPSEEK_API_KEY",
        "EMBEDDING_API_KEY",
        "DATABASE_URL",
        "TEST_DATABASE_URL",
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
        P4B_LIVE_LLM="false",
        P4A_ENABLED="false",
        P4B_ENABLED="false",
        P4B_ADVISORY_ENABLED="false",
        ENTERPRISE_ENABLED="false",
        EMBEDDING_PROVIDER="deterministic",
        MOCK_STORE_PATH=str(active() / "import-safety.sqlite3"),
    )
    return env


def init(config):
    if CURRENT.exists():
        manifest = active() / "processes.json"
        if manifest.exists():
            for entry in json.loads(manifest.read_text(encoding="utf-8")).values():
                state = subprocess.run(
                    ["ps", "-p", str(entry["pid"]), "-o", "stat="],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                ).stdout.strip()
                if state and not state.startswith("Z"):
                    raise RuntimeError("Stop the current owned generation before init/reset")
    from dotenv import dotenv_values
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    # Read ONLY the DB URL; never copy API keys or log the source configuration.
    url = make_url(dotenv_values(config)["DATABASE_URL"])
    assert url.host in ("localhost", "127.0.0.1") and url.port == 55432
    name = DB_PREFIX + uuid4().hex
    engine = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with engine.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    engine.dispose()
    gen = OUT / "generations" / uuid4().hex
    gen.mkdir(parents=True, mode=0o700)
    (gen / "database-url.txt").write_text(
        url.set(database=name).render_as_string(hide_password=False), encoding="utf-8"
    )
    (gen / "database-url.txt").chmod(0o600)
    save(CURRENT, dict(directory=str(gen)))
    save(
        gen / "isolation.json",
        dict(
            database=name,
            sqlite=str(gen / "portal.sqlite3"),
            real_deepseek_calls=0,
            real_bge_calls=0,
            real_erp_calls=0,
            providers="authored V3 MockTransport / deterministic embeddings / MockERP",
        ),
    )
    print("Created isolated generation", gen.name, "database", name)


def spawn(label, cmd, env):
    gen = active()
    with (gen / (label + ".log")).open("ab") as f:
        proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    CHILDREN[label] = proc
    return dict(pid=proc.pid, command=cmd, cwd=str(ROOT))


def start():
    import httpx

    for port in (8014, 5173):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(f"Port {port} occupied; refuse to replace service")
    gen = active()
    env = safe_env()
    backend = spawn(
        "backend", [sys.executable, "-m", "scripts.finance_workbench_demo", "serve"], env
    )
    save(active() / "processes.json", dict(backend=backend))
    for _ in range(180):
        if CHILDREN["backend"].poll() is not None:
            raise RuntimeError("Owned backend exited; see generation/backend.log")
        try:
            if httpx.get(API + "/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    else:
        raise RuntimeError("Backend not ready; see generation/backend.log")
    env.update(
        VITE_API_BASE=API,
        VITE_USE_MOCK_API="false",
        VITE_ENTERPRISE_MODE="true",
        VITE_P4A_ENABLED="true",
        VITE_M6_DEMO="true",
        VITE_M6_BUSINESS_DATE="2027-01-05",
    )
    frontend = spawn(
        "frontend",
        [
            str(ROOT / "apps/web/node_modules/.bin/vite"),
            "apps/web",
            "--host",
            "127.0.0.1",
            "--port",
            "5173",
            "--strictPort",
        ],
        env,
    )
    save(gen / "processes.json", dict(backend=backend, frontend=frontend))
    for _ in range(60):
        if CHILDREN["frontend"].poll() is not None:
            raise RuntimeError("Owned frontend exited; see generation/frontend.log")
        try:
            if httpx.get(WEB, timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    else:
        raise RuntimeError("Frontend not ready")
    print("Backend", API, "Frontend", WEB, "owned PIDs", backend["pid"], frontend["pid"])


def configure_demo_advisory(data, request):
    """Authored Mock responses follow server facts; never substitute for a real model."""
    from advisory_v3_support import output_v3

    positive = request.facts["positive"]

    def verified(key):
        item = positive.get(key, {})
        return item.get("status") == "VERIFIED" and item.get("value") is not False

    codes = ["GENERAL_FINANCE_CHECK"] if verified("original_invoice") else []
    if verified("stay") and verified("original_invoice"):
        codes.append("VERIFIED_STAY")
    if verified("policy_version"):
        codes.append("POLICY_EFFECTIVE")
    if verified("prior_approval"):
        codes.append("PRIOR_APPROVAL_VERIFIED")
    hotel_rule = next(
        (r for r in request.rule_results if r["rule_id"] == "HOTEL_LIMIT_MATRIX"), None
    )
    failed = hotel_rule and hotel_rule["outcome"] == "FAIL"
    if hotel_rule and hotel_rule["outcome"] in {"PASS", "FAIL"}:
        codes.append("RULE_FAILURE" if failed else "AMOUNT_WITHIN_LIMIT")
    missing = [key for key in request.facts["trusted"]["positive_required"]
               if not verified(key)]
    if missing:
        item = positive.get(missing[0], {})
        code = ("FACT_CONFLICT" if item.get("status") == "CONFLICTED"
                else "MISSING_REQUIRED_EVIDENCE")
        codes.append(code)
    data.update(output_v3(request, codes))
    if missing:
        data["issues"][-1].update(fact_key=missing[0], fact_refs=["positive." + missing[0]])
    if failed:
        data.update(risk_level="HIGH", recommendation="RECOMMEND_REJECTION")
    # A dedicated failure case keeps its intentionally invalid output for the demo.
    if request.facts["candidate"]["notes"].startswith("SYNTHETIC_AI_FAILURE"):
        data["missing_evidence"] = ["模型臆断的强制客户项目证明"]


def serve():
    assert not (ROOT / ".env").exists(), "Demo worktree must not contain a .env"
    os.environ.update(safe_env())
    # Explicit test runtime imports. Existing production app/API/graph stay intact.
    gen = active()
    from scripts.finance_workbench_ai import load_live_settings

    live_settings = load_live_settings(gen)
    sys.path.insert(0, str(ROOT / "tests"))
    os.environ["TEST_DATABASE_URL"] = (gen / "database-url.txt").read_text(encoding="utf-8")
    from datetime import date

    import uvicorn
    from advisory_support import BASE, prepare
    from advisory_v3_support import AdvisoryWireV3
    from alembic import command
    from alembic.config import Config
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from starlette.middleware.cors import CORSMiddleware
    from test_p4a_postgres import add_booking, prepared, publish
    from test_p4b_fix2_postgres import COMMON

    from apps.api.config import Settings
    from apps.api.main import create_app
    from apps.api.policy_store import PolicyDraftStore
    from apps.api.repository import MockClaimRepository
    from domain.enterprise import CreateUser
    from integrations.ocr.local import LocalOCRProvider
    from integrations.policies.parser import PolicyParser
    from rag.embeddings.providers import DeterministicTestEmbedding
    from rag.ingestion.postgres import PostgresPolicyIngestor, load_seed
    from rag.retrieval.pgvector import PgPolicyCatalog, PgVectorPolicyRetriever
    from tests.test_enterprise import PASSWORD, login

    url = (gen / "database-url.txt").read_text(encoding="utf-8")
    assert make_url(url).database.startswith(DB_PREFIX)
    first = not (gen / "fixture-summary.json").exists()
    engine = create_engine(url)
    embedding = DeterministicTestEmbedding()
    if first:
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["database_url"] = url
        command.upgrade(config, "head")
        PostgresPolicyIngestor(engine, embedding).ingest(load_seed())
    # Demo invoice OCR uses local language assets, never a cloud provider.
    tessdata = os.environ.get("OCR_TESSDATA", str(ROOT / "output/ui-p2/tessdata"))
    languages = os.environ.get("OCR_LANGUAGES", "chi_sim+eng")
    local_ocr = LocalOCRProvider(languages=languages, tessdata=tessdata)
    draft = PolicyDraftStore(engine, PolicyParser(local_ocr), lambda a: embedding)
    if first:
        publication, uid = prepared((engine, draft, embedding, url))
        released, _ = publish(publication, uid)
        pid = released["policy"]["policy_version_id"]
    else:
        pid = json.loads((gen / "fixture-summary.json").read_text(encoding="utf-8"))["policy_id"]

    provider = None
    legacy_provider = None
    if live_settings is None:
        provider = AdvisoryWireV3(configure_demo_advisory)
        provider.model = "mock-authored-advisory-v3"
    else:
        from integrations.llm.budgeted import BudgetedLLM
        from integrations.llm.deepseek import DeepSeekLLMProvider

        legacy_provider = BudgetedLLM(
            DeepSeekLLMProvider(
                api_key=live_settings.deepseek_api_key,
                model=live_settings.deepseek_model,
                base_url=live_settings.deepseek_base_url,
                timeout_seconds=live_settings.deepseek_timeout_seconds,
                max_tokens=live_settings.deepseek_max_tokens,
            ),
            live_settings.p4b_live_budget_path, live_settings.p4b_live_max_requests,
        )
    repo = MockClaimRepository(store_path=str(gen / "portal.sqlite3"))
    settings = Settings(
        app_env="test",
        llm_provider="mock",
        deepseek_api_key=None,
        embedding_api_key=None,
        embedding_provider="deterministic",
        p4a_enabled=True,
        p4b_enabled=True,
        p4b_advisory_enabled=True,
        p4b_advisory_protocol="p4b.advisory.3",
        p4b_live_llm=False,
        enterprise_enabled=True,
        ocr_languages=languages,
        ocr_tessdata=tessdata,
        enterprise_cookie_secure=False,
        cors_origins=[WEB],
        policy_remote_enabled=False,
        policy_remote_max_requests=0,
        _env_file=None,
    )
    if live_settings is not None:
        settings = settings.model_copy(update={
            key: getattr(live_settings, key) for key in (
                "llm_provider", "deepseek_api_key", "deepseek_model", "deepseek_base_url",
                "deepseek_timeout_seconds", "deepseek_max_tokens", "p4b_max_tokens",
                "p4b_live_llm", "p4b_live_budget_path", "p4b_live_max_requests",
                "p4b_advisory_protocol",
            )
        })
    app = create_app(
        settings,
        repo,
        policy_store=draft,
        catalog=PgPolicyCatalog(engine),
        retriever=PgVectorPolicyRetriever(engine, embedding),
        compliance_provider=provider,
        provider=legacy_provider,
        business_date_provider=lambda: date(2027, 1, 5),
    )
    store = app.state.enterprise_app.state.store
    if first:
        users = {}
        for username, role in (
            ("employee", "EMPLOYEE"),
            ("finance", "FINANCE"),
            ("admin", "ADMIN"),
            ("revoked", "FINANCE"),
        ):
            users[username] = store.create_user(
                "DEMO-CN",
                CreateUser(
                    username=username,
                    password=PASSWORD,
                    name="张三" if username == "employee" else "合成" + username,
                    department="研发部",
                    position="工程师" if role == "EMPLOYEE" else "审核员",
                    role=role,
                ),
            )
        with TestClient(app) as c:
            env = c, app, repo, store, users, pid
            login(c, "admin")
            source = c.get(BASE + "/policies/" + pid).json()
            response = c.post(
                BASE + "/policies/" + pid + "/confirm-scope",
                json=dict(
                    extraction_digest=source["rules"]["extraction_digest"],
                    confirmed=True,
                    reason="合成正文及规则独立核对",
                    idempotency_key=str(uuid4()),
                ),
            )
            assert response.status_code == 200, response.text
            add_booking(env)
            cases = {}
            for name in (
                "normal",
                "reject",
                "return",
                "overlimit",
                "missing",
                "ai_failure",
                "revoked",
                "manual",
            ):
                detail = prepare(
                    env,
                    amount="1200.00" if name == "overlimit" else "680.00",
                    notes="SYNTHETIC_AI_FAILURE 一般核查建议不得升级为强制材料"
                    if name == "ai_failure"
                    else "正常公务住宿一晚；全部材料仅用于合成演示。",
                    facts={k: v for k, v in COMMON.items() if k != "invoice_verified"}
                    if name == "missing"
                    else COMMON,
                )
                cases[name] = dict(
                    claim_id=detail["claim_id"],
                    revision=detail["revision"],
                    stage=detail["stage"],
                    advisory=detail["advisory_analysis"]["ai_advisory_status"],
                )
                assert repo.erp_write_count(detail["claim_id"]) == 0
            save(gen / "cases.json", cases)
            save(
                gen / "fixture-summary.json",
                dict(
                    policy_id=pid,
                    cases=cases,
                    users={k: v["id"] for k, v in users.items()},
                    mock_ai=True,
                    mock_erp=True,
                    real_deepseek_calls=0,
                    real_bge_calls=0,
                    real_erp_calls=0,
                ),
            )
    # User consent: exact loopback origin only, synthetic cookies, test runtime only.
    wrapped = CORSMiddleware(
        app,
        allow_origins=[WEB],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
        expose_headers=["X-Request-ID"],
    )
    try:
        uvicorn.run(wrapped, host="127.0.0.1", port=8014, log_level="info")
    finally:
        repo.close()
        engine.dispose()


def stop():
    import signal

    manifest = active() / "processes.json"
    if not manifest.exists():
        return
    for label, entry in json.loads(manifest.read_text(encoding="utf-8")).items():
        pid = entry["pid"]
        command = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        cwd = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.splitlines()
        marker = "scripts.finance_workbench_demo" if label == "backend" else str(ROOT / "apps/web")
        state = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, encoding="utf-8"
        ).stdout.strip()
        if not command or state.startswith("Z"):
            continue
        if marker not in command or "n" + str(ROOT) not in cwd:
            raise RuntimeError("Refuse to stop process with mismatched ownership")
        os.kill(pid, signal.SIGTERM)
        print("Stopped owned", label, pid)
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    for _ in range(40):
        live = []
        for entry in entries.values():
            state = subprocess.run(
                ["ps", "-p", str(entry["pid"]), "-o", "stat="],
                capture_output=True,
                text=True,
                encoding="utf-8",
            ).stdout.strip()
            if state and not state.startswith("Z"):
                live.append(entry["pid"])
        if not live:
            manifest.rename(active() / ("stopped-" + uuid4().hex + ".json"))
            return
        time.sleep(0.25)
    raise RuntimeError("Owned processes still exiting; manifest retained for safe retry")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "start", "stop", "serve", "status"))
    parser.add_argument("--source-config", type=Path)
    args = parser.parse_args()
    import fcntl

    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    lifecycle_lock = (OUT / "lifecycle.lock").open("a", encoding="utf-8")
    if args.action != "serve":
        fcntl.flock(lifecycle_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if args.action == "init":
        if not args.source_config:
            parser.error("init requires --source-config (read DB URL only)")
        init(args.source_config)
    elif args.action == "start":
        start()
    elif args.action == "stop":
        stop()
    elif args.action == "serve":
        serve()
    else:
        print(active())


if __name__ == "__main__":
    main()
