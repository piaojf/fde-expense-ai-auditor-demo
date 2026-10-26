"""Independent versioned API; mounting it leaves the frozen root OpenAPI untouched."""

import asyncio

from fastapi import FastAPI

from apps.api.errors import APIError, install_error_handlers
from domain.schemas import AuditRequest, LLMAnalysis, OperationResponse
from domain.submission import CreateClaimRequest, SubmissionFields, SubmissionResponse


def create_submission_app(repository, service):
    app = FastAPI(title="Claim Submission", version="1.1.0")
    install_error_handlers(app)
    lock = asyncio.Lock()

    def detail(claim_id):
        raw = repository.submission(claim_id)
        if raw is None:
            raise APIError(404, "CLAIM_NOT_FOUND", "Dynamic submission not found")
        claim = service.get_claim(claim_id)
        fields = SubmissionFields.model_validate(raw)
        context = (
            repository.run_context(claim_id, claim.active_run_id) if claim.active_run_id else None
        )
        operation = (
            OperationResponse(
                claim_id=claim_id,
                run_id=claim.active_run_id,
                status=claim.status,
                revision=context["revision"],
                decision_result=claim.decision_result,
            )
            if context
            else None
        )
        analyses = [
            e.payload["analysis"]
            for e in repository.events(claim_id)
            if e.event_type == "LLM_ANALYSIS_COMPLETED" and "analysis" in e.payload
        ]
        return SubmissionResponse(
            claim=claim,
            submission=fields,
            trip_days=(fields.trip_end - fields.trip_start).days + 1,
            operation=operation,
            human_review_submitted=bool(context and context.get("human_review")),
            llm_analysis=LLMAnalysis.model_validate(analyses[-1]) if analyses else None,
            erp_effects=repository.erp_effects(claim_id),
        )

    @app.post("/claims", status_code=202, response_model=SubmissionResponse)
    async def create(body: CreateClaimRequest):
        async with lock:
            claim = repository.create_submission(body)
            if claim.status == "NEW":
                await service.audit(
                    claim.claim_id,
                    AuditRequest(
                        idempotency_key=f"submission:{claim.claim_id}",
                    ),
                )
            return detail(claim.claim_id)

    @app.get("/claims/{claim_id}", response_model=SubmissionResponse)
    def get(claim_id: str):
        return detail(claim_id)

    app.state.detail = detail
    app.state.creation_lock = lock
    return app
