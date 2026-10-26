"""Explicit bounded real pilot. Reuses snapshot graph; never creates the formal API."""

import argparse
import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, event, text

from apps.api.config import Settings
from apps.api.repository import MockClaimRepository
from apps.api.workflow import AuditService
from domain.schemas import LLMAnalysis
from evals.comparison.comparator import EFFECT_KEYS
from evals.comparison.runner import compare_run
from evals.dataset import DEFAULT_DATASET, ROOT, load_dataset
from evals.runner import MeasuredProvider, sha256
from evals.shadow_mode.http_budget import HTTPBudget, HTTPHooks, PilotStopped
from evals.shadow_mode.pilot_manifest import manifest as fixed_manifest
from evals.shadow_mode.runner import execute_case, run_manifest, shadow_summary
from evals.shadow_mode.store import ShadowStore
from integrations.erp.mock import MockERP
from integrations.llm.deepseek import DeepSeekLLMProvider
from integrations.retry import RetryPolicy
from rag.embeddings.remote import RemoteEmbeddingProvider
from rag.retrieval.pgvector import PgPolicyCatalog, PgVectorPolicyRetriever

DEFAULT_DIRECTORY = ROOT / "output/m5b-local/real-pilot/approved-20261008"
FIXED_PATH = ROOT / "docs/m5b-real-shadow-pilot-manifest.json"


def verify_selection():
    fixed = fixed_manifest()
    if json.loads(FIXED_PATH.read_text(encoding="utf-8")) != fixed:
        raise ValueError("Frozen selection changed")
    return fixed


@contextmanager
def formal_write_guard():
    counts = dict.fromkeys(EFFECT_KEYS, 0)

    def trap(key):
        def forbidden(*args, **kwargs):
            counts[key] += 1
            raise PilotStopped("FORMAL_ACTION_FORBIDDEN")

        return forbidden

    from contextlib import ExitStack

    with ExitStack() as stack:
        for cls, names, key in (
            (
                MockClaimRepository,
                ("__init__", "save_operation", "append_events"),
                "claim_mutation_count",
            ),
            (MockClaimRepository, ("write_erp",), "erp_write_count"),
            (MockERP, ("__init__", "write_decision"), "erp_write_count"),
            (
                AuditService,
                ("audit", "persist", "human_review", "resume"),
                "human_review_action_count",
            ),
        ):
            for name in names:
                stack.enter_context(patch.object(cls, name, trap(key)))
        yield counts


def formal_file_hashes(settings):
    path = Path(settings.mock_store_path)
    return {str(p): sha256(p) for p in (path, Path(str(path) + "-wal")) if p.is_file()}


class InjectedProvider:
    async def analyze(self, request):
        # Genuine schema failure, before any HTTP; never an invented model response.
        LLMAnalysis.model_validate({})


class SuppressedProvider:
    async def analyze(self, request):
        raise PilotStopped("INJECTED_UPSTREAM_RETRIEVAL_FAILURE")


class PilotRetriever:
    def __init__(self, real, row, historical_text):
        self.real, self.row, self.historical_text = real, row, historical_text
        self.probe_result = None
        self.failed = False

    async def retrieve(self, query, **filters):
        try:
            if self.row["artificial_fault_injection"] == "RETRIEVER_TIMEOUT_BEFORE_HTTP":
                raise TimeoutError("INJECTED_RETRIEVER_TIMEOUT")
            if self.row["probe"] == "HISTORICAL_SIMILARITY":
                query = self.historical_text
            return await self.real.retrieve(query, **filters)
        except Exception:
            self.failed = True
            raise


class RecordedProvider(MeasuredProvider):
    def __init__(self, real, hooks, retriever, *, source):
        super().__init__(real, source=source)
        self.hooks, self.retriever = hooks, retriever

    async def analyze(self, request):
        if self.retriever.failed:
            # Do not pay for downstream analysis after a failed retrieval.
            raise PilotStopped("UPSTREAM_RETRIEVAL_FAILED")
        try:
            return await super().analyze(request)
        finally:
            if self.hooks.last_id is not None:
                self.hooks.budget.record(self.hooks.last_id, evidence=request._provider_evidence)


