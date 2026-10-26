"""M2 genuine durable LangGraph interrupt/resume with frozen public DTOs."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from uuid import uuid4

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from sqlalchemy.exc import SQLAlchemyError

import domain.schemas as schemas
from agent.graph import build_m2_graph
from agent.nodes.human_review import (
    HumanResumePayload,
    human_event,
    validate_human_authority,
)
from agent.state import ClaimState
from apps.api.enterprise_store import finance_execution
from apps.api.errors import APIError
from domain.schemas import (
    AuditEvent,
    AuditRequest,
    ClaimStatus,
    Decision,
    DecisionResult,
    HumanReview,
    HumanReviewRequest,
    OperationResponse,
    ResumeRequest,
    RunMode,
)
from rag.retrieval.memory import PolicyCatalog


class AuditService:
    def __init__(self, repository, **adapters):
        self.repository = repository
        self.catalog = adapters.setdefault("catalog", PolicyCatalog())
        self.graph_factory = partial(build_m2_graph, repository, **adapters)
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def graph_session(self):
        # Reopen the official durable saver; the existing thread/checkpoint selects execution.
        allowed = [ClaimState] + [
            value
            for value in vars(schemas).values()
            if isinstance(value, type) and value.__module__ == "domain.schemas"
        ]
        serde = JsonPlusSerializer(allowed_msgpack_modules=allowed)
        async with aiosqlite.connect(self.repository.checkpoint_path) as connection:
            saver = AsyncSqliteSaver(connection, serde=serde)
            yield self.graph_factory(checkpointer=saver)

    def get_claim(self, claim_id):
        claim = self.repository.get(claim_id)
        if claim is None:
            raise APIError(404, "CLAIM_NOT_FOUND", "Claim not found")
        return claim

    def replay(self, claim_id, request, kind):
        previous = self.repository.operation(claim_id, request.idempotency_key, kind)
        if previous:
            if previous[0] != request.model_dump_json():
                raise APIError(409, "IDEMPOTENCY_CONFLICT", "Key used with another payload")
            return previous[1]
        return None

    def persist(self, claim, request, state, kind, run_context, extra_events=()):
        result = state.audit_context.get("decision_result")
        claim.decision_result = DecisionResult.model_validate(result) if result else None
        claim.status, claim.erp_status = state.status, state.erp_status
        claim.documents, claim.updated_at = state.documents, datetime.now(UTC)
        response = OperationResponse(
            claim_id=claim.claim_id,
            run_id=state.run_id,
            status=claim.status,
            revision=run_context["revision"],
            decision_result=claim.decision_result,
        )
        events = [AuditEvent.model_validate(e) for e in state.audit_context.get("events", [])]
        self.repository.save_operation(
            claim,
            request.idempotency_key,
            request.model_dump_json(),
            response,
            [*events, *extra_events],
            kind=kind,
            run_context=run_context,
        )
        return response

    async def pending(self, graph, claim, request):
        context = self.repository.run_context(claim.claim_id, request.run_id)
        if context is None:
            raise APIError(404, "RUN_NOT_FOUND", "Run not found for claim")
        if context.get("superseded"):
            raise APIError(409, "INVALID_TRANSITION", "Returned workflow cannot be resumed")
        recovery = context.get("recoverable") and isinstance(request, ResumeRequest)
        expected_status = ClaimStatus.PENDING_ERP if recovery else ClaimStatus.PENDING_HUMAN
        if claim.active_run_id != request.run_id or claim.status != expected_status:
            raise APIError(409, "INVALID_TRANSITION", "Claim is not waiting for human review")
        if context["revision"] != request.expected_revision:
            raise APIError(409, "REVISION_CONFLICT", "Revision does not match current run")
        config = {
            "configurable": {
                "thread_id": context["thread_id"],
                "checkpoint_id": context["checkpoint_id"],
            }
        }
        snapshot = await graph.aget_state(config)
        if not context["interrupted"] or not any(t.interrupts for t in snapshot.tasks):
            raise APIError(409, "INVALID_TRANSITION", "No real LangGraph interrupt checkpoint")
        state = ClaimState.model_validate(snapshot.values)
        if state.run_id != request.run_id or state.claim_id != claim.claim_id:
            raise APIError(409, "INVALID_TRANSITION", "Checkpoint belongs to another workflow")
        if recovery:
            if (
                snapshot.next != ("erp_recovery",)
                or not state.audit_context.get("recovery_required")
                or state.audit_context.get("failed_node") != "write_erp"
                or state.audit_context.get("recovery_status") != "RECOVERY_PENDING"
            ):
                raise APIError(409, "INVALID_TRANSITION", "No recoverable ERP checkpoint")
            return context, snapshot, state
        try:
            action = (
                request.decision
                if isinstance(request, HumanReviewRequest)
                else (context.get("human_review") or {}).get("decision", "APPROVE")
            )
            validate_human_authority(state, self.catalog, action)
        except ValueError as exc:
            raise APIError(409, "INVALID_TRANSITION", str(exc)) from exc
        return context, snapshot, state

    async def audit(self, claim_id: str, request: AuditRequest) -> OperationResponse:
        async with self.lock:
            claim = self.get_claim(claim_id)
            replay = self.replay(claim_id, request, "audit")
            if replay:
                return replay
            if request.mode != RunMode.LIVE:
                raise APIError(501, "FEATURE_NOT_IMPLEMENTED", "SHADOW is reserved")
            if claim.status != ClaimStatus.NEW:
                raise APIError(409, "INVALID_TRANSITION", "Audit requires a NEW claim")
            run_id = uuid4()
            claim.status, claim.active_run_id = ClaimStatus.AUDITING, run_id
            claim.updated_at = datetime.now(UTC)
            context = {
                "workflow_id": str(run_id),
                "thread_id": str(run_id),
                "revision": 0,
                "interrupted": False,
                "human_review": None,
            }
            accepted = OperationResponse(
                claim_id=claim_id,
                run_id=run_id,
                status=ClaimStatus.AUDITING,
                revision=0,
                decision_result=None,
            )
            self.repository.save_operation(
                claim,
                request.idempotency_key,
                request.model_dump_json(),
                accepted,
                run_context=context,
            )
            async with self.graph_session() as graph:
                config = {"configurable": {"thread_id": str(run_id)}}
                output = await graph.ainvoke(
                    ClaimState(
                        claim_id=claim_id,
                        run_id=run_id,
                        mode=request.mode,
                        audit_context={"idempotency_key": request.idempotency_key},
                    ).model_dump(),
                    config,
                )
                interrupts = output.pop("__interrupt__", ())
                state = ClaimState.model_validate(output)
                snapshot = await graph.aget_state(config)
                context.update(
                    checkpoint_id=snapshot.config["configurable"]["checkpoint_id"],
                    revision=state.revision,
                    interrupted=bool(interrupts),
                )
                extra = []
                if interrupts:
                    assert any(t.interrupts for t in snapshot.tasks)
                    assert state.status in (ClaimStatus.PENDING_HUMAN, ClaimStatus.PENDING_ERP)
                    context.update(
                        interrupt_reason=state.audit_context["interrupt_reason"],
                        interrupt_payloads=[i.value for i in interrupts],
                        interrupt_ids=[i.id for i in interrupts],
                        pre_interrupt_nodes=state.audit_context["pre_interrupt_nodes"],
                        post_interrupt_nodes=state.audit_context["post_interrupt_nodes"],
                    )
                    if state.status == ClaimStatus.PENDING_HUMAN:
                        extra.append(
                            human_event(
                                state,
                                "WORKFLOW_INTERRUPTED",
                                checkpoint_id=context["checkpoint_id"],
                                thread_id=context["thread_id"],
                            )
                        )
                context.update(
                    recoverable=state.audit_context.get("recoverable", False),
                    ai_decision_result=state.audit_context.get("ai_decision_result"),
                    llm_analysis=(state.llm_analysis.model_dump(mode="json")
                                  if state.llm_analysis else None),
                    compliance_analysis=state.audit_context.get("p4b_report"),
                    p4b_started_at=state.audit_context.get("started_at")
                    if state.audit_context.get("p4b_enabled") else None,
                    recovery_required=state.audit_context.get("recovery_required", False),
                    recovery_status=state.audit_context.get("recovery_status"),
                    failed_node=state.audit_context.get("failed_node"),
                )
                return self.persist(claim, request, state, "audit", context, extra)

    async def human_review(self, claim_id: str, request: HumanReviewRequest):
        async with self.lock:
            claim = self.get_claim(claim_id)
            replay = self.replay(claim_id, request, "human-review")
            if replay:
                return replay
            async with self.graph_session() as graph:
                context, _, state = await self.pending(graph, claim, request)
                if context["human_review"] is not None:
                    raise APIError(409, "INVALID_TRANSITION", "Review already submitted")
                review = HumanReview(
                    decision=request.decision,
                    reviewer_id=request.reviewer_id,
                    comment=request.comment,
                    reviewed_at=datetime.now(UTC),
                )
                context.update(
                    human_review=review.model_dump(mode="json"),
                    revision=context["revision"] + 1,
                )
                # Persist review separately; leave original interrupt checkpoint untouched.
                return self.persist(
                    claim,
                    request,
                    state,
                    "human-review",
                    context,
                    [human_event(state, "HUMAN_REVIEW_SUBMITTED", review)],
                )

    def bind_finance_evidence(self, state, context):
        """Recheck uncommitted enterprise effects without creating another approval."""
        if not state.audit_context.get("mandatory_finance_review"):
            return
        execution = finance_execution.get()
        if not execution or execution["claim_id"] != state.claim_id:
            raise APIError(403, "FINANCE_EXECUTION_REQUIRED", "需要有效财务操作")
        if (context.get("human_review") or {}).get("decision") != "APPROVE":
            return  # Rejection does not execute enterprise ERP writes.
        validation_state = state
        if context.get("recoverable"):
            if (state.decision != Decision.APPROVE or not state.human_review
                    or state.human_review.model_dump(mode="json") != context["human_review"]):
                raise APIError(409, "FINANCE_INTENT_INVALID", "恢复检查点与人工批准不一致")
            validation_state = state.model_copy(update={
                "decision": Decision.HUMAN_REVIEW, "requires_human": True,
                "errors": [e for e in state.errors if e.node != "write_erp"],
            })

        def validate():
            try:
                validate_human_authority(validation_state, self.catalog, "APPROVE")
            except (ValueError, OSError, SQLAlchemyError) as exc:
                raise APIError(409, "FINANCE_EVIDENCE_INVALID",
                               "执行证据已改变或无法核验，需要人工恢复") from exc

        execution["validate_evidence"] = validate
        if not self.repository.erp_effects(state.claim_id):
            validate()

    async def resume(self, claim_id: str, request: ResumeRequest):
        async with self.lock:
            claim = self.get_claim(claim_id)
            replay = self.replay(claim_id, request, "resume")
            if replay:
                return replay
            async with self.graph_session() as graph:
                context, snapshot, state = await self.pending(graph, claim, request)
                self.bind_finance_evidence(state, context)
                if context.get("recoverable"):
                    payload = {
                        "claim_id": claim_id, "run_id": str(request.run_id),
                        "checkpoint_id": context["checkpoint_id"], "revision": context["revision"],
                    }
                elif context["human_review"] is None:
                    raise APIError(409, "INVALID_TRANSITION", "Human review must precede resume")
                else:
                    payload = HumanResumePayload(
                        claim_id=claim_id,
                        run_id=request.run_id,
                        checkpoint_id=context["checkpoint_id"],
                        revision=context["revision"],
                        review=HumanReview.model_validate(context["human_review"]),
                    ).model_dump(mode="json")
                output = await graph.ainvoke(
                    Command(resume=payload),
                    snapshot.config,
                )
                interrupts = output.pop("__interrupt__", ())
                resumed = ClaimState.model_validate(output)
                final_snapshot = await graph.aget_state(
                    {
                        "configurable": {
                            "thread_id": context["thread_id"],
                        }
                    }
                )
                context.update(
                    interrupted=bool(interrupts),
                    revision=resumed.revision,
                    resume_context=resumed.audit_context.get("resume_context"),
                    checkpoint_id=final_snapshot.config["configurable"]["checkpoint_id"],
                    recoverable=resumed.audit_context.get("recoverable", False),
                    recovery_required=resumed.audit_context.get("recovery_required", False),
                    recovery_status=resumed.audit_context.get("recovery_status"),
                    failed_node=resumed.audit_context.get("failed_node"),
                )
                if resumed.status == ClaimStatus.COMPLETED:
                    context["completed_checkpoint_id"] = context["checkpoint_id"]
                if interrupts:
                    assert resumed.status == ClaimStatus.PENDING_ERP
                    assert any(t.interrupts for t in final_snapshot.tasks)
                    context.update(
                        interrupt_reason="RECOVERY_PENDING",
                        interrupt_payloads=[i.value for i in interrupts],
                        interrupt_ids=[i.id for i in interrupts],
                    )
                return self.persist(claim, request, resumed, "resume", context)
