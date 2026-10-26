"""Explicit paid smoke/API gates and browser evidence server. Never certifies M4B alone."""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.api.config import Settings  # noqa: E402
from integrations.llm.base import AnalysisRequest  # noqa: E402
from integrations.llm.deepseek import DeepSeekError, DeepSeekLLMProvider  # noqa: E402

C1, C4 = "CLM-2026-00001", "CLM-2026-00004"


def live_settings():
    if os.getenv("RUN_LIVE_LLM_TESTS") != "1":
        raise DeepSeekError("LIVE_GATE_REQUIRED")
    settings = Settings(
        app_env="test",
        llm_provider="deepseek",
        policy_retriever="pgvector",
        embedding_provider="remote",
    )
    # Missing key stops before any RAG, database or network calls.
    make_provider(settings)
    return settings


def make_provider(settings):
    return DeepSeekLLMProvider(
        api_key=settings.deepseek_api_key,
        model=settings.deepseek_model,
        base_url=settings.deepseek_base_url,
        timeout_seconds=settings.deepseek_timeout_seconds,
        max_tokens=settings.deepseek_max_tokens,
    )


def run_probe(settings):
    request = AnalysisRequest(
        claim_id="synthetic-smoke",
        evidence=[],
        facts={
            "city": "深圳",
            "amount": "680.00",
            "limit": "700.00",
            "rules_passed": "true",
            "hard_gates_passed": "true",
            "business_ambiguity": "true",
            "flight_cancellation": "true",
            "employee_explanation": "航班取消导致多住一晚，判断是否存在业务歧义。",
        },
    )
    result = asyncio.run(make_provider(settings).analyze(request))
    assert result.summary and result.provider == "deepseek"
    assert result.suggested_risk == "MEDIUM" and result.anomalies
    return {"RESULT": "PASS", **request._provider_evidence}


def checked_app(settings, repo, **adapters):
    from apps.api.main import create_app
    from rag.embeddings.remote import RemoteEmbeddingProvider
    from rag.retrieval.pgvector import PgPolicyCatalog, PgVectorPolicyRetriever

    app = create_app(settings, repo, **adapters)
    service = app.state.audit_service
    args = service.graph_factory.keywords
    assert isinstance(args["provider"], DeepSeekLLMProvider)
    assert isinstance(args["retriever"], PgVectorPolicyRetriever)
    assert isinstance(args["retriever"].embedding, RemoteEmbeddingProvider)
    assert isinstance(service.catalog, PgPolicyCatalog)
    return app


def backend_evidence(app, sql):
    from sqlalchemy import text

    service, repo = app.state.audit_service, app.state.repository
    retriever = service.graph_factory.keywords["retriever"]
    with retriever.engine.connect() as connection:
        postgres = connection.scalar(text("SHOW server_version"))
        vector = connection.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname='vector'")
        )
    claims = {}
    for cid in (C1, C4):
        claim = repo.get(cid)
        events = repo.events(cid)
        claims[cid] = {
            "status": claim.status.value,
            "erp_status": claim.erp_status.value,
            "erp_writes": repo.erp_write_count(cid),
            "events": [e.event_type for e in events],
            "llm_calls": [e.payload for e in events if e.event_type == "LLM_PROVIDER_CALL"],
        }
    return {
        "provider": "DeepSeekLLMProvider",
        "retriever": type(retriever).__name__,
        "embedding_provider": type(retriever.embedding).__name__,
        "postgres": postgres,
        "pgvector": vector,
        "vector_queries": retriever.vector_queries,
        "vector_sql": sql,
        "actual_embedding_model": retriever.embedding.actual_model,
        "actual_embedding_dimension": retriever.embedding.actual_dimension,
        "claims": claims,
    }


def review_body(op):
    return {
        "run_id": op["run_id"],
        "expected_revision": op["revision"],
        "decision": "APPROVE",
        "reviewer_id": "finance-m4b",
        "comment": "Verified cancellation and additional stay",
        "idempotency_key": "m4b-review",
    }


def audit(client, cid):
    response = client.post(f"/claims/{cid}/audit", json={"idempotency_key": "m4b-live-audit"})
    assert response.status_code == 202
    op = response.json()
    evidence = op["decision_result"]["policy_evidence"]
    assert evidence and all(p["policy_version_id"] == "POL-SZ-2026" for p in evidence)
    return op


def capture_sql(app):
    from sqlalchemy import event

    sql = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        if "<=>" in statement:
            sql.append(statement)  # Never store query params, vectors or connection URLs.

    engine = app.state.audit_service.graph_factory.keywords["retriever"].engine
    event.listen(engine, "before_cursor_execute", capture)
    return sql


def assert_successful_calls(repo, cid):
    calls = [e.payload for e in repo.events(cid) if e.event_type == "LLM_PROVIDER_CALL"]
    assert calls and calls[-1]["http_status"] == 200 and calls[-1]["schema_validated"]
    assert calls[-1]["actual_model"] == calls[-1]["configured_model"]
    return len(calls)