def simulated_deepseek(request):
    body = json.loads(request.content)
    facts = json.loads(body["messages"][1]["content"])["facts"]
    ambiguity = facts.get("business_ambiguity") == "true"
    value = {
        "summary": "Simulated HTTP schema fixture; not a real model inference",
        "anomalies": ["STAY_OUTSIDE_APPROVED_TRIP_DATES"] if ambiguity else [],
        "suggested_risk": "MEDIUM" if ambiguity else "LOW",
        "confidence": 0.8,
        "provider": "deepseek",
        "model": body["model"],
    }
    return httpx.Response(
        200,
        json={
            "model": body["model"],
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(value)},
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        },
    )


def simulated_bge(request):
    body = json.loads(request.content)
    items = body["input"] if isinstance(body["input"], list) else [body["input"]]
    return httpx.Response(
        200,
        json={
            "model": "BAAI/bge-m3",
            "data": [{"index": i, "embedding": [1.0] + [0.0] * 1023} for i, _ in enumerate(items)],
        },
    )


def make_adapters(
    settings,
    engine,
    ledger,
    case_row,
    historical,
    *,
    dry_run=False,
    llm_transport=None,
    embedding_transport=None,
):
    if not dry_run and (llm_transport is not None or embedding_transport is not None):
        raise ValueError("Simulated transport prohibited in real pilot")
    llm_hooks = HTTPHooks(ledger, "deepseek", case_row["case_id"])
    bge_hooks = HTTPHooks(ledger, "bge-m3", case_row["case_id"])
    embedding = RemoteEmbeddingProvider(
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key,
        model_name=settings.embedding_model,
        dimension=settings.embedding_dimension,
        timeout=settings.embedding_timeout,
        max_attempts=2,
        batch_size=1,
        transport=embedding_transport,
        event_hooks=bge_hooks.hooks(),
    )
    real_retriever = PgVectorPolicyRetriever(engine, embedding)
    retriever = PilotRetriever(real_retriever, case_row, historical)
    real_llm = DeepSeekLLMProvider(
        api_key=settings.deepseek_api_key,
        model=settings.deepseek_model,
        base_url=settings.deepseek_base_url,
        timeout_seconds=settings.deepseek_timeout_seconds,
        max_tokens=settings.deepseek_max_tokens,
        transport=llm_transport,
        event_hooks=llm_hooks.hooks(asynchronous=True),
    )
    injection = case_row["artificial_fault_injection"]
    source = "simulated_deepseek_http" if dry_run else "deepseek"
    if injection:
        provider = MeasuredProvider(
            SuppressedProvider()
            if injection == "RETRIEVER_TIMEOUT_BEFORE_HTTP"
            else InjectedProvider(),
            source="injected",
        )
    else:
        provider = RecordedProvider(real_llm, llm_hooks, retriever, source=source)
    return (PgPolicyCatalog(engine), retriever, provider), real_retriever


def execution_class(result, row, *, dry_run):
    actual = result.evaluation_row["actual"]
    if row["artificial_fault_injection"]:
        return "INJECTED_SAFE_FALLBACK"
    if result.execution_status == "FAILED":
        return "EVALUATION_FAILED"
    errors = actual.get("errors", [])
    if errors:
        return (
            "RETRIEVAL_SAFE_FALLBACK"
            if any(e["node"] in ("rule_check", "retrieve_policy") for e in errors)
            else "LLM_SAFE_FALLBACK"
        )
    return ("SIMULATED_" if dry_run else "REAL_") + (
        "INFERENCE_HUMAN_REVIEW" if actual.get("requires_human") else "INFERENCE_COMPLETED"
    )


