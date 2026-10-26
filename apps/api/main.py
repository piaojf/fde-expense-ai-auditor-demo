from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from apps.api.config import Settings
from apps.api.errors import APIError, install_error_handlers
from apps.api.invoice_store import InvoiceStore
from apps.api.invoices import create_invoice_app
from apps.api.policies import create_policy_app
from apps.api.policy_publication import create_publication_app
from apps.api.repository import MockClaimRepository
from apps.api.submission import create_submission_app
from apps.api.workflow import AuditService
from domain.schemas import (
    AuditLogResponse,
    AuditRequest,
    ClaimDetail,
    ClaimList,
    ClaimStatus,
    ClaimSummary,
    DashboardMetrics,
    Decision,
    ErrorResponse,
    HealthResponse,
    HumanReviewRequest,
    OperationResponse,
    ResumeRequest,
)
from integrations.llm.deepseek import DeepSeekLLMProvider
from integrations.llm.reserved import OpenAIProvider, QwenProvider
from integrations.ocr.local import LocalOCRProvider
from rag.retrieval.factory import create_policy_adapters

Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]
ERRORS = {
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    501: {"model": ErrorResponse},
}


def create_app(
    settings: Settings | None = None, repository: MockClaimRepository | None = None,
    **adapters,
) -> FastAPI:
    settings = settings or Settings()
    compliance_provider = adapters.pop("compliance_provider", None)
    if settings.p4b_advisory_enabled and not settings.p4b_enabled:
        raise ValueError("Advisory mode requires P4B enterprise authority")
    if settings.p4b_enabled and not (settings.p4a_enabled and settings.enterprise_enabled):
        raise ValueError("P4B requires the paired P4A enterprise authority")
    if settings.app_env != "test" and adapters.get("business_date_provider") is not None:
        raise ValueError("Simulated business dates are restricted to test configuration")
    if adapters.get("provider") is None:
        if settings.llm_provider == "deepseek":
            adapters["provider"] = DeepSeekLLMProvider(
                api_key=settings.deepseek_api_key, model=settings.deepseek_model,
                base_url=settings.deepseek_base_url,
                timeout_seconds=settings.deepseek_timeout_seconds,
                max_tokens=settings.deepseek_max_tokens,
            )
        else:
            providers = {"openai": OpenAIProvider, "qwen": QwenProvider}
            adapters["provider"] = (
                providers[settings.llm_provider]() if settings.llm_provider != "mock" else None
            )
    repo = repository or MockClaimRepository(
        store_path=None if settings.app_env == "test" else settings.mock_store_path
    )
    policy_engine = None
    if settings.policy_retriever == "pgvector":
        if "retriever" in adapters or "catalog" in adapters:
            raise ValueError("PgVector mode requires the paired PostgreSQL authority adapters")
        policy_engine, adapters["catalog"], adapters["retriever"] = create_policy_adapters(settings)
    processor = LocalOCRProvider(binary=settings.ocr_binary, languages=settings.ocr_languages,
                                 tessdata=settings.ocr_tessdata)
    ocr_provider = adapters.pop("ocr_provider", None) or processor
    invoice_store = InvoiceStore(repo, processor)
    repo.invoice_store = invoice_store
    policy_app = create_policy_app(settings, processor, store=adapters.pop("policy_store", None))
    if settings.enterprise_enabled:
        adapters["mandatory_finance_review"] = True
    if settings.p4a_enabled:
        from apps.api.employee_hotel import EmployeeHotelFacts
        from apps.api.hotel_adapter import HotelCatalog
        from apps.api.hotel_publication import HotelPublication

        if not settings.enterprise_enabled or policy_app.state.store is None:
            raise ValueError("P4A requires enterprise mode and PostgreSQL policy storage")
        legacy = adapters.get("catalog")
        retrieval = adapters.get("retriever")
        if (
            legacy is None or not hasattr(legacy, "engine")
            or retrieval is None or not hasattr(retrieval, "engine")
            or not hasattr(retrieval, "embedding")
            or legacy.engine.url != retrieval.engine.url
            or legacy.engine.url != policy_app.state.store.engine.url
        ):
            raise ValueError("P4A requires paired PostgreSQL authority and retrieval")
        adapters["catalog"] = HotelCatalog(legacy, EmployeeHotelFacts(repo), legacy.engine)
    service = AuditService(repo, **adapters)

    @asynccontextmanager
    async def lifespan(app):
        try:
            if settings.enterprise_enabled:
                await app.state.enterprise_app.state.initialize()
            yield
        finally:
            if policy_app.state.engine is not None:
                policy_app.state.engine.dispose()
            if policy_app.state.budget is not None:
                policy_app.state.budget.close()
            if policy_engine is not None:
                policy_engine.dispose()
            if repository is None:
                repo.close()

    app = FastAPI(
        title="FDE Expense AI Auditor",
        version="0.1.0",
        description="M0 read-only mock foundation. Audit operations return 501.",
        lifespan=lifespan,
    )
    app.state.repository = repo
    app.state.audit_service = service
    app.state.policy_retriever = settings.policy_retriever
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=settings.enterprise_enabled,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
        expose_headers=["X-Request-ID"],
    )
    install_error_handlers(app)

    def get_claim(claim_id: str) -> ClaimDetail:
        claim = repo.get(claim_id)
        if claim is None:
            raise APIError(404, "CLAIM_NOT_FOUND", "Claim not found")
        return claim

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health():
        return HealthResponse()

    @app.get("/claims", response_model=ClaimList, responses=ERRORS, tags=["claims"])
    def list_claims(limit: Limit = 20, offset: Offset = 0, status: ClaimStatus | None = None):
        claims = repo.list(status)
        summaries = [
            ClaimSummary.model_validate(
                c.model_dump(exclude={"documents", "erp_status", "active_run_id", "data_source"})
            )
            for c in claims[offset : offset + limit]
        ]
        return ClaimList(items=summaries, total=len(claims), limit=limit, offset=offset)

    @app.get("/claims/{claim_id}", response_model=ClaimDetail, responses=ERRORS, tags=["claims"])
    def claim_detail(claim_id: str):
        return get_claim(claim_id)

    @app.post(
        "/claims/{claim_id}/audit",
        status_code=202,
        response_model=OperationResponse,
        responses=ERRORS,
        tags=["workflow"],
    )
    async def audit(claim_id: str, body: AuditRequest):
        return await service.audit(claim_id, body)

    @app.post(
        "/claims/{claim_id}/human-review",
        response_model=OperationResponse,
        responses=ERRORS,
        tags=["workflow"],
    )
    async def human_review(claim_id: str, body: HumanReviewRequest):
        return await service.human_review(claim_id, body)

    @app.post(
        "/claims/{claim_id}/resume",
        status_code=202,
        response_model=OperationResponse,
        responses=ERRORS,
        tags=["workflow"],
    )
    async def resume(claim_id: str, body: ResumeRequest):
        return await service.resume(claim_id, body)

    @app.get(
        "/claims/{claim_id}/audit-log",
        response_model=AuditLogResponse,
        responses=ERRORS,
        tags=["audit"],
    )
    def audit_log(claim_id: str, limit: Limit = 20, offset: Offset = 0):
        get_claim(claim_id)
        events = repo.events(claim_id)
        return AuditLogResponse(
            claim_id=claim_id,
            items=events[offset : offset + limit],
            total=len(events),
            limit=limit,
            offset=offset,
        )

    @app.get("/dashboard/metrics", response_model=DashboardMetrics, tags=["dashboard"])
    def dashboard():
        claims = repo.list()
        statuses = {s: sum(c.status == s for c in claims) for s in ClaimStatus}
        decisions = {
            d: sum(
                c.decision_result is not None and c.decision_result.decision == d for c in claims
            )
            for d in Decision
        }
        return DashboardMetrics(
            total_claims=len(claims),
            by_status=statuses,
            by_decision=decisions,
            pending_human=statuses[ClaimStatus.PENDING_HUMAN],
            pending_erp=statuses[ClaimStatus.PENDING_ERP],
        )

    submission_app = create_submission_app(repo, service)
    app.mount("/api/v1.1", submission_app)
    invoice_app = create_invoice_app(
        repo, service, submission_app, invoice_store, ocr_provider,
    )
    app.mount("/api/v1.2", invoice_app)
    app.mount("/api/v1.3", policy_app)
    publication_app = create_publication_app(
        settings, policy_app.state.store, getattr(adapters.get("retriever"), "embedding", None),
    )
    app.mount("/api/v1.4", publication_app)
    app.state.publication_app = publication_app
    app.state.policy_app = policy_app
    app.state.invoice_store = invoice_store
    if settings.enterprise_enabled:
        from apps.api.enterprise import EnterpriseBoundary, create_enterprise_app

        enterprise_app = create_enterprise_app(
            settings, repo, service, invoice_app, policy_app, publication_app,
        )
        if settings.p4a_enabled:
            from apps.api.hotel_api import install_hotel_api

            install_hotel_api(
                enterprise_app, settings, repo, service, enterprise_app.state.store,
                policy_app.state.store,
                HotelPublication(policy_app.state.store, adapters["retriever"].embedding,
                                 app_env=settings.app_env),
                adapters.get("business_date_provider"),
            )
        if settings.p4a_enabled:
            from apps.api.employee_hotel import install_employee_hotel_api

            install_employee_hotel_api(enterprise_app, repo, service)
        app.mount("/api/v1.5", enterprise_app)
        app.state.enterprise_app = enterprise_app
        if settings.p4b_enabled:
            from apps.api.compliance_api import install_compliance_api

            install_compliance_api(
                enterprise_app, settings, repo, service, compliance_provider,
                adapters.get("business_date_provider"),
            )
        app.add_middleware(EnterpriseBoundary)
    return app


app = create_app()
