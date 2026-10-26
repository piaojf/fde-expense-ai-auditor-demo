"""Zero-paid verification of actual provider HTTP hooks, caps and Shadow composition."""

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from apps.api.config import Settings
from evals.comparison.runner import compare_run
from evals.dataset import ROOT
from evals.shadow_mode import real_pilot
from evals.shadow_mode.http_budget import HTTPBudget, HTTPHooks, PilotStopped
from integrations.llm.base import AnalysisRequest
from integrations.llm.deepseek import DeepSeekLLMProvider, DeepSeekOutputError
from integrations.retry import RetryPolicy, call_with_retry
from rag.embeddings.remote import RemoteEmbeddingProvider
from rag.ingestion.postgres import PostgresPolicyIngestor, load_seed


@pytest.mark.parametrize("provider", ["deepseek", "bge-m3"])
def test_durable_cap_stops_both_providers_before_send(tmp_path, provider):
    path = tmp_path / "budget.sqlite3"
    ledger = HTTPBudget(path, "frozen-pilot")
    for _ in range(30):
        ledger.reserve(provider, "case")
    ledger.close()
    ledger = HTTPBudget(path, "frozen-pilot")
    try:
        for p in ("deepseek", "bge-m3"):
            with pytest.raises(PilotStopped):
                ledger.reserve(p, "next")
        assert ledger.counts()[provider] == 30
    finally:
        ledger.close()
    with pytest.raises(ValueError):
        HTTPBudget(path, "different-pilot")


def test_concurrent_budget_connections_cannot_exceed_cap(tmp_path):
    path = tmp_path / "budget.sqlite3"
    ledger = HTTPBudget(path, "same")
    ledger.close()

    def send(_):
        ledger = HTTPBudget(path, "same")
        try:
            ledger.reserve("deepseek", "case")
            return 1
        except PilotStopped:
            return 0
        finally:
            ledger.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(send, range(35))) == 30


def test_embedding_inner_retries_all_counted_and_stop_before_third_send(tmp_path):
    ledger = HTTPBudget(tmp_path / "bge.sqlite3", "retry", limit=2)
    hooks = HTTPHooks(ledger, "bge-m3", "case")
    sent = []

    def handler(request):
        sent.append(True)
        return httpx.Response(503)

    embedding = RemoteEmbeddingProvider(
        base_url="https://mock.example/v1",
        api_key="mock-test-only",
        model_name="BAAI/bge-m3",
        max_attempts=3,
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
        event_hooks=hooks.hooks(),
    )
    try:
        with pytest.raises(PilotStopped):
            embedding.embed_query("深圳住宿限额")
        assert len(sent) == ledger.counts()["bge-m3"] == 2
    finally:
        ledger.close()


def test_deepseek_retries_network_failure_counted_before_budget_stop(tmp_path):
    from agent.state import ClaimState
    from integrations.retry import DependencyFailure

    ledger = HTTPBudget(tmp_path / "ds.sqlite3", "retry", limit=2)
    hooks = HTTPHooks(ledger, "deepseek", "case")
    sent = []

    def handler(request):
        sent.append(True)
        raise httpx.ConnectError("simulated unavailable")

    provider = DeepSeekLLMProvider(
        api_key="mock-test-only",
        transport=httpx.MockTransport(handler),
        event_hooks=hooks.hooks(asynchronous=True),
    )
    try:
        with pytest.raises(DependencyFailure):
            asyncio.run(
                call_with_retry(
                    ClaimState(claim_id="case", run_id=uuid4()),
                    "llm",
                    "llm_analysis",
                    lambda: provider.analyze(
                        AnalysisRequest(claim_id="case", facts={}, evidence=[])
                    ),
                    RetryPolicy(max_attempts=3, backoff=0),
                )
            )
        assert len(sent) == ledger.counts()["deepseek"] == 2
    finally:
        ledger.close()


