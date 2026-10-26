"""Explicit local runtime binding for P0 rehearsal and preserved hotel demo data.

Never initializes a generation, publishes a policy, or replenishes a call budget.
Rehearsal uses authored V4 responses through the real HTTP/schema adapter.
"""

import argparse
import json
import os
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/p0"
API = "http://127.0.0.1:8014"
WEB = "http://127.0.0.1:5173"


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)


def binding(path):
    from sqlalchemy.engine import make_url

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("mode") not in {"rehearsal", "preserve"}:
        raise ValueError("Explicit rehearsal/preserve mode required")
    # Scope simulated claim dates to this binding, independently of the real
    # publication clock. Keep existing rehearsal bindings reproducible.
    business_date = data.get("business_date", "2027-01-05")
    if (not isinstance(business_date, str)
            or date.fromisoformat(business_date).isoformat() != business_date):
        raise ValueError("Business date must be an ISO date (YYYY-MM-DD)")
    data["business_date"] = business_date
    gen = Path(data["generation"])
    if not gen.is_absolute() or gen.is_symlink() or not gen.is_dir():
        raise ValueError("Existing absolute generation directory required")
    for name in ("portal.sqlite3", "fixture-summary.json", "database-url.txt"):
        if not (gen / name).is_file():
            raise ValueError("Existing initialized generation required: " + name)
    url = make_url((gen / "database-url.txt").read_text(encoding="utf-8").strip())
    if (url.host not in {"localhost", "127.0.0.1"} or url.port != 55432
            or not (url.database or "").startswith("fde_m0_test_finance_browser_")):
        raise ValueError("Allowlisted loopback finance database required")
    if data["mode"] == "rehearsal":
        marker = gen / "p0-rehearsal.json"
        if (not marker.is_file()
                or json.loads(marker.read_text(encoding="utf-8")).get("isolated") is not True):
            raise ValueError("Explicit isolated rehearsal marker required")
        if (gen / "deepseek-live.json").exists():
            raise ValueError("Rehearsal must not contain a live model profile")
    elif not (gen / "deepseek-live.json").is_file():
        raise ValueError("Preserve requires the existing live profile; no Mock fallback")
    tessdata = Path(data["ocr_tessdata"])
    if not tessdata.is_absolute() or any(
        not (tessdata / (lang + ".traineddata")).is_file() for lang in ("chi_sim", "eng")
    ):
        raise ValueError("Existing chi_sim+eng assets required")
    return data, gen, url


def preflight(path):
    if (ROOT / ".env").exists():
        raise ValueError("Use the dedicated source worktree without a .env")
    data, gen, url = binding(path)
    with sqlite3.connect("file:" + str(gen / "portal.sqlite3") + "?mode=ro", uri=True) as db:
        pending = db.execute(
            "SELECT count(*) FROM enterprise_claims WHERE stage IN "
            "('AI_AUDITING','FINANCE_APPLYING','ERP_SYNC_PENDING')"
        ).fetchone()[0]
        if pending:
            raise ValueError("Pending operations require separate review before startup")
    return data, gen, url


def clean_env():
    env = os.environ.copy()
    for key in ("DEEPSEEK_API_KEY", "EMBEDDING_API_KEY", "DATABASE_URL", "TEST_DATABASE_URL",
                "RUN_LIVE_LLM_ACCEPTANCE", "RUN_LIVE_LLM_TESTS"):
        env.pop(key, None)
    env.update(
        APP_ENV="test", LLM_PROVIDER="mock", POLICY_RETRIEVER="inmemory",
        POLICY_DEMO_ENABLED="false", POLICY_REMOTE_ENABLED="false",
        POLICY_REMOTE_MAX_REQUESTS="0", P4B_LIVE_LLM="false", P4A_ENABLED="false",
        P4B_ENABLED="false", P4B_ADVISORY_ENABLED="false", ENTERPRISE_ENABLED="false",
        EMBEDDING_PROVIDER="deterministic", MOCK_STORE_PATH=str(OUT / "import-safety.sqlite3"),
    )
    return env