def case_stop_reason(result, row, real_retriever):
    actual = result.evaluation_row["actual"]
    if any(result.side_effects.values()):
        return "SIDE_EFFECT_ISOLATION_FAILED"
    if (
        row["expected_decision"] == "REJECT"
        and result.evaluation_row["expected"]["hard_block"]
        or actual.get("hard_block")
    ) and actual.get("decision") != "REJECT":
        return "HARD_BLOCK_VIOLATION"
    if result.execution_status == "FAILED":
        return "EVALUATION_FAILED"
    if row["artificial_fault_injection"]:
        if (
            result.agent_decision != "HUMAN_REVIEW"
            or result.execution_status != "DEPENDENCY_FAILED"
        ):
            return "INJECTION_NOT_SAFE"
        return None
    attempts = result.evaluation_row["llm_attempts"]
    if (
        result.execution_status != "COMPLETED"
        or not attempts
        or not attempts[-1].get("schema_validated")
        or attempts[-1].get("http_status") != 200
    ):
        return "REAL_DEPENDENCY_OR_SCHEMA_FAILED"
    if row["expected_policy"]["policy_version_id"] is not None:
        if (
            real_retriever.vector_queries != 1
            or real_retriever.embedding.actual_model != "BAAI/bge-m3"
            or real_retriever.embedding.actual_dimension != 1024
            or not actual.get("policy_evidence")
        ):
            return "REAL_RAG_FAILED"
        if row["smoke"] and (
            set(actual["policy_evidence"]) != {row["expected_policy"]["policy_version_id"]}
            or actual.get("authoritative_version_id") != row["expected_policy"]["policy_version_id"]
        ):
            return "SMOKE_POLICY_PROVENANCE_FAILED"
    return None