def test_schema_failure_keeps_usage_and_auth_failure_stops_other_provider(tmp_path):
    ledger = HTTPBudget(tmp_path / "ds.sqlite3", "schema")
    hooks = HTTPHooks(ledger, "deepseek", "case")
    provider = DeepSeekLLMProvider(
        api_key="mock-test-only",
        event_hooks=hooks.hooks(asynchronous=True),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "model": "deepseek-flash",
                    "choices": [],
                    "usage": {"prompt_tokens": 123, "completion_tokens": 2, "total_tokens": 125},
                },
            )
        ),
    )
    request = AnalysisRequest(claim_id="case", facts={}, evidence=[])
    try:
        with pytest.raises(DeepSeekOutputError):
            asyncio.run(provider.analyze(request))
        assert request._provider_evidence["token_usage"]["total_tokens"] == 125
        assert not request._provider_evidence["schema_validated"]
        with httpx.Client(
            event_hooks=hooks.hooks(), transport=httpx.MockTransport(lambda _: httpx.Response(401))
        ) as client:
            client.post("https://mock.example/api")
        with pytest.raises(PilotStopped):
            ledger.reserve("bge-m3", "next")
        assert ledger.counts() == {"deepseek": 2, "bge-m3": 0}
    finally:
        ledger.close()


def test_formal_guard_counts_and_blocks_all_entrypoints():
    with real_pilot.formal_write_guard() as effects:
        with pytest.raises(PilotStopped):
            real_pilot.MockERP()
        assert effects["erp_write_count"] == 1
        with pytest.raises(PilotStopped):
            real_pilot.MockClaimRepository()
        assert effects["claim_mutation_count"] == 1


def test_live_approval_cannot_reset_budget_by_new_directory(tmp_path):
    with pytest.raises(ValueError, match="directory is fixed"):
        asyncio.run(real_pilot.run_pilot(directory=tmp_path, approved=True))


@pytest.fixture
def pilot_pg():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Disposable PostgreSQL gate required")
    url = make_url(url)
    assert url.database.startswith("fde_m0_test_")
    schema = "pilot_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    test_url = url.update_query_dict({"options": f"-csearch_path={schema},public"})
    engine = create_engine(test_url)
    try:
        with engine.begin() as conn:
            config = Config("alembic.ini")
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
        embedding = RemoteEmbeddingProvider(
            base_url="https://mock.example/v1",
            api_key="mock-test-only",
            model_name="BAAI/bge-m3",
            transport=httpx.MockTransport(real_pilot.simulated_bge),
        )
        PostgresPolicyIngestor(engine, embedding).ingest(load_seed())
        yield test_url
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.integration
def test_full_zero_paid_pilot_and_comparator_with_real_sql(pilot_pg, monkeypatch):
    settings = Settings(
        _env_file=None,
        database_url=pilot_pg.render_as_string(hide_password=False),
        deepseek_api_key="mock-test-only",
        embedding_api_key="mock-test-only",
        embedding_base_url="https://mock.example/v1",
        llm_provider="deepseek",
        policy_retriever="pgvector",
        embedding_provider="remote",
    )
    monkeypatch.setattr(real_pilot, "Settings", lambda **kwargs: settings)

    def forbidden(*args, **kwargs):
        raise AssertionError("Zero-paid test cannot use a real HTTP transport")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    root = ROOT / "output/m5b-local"
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=root) as temporary:
        directory = Path(temporary) / "dry-run"
        result = asyncio.run(real_pilot.run_pilot(directory=directory, dry_run=True))
        assert result["engineering_status"] == "PASS"
        assert result["persisted_case_count"] == 24
        assert result["http_attempt_counts"] == {"deepseek": 22, "bge-m3": 19}
        assert not any(result["side_effects"].values())
        assert result["metrics"]["paid_api_calls"] == 0
        assert result["comparison"]["coverage"]["valid_agent_output"]["numerator"] == 24
        injections = [
            c for c in result["cases"] if c["evaluation_row"]["pilot"]["artificial_fault_injection"]
        ]
        assert len(injections) == 2
        assert all(c["evaluation_row"]["pilot"]["deepseek_http_attempts"] == 0 for c in injections)
        assert len(result["vector_sql"]) == 19
        assert result["database"]["read_only"] == "on"
        assert all(c["model_provider"] != "mock" for c in result["cases"])
        tampered = json.loads(json.dumps(result))
        tampered["manifest"]["paid_approval"] = False
        tampered["manifest"]["mode"] = "shadow-synthetic-real-deepseek"
        tampered["mode"] = "shadow-synthetic-real-deepseek"
        with pytest.raises(ValueError):
            compare_run(tampered)
        with pytest.raises(ValueError, match="Existing pilot"):
            asyncio.run(real_pilot.run_pilot(directory=directory, dry_run=True))