def run_verify(settings):
    from fastapi.testclient import TestClient

    from apps.api.repository import MockClaimRepository
    from integrations.documents.mock import MockDocumentParser

    report = {"smoke": run_probe(settings), "BROWSER_E2E": "NOT_RUN", "M4B_STATUS": "NOT_CERTIFIED"}
    repo = MockClaimRepository()
    try:
        app = checked_app(settings, repo)
        sql = capture_sql(app)
        with TestClient(app) as client:
            first = audit(client, C1)
            assert first["status"] == "COMPLETED"
            assert first["decision_result"]["risk_level"] == "LOW"
            assert first["decision_result"]["decision"] == "APPROVE"
            assert repo.get(C1).erp_status == "SUCCEEDED" and repo.erp_write_count(C1) == 1
            assert_successful_calls(repo, C1)
            pending = audit(client, C4)
            assert pending["status"] == "PENDING_HUMAN"
            assert pending["decision_result"]["risk_level"] == "MEDIUM"
            assert pending["decision_result"]["decision"] == "HUMAN_REVIEW"
            assert pending["decision_result"]["rule_results"][0]["outcome"] == "PASS"
            assert repo.erp_write_count(C4) == 0
            before = assert_successful_calls(repo, C4)

            async def snapshot():
                service = app.state.audit_service
                context = repo.run_context(C4, pending["run_id"])
                async with service.graph_session() as graph:
                    return await graph.aget_state(
                        {
                            "configurable": {
                                "thread_id": context["thread_id"],
                                "checkpoint_id": context["checkpoint_id"],
                            }
                        }
                    )

            state = asyncio.run(snapshot())
            assert state.next == ("human_review",) and any(t.interrupts for t in state.tasks)
            reviewed = client.post(f"/claims/{C4}/human-review", json=review_body(pending))
            assert reviewed.status_code == 200
            op = reviewed.json()
            resume = {
                "run_id": op["run_id"],
                "expected_revision": op["revision"],
                "idempotency_key": "m4b-resume",
            }
            final = client.post(f"/claims/{C4}/resume", json=resume)
            assert final.status_code == 202 and final.json()["status"] == "COMPLETED"
            assert repo.get(C4).erp_status == "SUCCEEDED" and repo.erp_write_count(C4) == 1
            assert assert_successful_calls(repo, C4) == before
            assert client.post(f"/claims/{C4}/resume", json=resume).json() == final.json()
            report["cases"] = backend_evidence(app, sql)
            assert len(sql) == 2
            assert report["cases"]["actual_embedding_model"] == "BAAI/bge-m3"
            assert report["cases"]["actual_embedding_dimension"] == 1024
    finally:
        repo.close()
    # Independent fixture, same workflow and authority, no production fixture modification.
    with tempfile.TemporaryDirectory(prefix="m4b-governance-") as directory:
        root = Path(directory)
        for name in ("claims", "documents"):
            data = json.loads(
                (ROOT / f"apps/api/mock_data/{name}.json").read_text(encoding="utf-8")
            )
            for item in data:
                if item["claim_id"] == C1 and item.get("expense"):
                    item["expense"]["amount"] = "780.00"
            (root / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
        repo = MockClaimRepository(root / "claims.json")
        try:
            app = checked_app(settings, repo, parser=MockDocumentParser(root / "documents.json"))
            with TestClient(app) as client:
                blocked = audit(client, C1)
                rule = blocked["decision_result"]["rule_results"][0]
                assert rule["actual"] == "780.00" and rule["limit"] == "700.00"
                assert rule["outcome"] == "FAIL" and rule["severity"] == "BLOCK"
                assert blocked["decision_result"]["decision"] == "REJECT"
                assert_successful_calls(repo, C1)
                review = client.post(f"/claims/{C1}/human-review", json=review_body(blocked))
                assert review.status_code == 409 and repo.erp_write_count(C1) == 0
                report["governance"] = {
                    "RESULT": "PASS",
                    "human_approve_http": 409,
                    "erp_writes": 0,
                }
        finally:
            repo.close()
    report["RESULT"] = "PASS"
    return report


def browser_server(settings, args):
    import uvicorn

    from apps.api.repository import MockClaimRepository

    if args.output is None or args.store is None or args.store.exists():
        raise DeepSeekError("FRESH_BROWSER_STORE_AND_OUTPUT_REQUIRED")
    args.store.parent.mkdir(parents=True, exist_ok=True)
    repo = MockClaimRepository(store_path=str(args.store))
    app = checked_app(settings, repo)
    sql = capture_sql(app)

    def record():
        report = backend_evidence(app, sql)
        report.update(M4B_STATUS="NOT_CERTIFIED", BROWSER_E2E="EXTERNAL_VERIFICATION_REQUIRED")
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    @app.middleware("http")
    async def record_response(request, call_next):
        response = await call_next(request)
        record()
        return response

    try:
        record()
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        repo.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["probe", "verify", "browser-server"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    try:
        settings = live_settings()
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.action == "browser-server":
            browser_server(settings, args)
            return 0
        report = run_probe(settings) if args.action == "probe" else run_verify(settings)
        code = 0
    except Exception as error:
        required = isinstance(error, DeepSeekError) and error.code in {
            "LIVE_GATE_REQUIRED",
            "API_KEY_REQUIRED",
        }
        report = {
            "RESULT": "USER_ACTION_REQUIRED" if required else "FAIL",
            "M4B_STATUS": "USER_ACTION_REQUIRED" if required else "FAIL",
            "ERROR_TYPE": type(error).__name__,
        }
        if isinstance(error, DeepSeekError):
            report.update(ERROR_CODE=error.code, HTTP_STATUS=error.status_code)
        code = 2 if required else 1
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