async def run_pilot(*, directory=DEFAULT_DIRECTORY, dry_run=False, approved=False, resume=False):
    if not dry_run and not approved:
        raise ValueError("Explicit paid pilot approval flag required")
    directory = Path(directory).resolve()
    if not dry_run and directory != DEFAULT_DIRECTORY.resolve():
        raise ValueError("Approved live pilot directory is fixed; budget cannot be reset")
    if not directory.is_relative_to((ROOT / "output/m5b-local").resolve()):
        raise ValueError("Pilot artifacts must be isolated under output/m5b-local")
    directory.mkdir(parents=True, exist_ok=True)
    fixed = verify_selection()
    if not dry_run:
        dry = json.loads((directory.parent / "dry-run/report.json").read_text(encoding="utf-8"))
        if (
            dry["engineering_status"] != "PASS"
            or not dry["dry_run"]
            or dry["manifest"]["pilot_selection_sha256"] != fixed["manifest_sha256"]
        ):
            raise ValueError("Successful matching zero-paid dry run required")
    # Process-only override; the .env and global process environment are not written.
    settings = Settings(
        policy_retriever="pgvector", embedding_provider="remote", llm_provider="deepseek"
    )
    if settings.embedding_model != "BAAI/bge-m3" or settings.embedding_dimension != 1024:
        raise ValueError("BGE model/schema mismatch")
    dataset = load_dataset()
    by_id = {c.case_id: c for c in dataset.cases}
    cases = [by_id[i] for i in fixed["execution_order"]]
    version = run_manifest(DEFAULT_DATASET, dataset, cases)
    version.update(
        mode="shadow-synthetic-simulated-real" if dry_run else "shadow-synthetic-real-deepseek",
        prompt_version=sha256(ROOT / "integrations/llm/deepseek.py"),
        pilot_selection_sha256=fixed["manifest_sha256"],
        configured_model=settings.deepseek_model,
        http_budget={"deepseek": 30, "bge-m3": 30},
        paid_approval=approved and not dry_run,
    )
    for name in (
        "evals/shadow_mode/real_pilot.py",
        "evals/shadow_mode/http_budget.py",
        "evals/comparison/comparator.py",
        "evals/comparison/runner.py",
        "rag/retrieval/pgvector.py",
        "rag/embeddings/remote.py",
        "integrations/llm/deepseek.py",
    ):
        version["source_sha256"][name] = sha256(ROOT / name)
    if not dry_run and (dry["manifest"]["source_sha256"] != version["source_sha256"]
                        or dry["manifest"]["prompt_version"] != version["prompt_version"]):
        raise ValueError("Code changed after dry run")
    identity_path = directory / "identity.json"
    if identity_path.exists():
        if not resume:
            raise ValueError("Existing pilot; explicit resume required; no outcome reruns")
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        if identity["manifest"] != version:
            raise ValueError("Pilot identity/code conflict")
        if (directory / "report.json").exists():
            # A completed/stopped run is immutable. Resume only reads its first result.
            return json.loads((directory / "report.json").read_text(encoding="utf-8"))
    else:
        if resume:
            raise ValueError("Unknown pilot resume")
        identity = {"run_id": str(uuid4()), "manifest": version}
        with identity_path.open("x", encoding="utf-8") as handle:
            json.dump(identity, handle, indent=2)
    run_id = identity["run_id"]
    ledger = HTTPBudget(directory / "http-budget.sqlite3", json.dumps(identity, sort_keys=True))
    store = ShadowStore(directory / "shadow.sqlite3")
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
        isolation_level="REPEATABLE READ",
        execution_options={"postgresql_readonly": True},
    )
    sql = []

    def record_sql(conn, cursor, statement, parameters, context, many):
        if "<=>" in statement:
            sql.append(statement)  # SQL text only; no connection URL or params/vectors.

    event.listen(engine, "before_cursor_execute", record_sql)
    results, stop_reason = [], None
    before = formal_file_hashes(settings)
    historical = next(
        p["content"]
        for p in json.loads((ROOT / dataset.policy_fixture).read_text(encoding="utf-8"))
        if p["document_id"] == "POL-SZ-2025"
    )
    try:
        with engine.connect() as conn:
            database = {
                "postgres": conn.scalar(text("SHOW server_version")),
                "pgvector": conn.scalar(
                    text("SELECT extversion FROM pg_extension WHERE extname='vector'")
                ),
                "read_only": conn.scalar(text("SHOW transaction_read_only")),
            }
        assert database["read_only"] == "on" and database["pgvector"]
        with store.exclusive(), formal_write_guard() as guarded:
            started = store.begin(run_id, version, resume=resume)
            for case in cases:
                row = next(r for r in fixed["cases"] if r["case_id"] == case.case_id)
                saved = store.get(run_id, case.case_id)
                if saved is not None:
                    results.append(saved)
                    continue
                if ledger.db.execute("SELECT reason FROM stop LIMIT 1").fetchone():
                    stop_reason = "PERSISTED_STOP"
                    break
                if any(v >= 30 for v in ledger.counts().values()):
                    stop_reason = "HTTP_BUDGET_EXHAUSTED"
                    break
                if ledger.records(case.case_id):
                    stop_reason = "AMBIGUOUS_UNSAVED_ATTEMPTS_NO_AUTOMATIC_RERUN"
                    break
                adapters, real_retriever = make_adapters(
                    settings,
                    engine,
                    ledger,
                    row,
                    historical,
                    dry_run=dry_run,
                    llm_transport=httpx.MockTransport(simulated_deepseek) if dry_run else None,
                    embedding_transport=httpx.MockTransport(simulated_bge) if dry_run else None,
                )
                result = await execute_case(
                    dataset,
                    case,
                    run_id,
                    version,
                    adapters=adapters,
                    retry_policies={
                        "llm": RetryPolicy(max_attempts=2),
                        "retriever": RetryPolicy(max_attempts=1),
                    },
                )
                for key in EFFECT_KEYS:
                    result.side_effects[key] += guarded[key]
                if formal_file_hashes(settings) != before:
                    result.side_effects["claim_mutation_count"] += 1
                records = ledger.records(case.case_id)
                result.token_usage = {
                    "paid_api_calls": sum(r["provider"] == "deepseek" for r in records)
                    if not dry_run
                    else 0,
                    "source": "simulated_http" if dry_run else "real_http_ledger",
                    "attempts": [
                        a.get("token_usage") for a in result.evaluation_row["llm_attempts"]
                    ],
                }
                result.evaluation_row["pilot"] = {
                    "execution_class": execution_class(result, row, dry_run=dry_run),
                    "artificial_fault_injection": row["artificial_fault_injection"],
                    "actual_model": next(
                        (
                            a.get("actual_model")
                            for a in reversed(result.evaluation_row["llm_attempts"])
                            if a.get("actual_model")
                        ),
                        None,
                    ),
                    "deepseek_http_attempts": sum(r["provider"] == "deepseek" for r in records),
                    "bge_http_attempts": sum(r["provider"] == "bge-m3" for r in records),
                    "vector_queries": real_retriever.vector_queries,
                    "embedding_model": real_retriever.embedding.actual_model,
                    "embedding_dimension": real_retriever.embedding.actual_dimension,
                }
                stop_reason = case_stop_reason(result, row, real_retriever)
                store.save(result)
                results.append(result)
                print(
                    json.dumps(
                        {
                            "case_id": case.case_id,
                            "status": result.execution_status,
                            "counts": ledger.counts(),
                            "stop_reason": stop_reason,
                        }
                    ),
                    flush=True,
                )
                if stop_reason:
                    ledger.stop(stop_reason)
                    break
                if len(results) == len(fixed["smoke_case_ids"]):
                    smoke = {"smoke_gate": "PASS", "run_id": run_id,
                             "case_ids": [r.case_id for r in results],
                             "counts": ledger.counts(), "dry_run": dry_run}
                    with (directory / "smoke.json").open("x", encoding="utf-8") as handle:
                        json.dump(smoke, handle, indent=2)
                    print(json.dumps(smoke), flush=True)
            report = shadow_summary(run_id, version, results, 24, started)
            report.update(
                mode=version["mode"],
                dry_run=dry_run,
                stop_reason=stop_reason,
                database=database,
                http_attempt_counts=ledger.counts(),
                http_ledger=ledger.records(),
                vector_sql=sql,
                configured_model=settings.deepseek_model,
                formal_store_unchanged=formal_file_hashes(settings) == before,
            )
            # Engineering does not require agreement with unvalidated business labels.
            report["engineering_status"] = (
                "PASS"
                if (
                    len(results) == 24
                    and not stop_reason
                    and not any(report["side_effects"].values())
                    and not report["metrics"]["hard_block_violations"]
                )
                else "FAIL"
            )
            report["shadow_status"] = report["engineering_status"]
            report["comparison"] = compare_run(report)
            if report["comparison"]["coverage"]["valid_agent_output"]["numerator"] != 24:
                report["engineering_status"] = report["shadow_status"] = "FAIL"
            store.finish(
                run_id, {k: v for k, v in report.items() if k not in ("cases", "comparison")}
            )
            with (directory / "report.json").open("x", encoding="utf-8") as handle:
                json.dump(report, handle, indent=2, ensure_ascii=False)
            return report
    finally:
        engine.dispose()
        store.close()
        ledger.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("dry-run", "execute"))
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIRECTORY)
    parser.add_argument("--approved-paid-pilot", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(
            run_pilot(
                directory=args.directory,
                dry_run=args.action == "dry-run",
                approved=args.approved_paid_pilot,
                resume=args.resume,
            )
        )
    except Exception as exc:
        # Never print arbitrary exception text/tracebacks containing URLs or credentials.
        print(json.dumps({"engineering_status": "FAIL", "error_type": type(exc).__name__}))
        return 1
    print(
        json.dumps(
            {
                "engineering_status": report["engineering_status"],
                "executed": report["persisted_case_count"],
                "http_attempt_counts": report["http_attempt_counts"],
                "stop_reason": report["stop_reason"],
            }
        )
    )
    return 0 if report["engineering_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