class OfflineV4:
    """Explicit authored Mock; transport/schema/semantic validation remain real."""

    protocol_version = "p4b.advisory.4"
    model = "mock-authored-advisory-v4"

    async def analyze(self, request):
        import httpx

        from integrations.llm.advisory_v4 import AdvisoryDeepSeekProviderV4
        from integrations.llm.evidence_selector import selector_manifest

        def respond(wire):
            # The real adapter's _body pins the selector before invoking transport.
            # Read that exact manifest here; never prepare or replace it ourselves.
            manifest = selector_manifest(request)
            sent = json.loads(json.loads(wire.content)["messages"][1]["content"])
            if sent["evidence_snapshot_id"] != manifest["evidence_snapshot_id"]:
                raise ValueError("REHEARSAL_EVIDENCE_SNAPSHOT_MISMATCH")
            decision = request.facts["declared_amount_check"]["decision"]
            city_fact = request.facts["trusted"].get("city_fact") or {}
            city = city_fact.get("value", {})
            city_keys = [e["short_id"] for e in manifest["entries"]
                         if e["source_position"].get("cells")
                         == city_fact.get("original_cells")]
            if not city_keys:
                city_keys = [e["short_id"] for e in manifest["entries"]
                             if e["source_position"].get("table_index") == 4][:1]
            payload = dict(
                schema_version=self.protocol_version,
                evidence_snapshot_id=manifest["evidence_snapshot_id"],
                risk_level={"PASS": "LOW", "FAIL": "HIGH"}.get(decision, "MEDIUM"),
                recommendation={"PASS": "RECOMMEND_APPROVAL", "FAIL": "RECOMMEND_REJECTION"}
                .get(decision, "ESCALATE"),
                summary="依据现有申报与有效制度进行金额比较，最终由财务决定。",
                city_classification=dict(
                    city=city.get("name", request.facts["candidate"]["city"]),
                    tier=city.get("tier", "UNKNOWN"),
                    explanation="引用制度原文中的城市分类。", policy_ref_keys=city_keys),
                issues=[], missing_evidence=[], requires_human_review=True,
                policy_ref_keys=[e["short_id"] for e in manifest["entries"]
                                 if e["clause_id"] == "第十五条"],
                fact_refs=["candidate.amount", "account.grade", "declared_amount_check"],
            )
            content = ("{invalid-json" if request.facts["candidate"]["notes"].startswith(
                "SYNTHETIC_P0_AI_FAILURE") else json.dumps(payload, ensure_ascii=False))
            return httpx.Response(200, json=dict(
                model=self.model, choices=[dict(finish_reason="stop", message=dict(
                    role="assistant", content=content))]))

        transport = httpx.MockTransport(respond)
        return await AdvisoryDeepSeekProviderV4(
            api_key="synthetic-local-only", model=self.model, max_tokens=4096,
            transport=transport,
        ).analyze(request)


def install_identity(app, identity):
    """Keep version verification inside the existing authenticated enterprise API."""
    @app.state.enterprise_app.get("/p0-runtime")
    def current_identity():
        return identity


def local_settings(ocr_tessdata):
    from apps.api.config import Settings

    return Settings(
        _env_file=None, app_env="test", llm_provider="mock", deepseek_api_key=None,
        embedding_api_key=None, embedding_provider="deterministic", p4a_enabled=True,
        p4b_enabled=True, p4b_advisory_enabled=True, p4b_advisory_protocol="p4b.advisory.4",
        enterprise_enabled=True, enterprise_cookie_secure=False, cors_origins=[WEB],
        # Enterprise handlers delegate to the capability-gated local policy app.
        # Keep this internal credential process-local; never send it to the browser.
        policy_demo_enabled=True, policy_demo_token=secrets.token_urlsafe(32),
        policy_draft_embedding="deterministic",
        policy_remote_enabled=False, policy_remote_max_requests=0,
        ocr_languages="chi_sim+eng", ocr_tessdata=ocr_tessdata,
    )


def serve(path):
    data, gen, url = preflight(path)
    os.environ.update(clean_env())
    import uvicorn
    from sqlalchemy import create_engine

    from apps.api.main import create_app
    from apps.api.policy_store import PolicyDraftStore
    from apps.api.repository import MockClaimRepository
    from integrations.ocr.local import LocalOCRProvider
    from integrations.policies.parser import PolicyParser
    from rag.embeddings.providers import DeterministicTestEmbedding
    from rag.retrieval.pgvector import PgPolicyCatalog, PgVectorPolicyRetriever

    engine = create_engine(url)
    embedding = DeterministicTestEmbedding()
    ocr = LocalOCRProvider(languages="chi_sim+eng", tessdata=data["ocr_tessdata"])
    draft = PolicyDraftStore(engine, PolicyParser(ocr), lambda _: embedding)
    settings = local_settings(data["ocr_tessdata"])
    provider, legacy = OfflineV4(), None
    if data["mode"] == "preserve":
        from integrations.llm.budgeted import BudgetedLLM
        from integrations.llm.deepseek import DeepSeekLLMProvider
        from scripts.finance_workbench_ai import load_live_settings

        live = load_live_settings(gen)
        if live.p4b_advisory_protocol != "p4b.advisory.4":
            raise ValueError("Existing V4 profile required")
        settings = settings.model_copy(update={
            key: getattr(live, key) for key in (
                "llm_provider", "deepseek_api_key", "deepseek_model", "deepseek_base_url",
                "deepseek_timeout_seconds", "deepseek_max_tokens", "p4b_max_tokens",
                "p4b_live_llm", "p4b_live_budget_path", "p4b_live_max_requests",
                "p4b_advisory_protocol",
            )
        })
        legacy = BudgetedLLM(DeepSeekLLMProvider(
            api_key=live.deepseek_api_key, model=live.deepseek_model,
            base_url=live.deepseek_base_url, timeout_seconds=live.deepseek_timeout_seconds,
            max_tokens=live.deepseek_max_tokens,
        ), live.p4b_live_budget_path, live.p4b_live_max_requests)
        provider = None
    repo = MockClaimRepository(store_path=str(gen / "portal.sqlite3"))
    app = create_app(
        settings, repo, policy_store=draft, catalog=PgPolicyCatalog(engine),
        retriever=PgVectorPolicyRetriever(engine, embedding), compliance_provider=provider,
        provider=legacy, business_date_provider=lambda: date.fromisoformat(data["business_date"]),
    )

    install_identity(app, dict(
            source_sha=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                               text=True, encoding="utf-8").strip(),
            source_root=str(ROOT), mode=data["mode"], database=url.database,
            generation=gen.name, protocol="p4b.advisory.4",
            configured_model=settings.deepseek_model if provider is None else provider.model,
            live_model_configured=settings.p4b_live_llm,
            retrieval="LOCAL_TEXT_ORIGINAL_DOCX / deterministic test embeddings",
            erp="MockERP", business_date=data["business_date"],
    ))

    try:
        uvicorn.run(app, host="127.0.0.1", port=8014, log_level="info")
    finally:
        repo.close()
        engine.dispose()
        if legacy is not None:
            legacy.db.close()


def start(path):
    import httpx

    data, _, _ = preflight(path)
    for port in (8014, 5173):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError("Occupied port; refuse to replace any process")
    state = OUT / data["mode"]
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = clean_env()
    commands = {
        "backend": [sys.executable, "-m", "scripts.p0_runtime", "serve", "--binding", str(path)],
        "frontend": [str(ROOT / "apps/web/node_modules/.bin/vite"), "apps/web", "--host",
                     "127.0.0.1", "--port", "5173", "--strictPort"],
    }
    entries = {}
    for name, command in commands.items():
        if name == "frontend":
            env.update(VITE_API_BASE=API, VITE_USE_MOCK_API="false", VITE_ENTERPRISE_MODE="true",
                       VITE_P4A_ENABLED="true", VITE_P4B_ENABLED="true", VITE_M6_DEMO="true",
                       VITE_M6_BUSINESS_DATE=data["business_date"])
        with (state / (name + ".log")).open("ab") as log:
            proc = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        entries[name] = dict(pid=proc.pid, command=command, cwd=str(ROOT))
        save(state / "processes.json", entries)
        for _ in range(100):
            if proc.poll() is not None:
                raise RuntimeError("Owned process exited: " + name)
            try:
                # Liveness is public. Identity is checked only after browser login.
                if httpx.get(API + "/health" if name == "backend" else WEB,
                             timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        else:
            raise RuntimeError("Owned service did not become ready: " + name)
    print(json.dumps(dict(mode=data["mode"], processes=entries)))


def stop(path):
    data, _, _ = binding(path)
    manifest = OUT / data["mode"] / "processes.json"
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    for name, entry in entries.items():
        pid = entry["pid"]
        command = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True, encoding="utf-8").stdout.strip()
        if not command:
            continue
        cwd = subprocess.check_output(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                                      text=True, encoding="utf-8").splitlines()
        marker = "scripts.p0_runtime" if name == "backend" else str(ROOT / "apps/web")
        if marker not in command or "n" + str(ROOT) not in cwd:
            raise RuntimeError("Refuse mismatched process ownership")
        os.kill(pid, signal.SIGTERM)
    print("Stopped owned " + data["mode"] + " processes")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "start", "serve", "stop"])
    parser.add_argument("--binding", required=True, type=Path)
    args = parser.parse_args()
    path = args.binding.resolve(strict=True)
    if args.command == "check":
        data, gen, url = preflight(path)
        print(json.dumps(dict(mode=data["mode"], generation=gen.name, database=url.database)))
    else:
        {"start": start, "serve": serve, "stop": stop}[args.command](path)


if __name__ == "__main__":
    main()
