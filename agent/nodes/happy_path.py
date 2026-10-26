"""M1 real LangGraph nodes. Each adapter and deterministic rule remains independent."""

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from time import perf_counter
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agent.nodes.human_review import (
    human_decision_result,
    human_event,
    validate_resume_payload,
)
from agent.routing.decision import assess_risk, route_decision
from agent.state import ClaimState
from domain.schemas import (
    AgentError,
    AuditEvent,
    ClaimStatus,
    Decision,
    DecisionResult,
    ERPStatus,
    LLMAnalysis,
    Outcome,
)
from integrations.documents.mock import MockDocumentParser
from integrations.documents.submission import parse_submission
from integrations.erp.base import ERPAdapter
from integrations.erp.mock import MockERP
from integrations.llm.base import AnalysisRequest, LLMProvider
from integrations.llm.mock import MockLLMProvider
from integrations.retry import DependencyFailure, RetryPolicy, call_with_retry, dependency_event
from rag.retrieval.base import PolicyRetriever
from rag.retrieval.memory import InMemoryPolicyRetriever, PolicyCatalog
from rules.hard_gates import check_hard_gates
from rules.travel_rules import hotel_city_limit


def _build_audit_graph(
    repository,
    *,
    erp: ERPAdapter | None = None,
    retriever: PolicyRetriever | None = None,
    provider: LLMProvider | None = None,
    catalog: PolicyCatalog | None = None,
    parser: MockDocumentParser | None = None,
    checkpointer=None,
    human_in_loop: bool = False,
    retry_policies: dict[str, RetryPolicy] | None = None,
    sleeper=asyncio.sleep,
    shadow_boundary: bool = False,
    business_date_provider=None,
    mandatory_finance_review: bool = False,
):
    if shadow_boundary and (human_in_loop or checkpointer is not None):
        raise ValueError("Shadow execution cannot attach business checkpoints or human actions")
    erp = erp or MockERP(repository)
    catalog = catalog or PolicyCatalog()
    retriever = retriever or InMemoryPolicyRetriever(catalog)
    provider = provider or MockLLMProvider()
    parser = parser or MockDocumentParser()
    policies = {name: RetryPolicy() for name in ("erp", "llm", "retriever")}
    policies.update(retry_policies or {})

    async def dependency(state, name, operation, call):
        def journal(items):
            repository.append_events([
                *[AuditEvent.model_validate(e) for e in state.audit_context.get("events", [])],
                *items,
            ])

        return await call_with_retry(
            state, name, operation, call, policies[name], sleeper=sleeper, journal=journal,
        )

    def event(state, node, kind, status, summary, duration_ms):
        return AuditEvent(
            event_id=uuid4(),
            claim_id=state.claim_id,
            run_id=state.run_id,
            event_type=kind,
            actor=f"agent:{node}",
            occurred_at=datetime.now(UTC),
            payload={
                "node": node,
                "status": status,
                "summary": summary,
                "duration_ms": round(duration_ms, 3),
                "input_reference": state.claim_id,
                "output_reference": str(state.run_id),
                "error": None,
            },
        ).model_dump(mode="json")

    def instrument(name, kinds, action):
        async def wrapped(state: ClaimState):
            start = perf_counter()
            try:
                update = await action(state)
                context = {**state.audit_context, **update.pop("audit_context", {})}
                current = state.model_copy(update={**update, "audit_context": context})
                status = "PASS"
                if name == "rule_check":
                    checks = [*current.hard_gate_results, *current.rule_results]
                    status = "PASS" if all(c.outcome == Outcome.PASS for c in checks) else "FAIL"
                if name == "parse_documents" and not context.get("document_facts_match"):
                    status = "FAIL"
                if name == "retrieve_policy" and not current.policy_evidence:
                    status = "UNKNOWN"
                if name == "audit_log":
                    kinds_actual = [
                        "WORKFLOW_COMPLETED"
                        if current.status == ClaimStatus.COMPLETED
                        else "WORKFLOW_FAILED"
                        if current.status == ClaimStatus.FAILED
                        else "WORKFLOW_DEFERRED"
                    ]
                else:
                    kinds_actual = kinds
                events = [*context.get("events", [])]
                for kind in kinds_actual:
                    event_status = status
                    if kind == "HARD_GATE_CHECKED":
                        event_status = (
                            "PASS"
                            if all(g.outcome == Outcome.PASS for g in current.hard_gate_results)
                            else "FAIL"
                        )
                    elif kind == "RULES_CHECKED":
                        event_status = (
                            "PASS"
                            if all(r.outcome == Outcome.PASS for r in current.rule_results)
                            else "FAIL"
                        )
                    elif kind == "WORKFLOW_FAILED":
                        event_status = "FAILED"
                    elif kind == "WORKFLOW_DEFERRED":
                        event_status = "PENDING"
                    elif kind == "HUMAN_REVIEW_REQUIRED":
                        event_status = "PENDING"
                    details = {}
                    summary = kind
                    if kind == "HARD_GATE_CHECKED":
                        details["hard_gate_results"] = [
                            g.model_dump(mode="json") for g in current.hard_gate_results
                        ]
                        summary = (
                            f"{sum(g.outcome == Outcome.PASS for g in current.hard_gate_results)}"
                        )
                        summary += f"/{len(current.hard_gate_results)} hard gates passed"
                    elif kind == "RULES_CHECKED":
                        details["rule_results"] = [
                            r.model_dump(mode="json") for r in current.rule_results
                        ]
                        if current.audit_context.get("hotel_rule_result"):
                            details["hotel_rule_result"] = current.audit_context[
                                "hotel_rule_result"
                            ]
                        summary = "; ".join(r.reason for r in current.rule_results)
                    elif kind == "POLICY_RETRIEVED":
                        if current.audit_context.get("hotel_rule_result"):
                            details["retrieval_outcome"] = context.get("retrieval_outcome")
                        details["policy_version_ids"] = [
                            p.policy_version_id for p in current.policy_evidence
                        ]
                        summary = "Authority-filtered retrieval: " + ", ".join(
                            details["policy_version_ids"]
                        )
                    elif kind == "LLM_ANALYSIS_COMPLETED" and current.llm_analysis:
                        details["analysis"] = current.llm_analysis.model_dump(mode="json")
                        summary = current.llm_analysis.summary
                    elif kind == "RISK_ASSESSED":
                        summary = str(current.risk_level)
                    elif kind == "DECISION_MADE":
                        summary = str(current.decision)
                        if mandatory_finance_review:
                            details["ai_decision_result"] = context["ai_decision_result"]
                            details["finance_required"] = True
                    elif kind == "HUMAN_REVIEW_REQUIRED":
                        summary = context["interrupt_reason"]
                        details.update(
                            workflow_id=context["workflow_id"],
                            thread_id=context["thread_id"],
                        )
                    elif kind == "ERP_UPDATED":
                        if current.erp_status != ERPStatus.SUCCEEDED:
                            kind, event_status = "ERP_WRITE_SKIPPED", "FAILED"
                        details["erp_status"] = current.erp_status.value
                        summary = str(context.get("erp_write", {}).get("status", "NOT_WRITTEN"))
                        details.update(
                            dependency="erp", operation="write_erp",
                            attempt=context.get("erp_attempts", 1),
                            idempotency_key=context.get("erp_idempotency_key"),
                            actual_side_effect=not context.get("erp_write", {}).get(
                                "idempotent_replay", False
                            ),
                        )
                    elif name == "audit_log":
                        summary = current.status.value
                    elif kind == "RECOVERY_REQUIRED":
                        details.update(
                            dependency="erp", operation="write_erp",
                            attempt=context.get("erp_attempts", 0),
                            error_type=current.errors[-1].message if current.errors else None,
                            retryable=True, timestamp=datetime.now(UTC).isoformat(),
                            recoverable=True, recovery_required=True, erp_unresolved=True,
                            idempotency_key=context.get("erp_idempotency_key"),
                        )
                    item = event(
                        current, name, kind, event_status, summary, (perf_counter() - start) * 1000
                    )
                    item["payload"].update(details)
                    events.append(item)
                return {**update, "audit_context": {**context, "events": events}}
            except Exception as exc:
                error = AgentError(
                    node=name,
                    code="NODE_FAILED",
                    message=exc.error_type if isinstance(exc, DependencyFailure)
                    else type(exc).__name__,
                    retryable=isinstance(exc, DependencyFailure) and exc.retryable,
                    occurred_at=datetime.now(UTC),
                )
                failed = event(
                    state,
                    name,
                    f"{name.upper()}_FAILED",
                    "FAILED",
                    "Node failed; automatic approval disabled",
                    (perf_counter() - start) * 1000,
                )
                failed["payload"]["error"] = error.model_dump(mode="json")
                update = {
                    "errors": [*state.errors, error],
                    "audit_context": {
                        **state.audit_context,
                        "events": [*state.audit_context.get("events", []),
                                   *(exc.events if isinstance(exc, DependencyFailure) else []),
                                   failed],
                    },
                }
                if name == "write_erp":
                    update.update(erp_status=ERPStatus.FAILED, status=ClaimStatus.FAILED)
                    update["audit_context"].update(
                        recoverable=False, recovery_required=False, recovery_status=None,
                        erp_unresolved=True, failed_node="write_erp",
                    )
                    if human_in_loop and isinstance(exc, DependencyFailure) and exc.retryable:
                        update["status"] = ClaimStatus.PENDING_ERP
                        update["audit_context"].update(
                            recovery_status="RECOVERY_PENDING", recoverable=True,
                            recovery_required=True, failed_node="write_erp",
                            erp_unresolved=True,
                            erp_attempts=state.audit_context.get("erp_attempts", 0) + exc.attempts,
                            erp_idempotency_key=f"{state.claim_id}:{state.run_id}:write_decision",
                        )
                elif name == "retrieve_policy":
                    update["audit_context"]["retrieval_outcome"] = "RETRIEVAL_SYSTEM_FAILURE"
                elif name == "llm_analysis":
                    update["audit_context"]["llm_outcome"] = "ANALYSIS_SYSTEM_FAILURE"
                return update

        return wrapped

    def filters(state):
        return dict(
            legal_entity=state.employee.legal_entity,
            category=state.expense.category,
            city=state.expense.city,
            incurred_on=state.expense.incurred_on,
        )

    async def load_claim(state):
        claim = repository.get(state.claim_id)
        if claim is None:
            raise ValueError("Claim not found")
        started = datetime.now(UTC)
        return dict(
            employee=claim.employee,
            expense=claim.expense,
            documents=claim.documents,
            status=claim.status if shadow_boundary else ClaimStatus.AUDITING,
            audit_context={
                "milestone": "M5B-1" if shadow_boundary else "M1",
                "data_source": "shadow-snapshot" if shadow_boundary else "mock",
                "claim_loaded": True,
                "started_at": started.isoformat(),
                "business_date": (business_date_provider() if business_date_provider else
                                  started.astimezone(timezone(timedelta(hours=8))).date()).isoformat(),
            },
        )

    async def fetch_erp_context(state):
        context, events, _ = await dependency(
            state, "erp", "fetch_erp_context", lambda: erp.fetch_claim_context(state.claim_id),
        )
        return {"erp_context": context, "audit_context": {
            "events": [*state.audit_context.get("events", []), *events],
        }}

    async def parse_documents(state):
        # Dynamic declarations are read from durable storage, never from fixture Case IDs.
        submission = getattr(repository, "submission", lambda _: None)(state.claim_id)
        if submission is not None:
            hybrid = (catalog.parse_hybrid(state, submission)
                      if hasattr(catalog, "parse_hybrid") else None)
            if hybrid is not None:
                parsed = hybrid
            elif hasattr(catalog, "is_hotel_matrix") and catalog.is_hotel_matrix(state.claim_id):
                parsed = catalog.parse_hotel_declaration(state, submission)
            else:
                parsed = parse_submission(
                    state, submission, expected_legal_entity=state.employee.legal_entity
                    if mandatory_finance_review else "DEMO-CN",
                )
            store = getattr(repository, "invoice_store", None)
            hints = store.workflow_hints(state.claim_id) if store else None
            if hints:
                context = parsed["audit_context"]
                context["invoice_warning_codes"] = hints["warnings"]
                # Non-authoritative evidence may increase scrutiny, never lower risk.
                if hints["warnings"]:
                    context["business_analysis"]["ambiguity"] = True
                    context["business_analysis"]["risk_factors"].extend(hints["warnings"])
                if not hints["integrity"]:
                    context["document_facts_match"] = False
            return parsed
        documents, matches = [], []
        invoice_count = 0
        trip_context = None
        cancellations = []
        for doc in state.documents:
            invoice = parser.parse(doc.document_id)
            valid = invoice is not None and invoice.claim_id == state.claim_id
            if valid and invoice.kind == "INVOICE":
                invoice_count += 1
                valid = invoice.expense == state.expense
                if invoice.trip_context:
                    trip_context = invoice.trip_context
            elif valid:
                valid = bool(invoice.cancelled_on and invoice.flight_reference)
                if valid:
                    cancellations.append(invoice)
            matches.append(valid)
            documents.append(
                doc.model_copy(update={"parse_status": "PARSED" if valid else "FAILED"})
            )
        outside = bool(trip_context and trip_context.stay_to > trip_context.approved_to)
        cancellation = bool(
            trip_context
            and any(
                c.document_id == trip_context.cancellation_document_id
                and c.cancelled_on == trip_context.approved_to
                for c in cancellations
            )
        )
        risk_factors = ["STAY_OUTSIDE_APPROVED_TRIP_DATES"] if outside else []
        if outside and cancellation:
            risk_factors.append("FLIGHT_CANCELLATION_EXCEPTION")
        return {
            "documents": documents,
            "audit_context": {
                "document_facts_match": invoice_count > 0 and bool(matches) and all(matches),
                "document_parser": "mock-json; no OCR",
                "trip_context": trip_context.model_dump(mode="json") if trip_context else None,
                "business_analysis": {
                    "ambiguity": outside,
                    "risk_factors": risk_factors,
                    "flight_cancellation": cancellation,
                    "automatic_exception_rule": False,
                },
            },
        }

    async def rule_check(state):
        if hasattr(catalog, "evaluate") and hasattr(catalog, "analyze_hybrid"):
            hybrid = catalog.evaluate(state)
            if hybrid is not None:
                return hybrid
        if hasattr(catalog, "check_hotel"):
            checked = catalog.check_hotel(state)
            if checked is not None:
                return {
                    "hard_gate_results": check_hard_gates(state),
                    "rule_results": [checked],
                    "audit_context": {
                        "authoritative_version_id": checked.policy_version_id or "",
                        "hotel_rule_result": catalog.facts.read(state.claim_id)[1],
                    },
                }
        travel = catalog.evaluate_travel(state) if hasattr(catalog, "evaluate_travel") else None
        if travel is not None:
            return {
                "hard_gate_results": check_hard_gates(state),
                "rule_results": travel["rules"],
                "audit_context": {
                    "travel_policy_ids": travel["ids"],
                    "travel_evidence": [e.model_dump(mode="json") for e in travel["evidence"]],
                    "travel_facts": travel["facts"],
                    "authoritative_version_id": travel["ids"][0] if travel["ids"] else "",
                },
            }
        policy = None
        if state.employee and state.expense:
            if hasattr(catalog, "resolve_for_audit"):
                policy = catalog.resolve_for_audit(
                    **filters(state),
                    audit_started_at=datetime.fromisoformat(state.audit_context["started_at"]),
                )
            else:
                policy = catalog.resolve(**filters(state))
        return {
            "hard_gate_results": check_hard_gates(state),
            "rule_results": [hotel_city_limit(state.expense, policy)],
            "audit_context": {"authoritative_version_id": policy.document_id if policy else ""},
        }

    async def retrieve_policy(state):
        if state.audit_context.get("p4b_enabled"):
            evidence = catalog.legacy_evidence(state.audit_context)
            return {"policy_evidence": evidence, "audit_context": {
                "retrieval_outcome": state.audit_context.get("p4b_source_error") or
                ("POLICY_FOUND" if evidence else "NO_POLICY_FOUND"),
            }}
        if "travel_policy_ids" in state.audit_context:
            from domain.schemas import PolicyEvidence

            evidence = [
                PolicyEvidence.model_validate(e) for e in state.audit_context["travel_evidence"]
            ]
            return {"policy_evidence": evidence, "audit_context": {
                "retrieval_outcome": "POLICY_FOUND" if evidence else "NO_POLICY_FOUND",
            }}
        if state.employee is None or state.expense is None:
            return {"policy_evidence": []}
        evidence, events, _ = await dependency(
            state, "retriever", "retrieve_policy", lambda: retriever.retrieve(
                f"{state.expense.city}住宿费用限额", **filters(state),
                audit_started_at=datetime.fromisoformat(state.audit_context["started_at"]),
                authoritative_version_id=state.audit_context.get("authoritative_version_id", ""),
            ),
        )
        version_conflict = bool(
            hasattr(catalog, "is_hotel_matrix") and catalog.is_hotel_matrix(state.claim_id)
            and evidence and any(
                e.policy_version_id != state.audit_context.get("authoritative_version_id")
                for e in evidence
            )
        )
        if hasattr(catalog, "hotel_evidence"):
            evidence = catalog.hotel_evidence(state, evidence)
        return {"policy_evidence": evidence, "audit_context": {
            "events": [*state.audit_context.get("events", []), *events],
            "retrieval_outcome": "RULE_RAG_VERSION_CONFLICT" if version_conflict else
            "POLICY_FOUND" if evidence else "NO_POLICY_FOUND",
        }}

    async def llm_analysis(state):
        if state.audit_context.get("p4b_enabled"):
            return await catalog.analyze_hybrid(state, dependency)
        facts = {
            "business_ambiguity": str(
                state.audit_context.get("business_analysis", {}).get("ambiguity", False)
            ).lower(),
            "flight_cancellation": str(
                state.audit_context.get("business_analysis", {}).get("flight_cancellation", False)
            ).lower(),
            "city": state.expense.city if state.expense else "",
            "amount": str(state.expense.amount) if state.expense else "",
            "limit": str(state.rule_results[0].limit) if state.rule_results else "",
            "hard_gates_passed": str(
                bool(state.hard_gate_results)
                and all(g.outcome == Outcome.PASS for g in state.hard_gate_results)
            ).lower(),
            "rules_passed": str(
                bool(state.rule_results)
                and all(r.outcome == Outcome.PASS for r in state.rule_results)
            ).lower(),
        }
        # Parsed dates are business facts; explanation remains low-trust employee text.
        trip = state.audit_context.get("trip_context") or {}
        facts.update({key: str(trip[key]) for key in (
            "approved_from", "approved_to", "stay_from", "stay_to", "employee_explanation",
        ) if key in trip})
        submitted = state.audit_context.get("submitted_facts")
        if submitted:
            facts.update(
                declaration="true", purpose=submitted["purpose"],
                seller=submitted["seller"], position=submitted["position"],
                employee_explanation=submitted["notes"],
            )
        facts.update({key: str(value) for key, value in
                      state.audit_context.get("travel_facts", {}).items()})
        provider_calls = []

        async def analyze_validated():
            request = AnalysisRequest(
                claim_id=state.claim_id,
                facts=facts,
                evidence=state.policy_evidence,
            )
            try:
                raw = await provider.analyze(request)
            finally:
                # Request-scoped metadata avoids cross-claim races and never contains text/key.
                if request._provider_evidence:
                    item = dependency_event(
                        state, "LLM_PROVIDER_CALL", "llm", "llm_analysis",
                        len(provider_calls) + 1, **request._provider_evidence,
                    )
                    provider_calls.append(item)
                    repository.append_events([
                        *[AuditEvent.model_validate(e) for e in
                          [*state.audit_context.get("events", []), *provider_calls]],
                    ])
            # Validate at the trust boundary, including JSON returned by alternate adapters.
            if isinstance(raw, LLMAnalysis):
                raw = raw.model_dump()
            return (LLMAnalysis.model_validate_json(raw) if isinstance(raw, str)
                    else LLMAnalysis.model_validate(raw))

        try:
            analysis, events, _ = await dependency(
                state, "llm", "llm_analysis", analyze_validated,
            )
        except DependencyFailure as error:
            error.events = [*provider_calls, *error.events]
            raise
        return {"llm_analysis": analysis, "audit_context": {
            "events": [*state.audit_context.get("events", []), *provider_calls, *events],
        }}

    async def risk_assessment(state):
        return {"risk_level": assess_risk(state)}

    async def decision_router(state):
        decision = route_decision(state.risk_level)
        result = DecisionResult(
            claim_id=state.claim_id,
            run_id=state.run_id,
            decision=decision,
            risk_level=state.risk_level,
            hard_gate_passed=bool(state.hard_gate_results)
            and all(g.outcome == Outcome.PASS for g in state.hard_gate_results),
            rule_results=state.rule_results,
            policy_evidence=state.policy_evidence,
            reason="Deterministic checks and authoritative policy passed"
            if decision == Decision.APPROVE
            else "Deterministic checks failed or evidence requires review",
            confidence=1.0 if decision == Decision.APPROVE else 0.0,
            requires_human=decision == Decision.HUMAN_REVIEW,
            decided_at=None if decision == Decision.HUMAN_REVIEW else datetime.now(UTC),
        )
        context = {"decision_result": result.model_dump(mode="json")}
        if mandatory_finance_review:
            # The AI conclusion remains an immutable, valid v1 DTO. The workflow
            # waits for finance even when AI recommends approval or a hard block.
            context.update(ai_decision_result=result.model_dump(mode="json"),
                           mandatory_finance_review=True)
        return {
            "decision": Decision.HUMAN_REVIEW if mandatory_finance_review else decision,
            "requires_human": mandatory_finance_review or result.requires_human,
            "audit_context": context,
        }

    async def auto_approve(state):
        result = DecisionResult.model_validate(state.audit_context["decision_result"])
        if result.decision != Decision.APPROVE or assess_risk(state).value != "LOW":
            raise ValueError("Automatic approval requires LOW and validated constraints")
        return {"erp_status": ERPStatus.PENDING, "status": ClaimStatus.PENDING_ERP}

    async def write_erp(state):
        if state.errors:
            return {"erp_status": ERPStatus.FAILED, "status": ClaimStatus.FAILED}
        result = DecisionResult.model_validate(state.audit_context["decision_result"])
        expected = "APPROVED" if result.decision == Decision.APPROVE else "REJECTED"
        key = f"{state.claim_id}:{state.run_id}:write_decision"

        async def write_validated():
            response = await erp.write_decision(result, idempotency_key=key)
            if not isinstance(response, dict) or response.get("status") != expected:
                raise ValueError("ERP decision was not acknowledged")
            return response

        response, events, attempts = await dependency(state, "erp", "write_erp", write_validated)
        return {"erp_status": ERPStatus.SUCCEEDED, "audit_context": {
            "erp_write": response, "erp_idempotency_key": key,
            "erp_attempts": state.audit_context.get("erp_attempts", 0) + attempts,
            "events": [*state.audit_context.get("events", []), *events],
            "recoverable": False, "recovery_required": False, "erp_unresolved": False,
            "recovery_status": None,
            "failed_node": None,
        }}

    async def prepare_erp_recovery(state):
        return {"revision": state.revision + 1, "audit_context": {
            "interrupt_reason": "RECOVERY_PENDING", "interrupted": True,
            "workflow_id": str(state.run_id), "thread_id": str(state.run_id),
            "pre_interrupt_nodes": [n[0] for n in nodes[:10]],
            "post_interrupt_nodes": ["erp_recovery", "write_erp", "audit_log"],
        }}

    async def erp_recovery(state):
        # The failed write is already checkpointed. No adapter call before interrupt.
        raw = interrupt({
            "claim_id": state.claim_id, "run_id": str(state.run_id),
            "reason": "RECOVERY_PENDING", "failed_node": "write_erp",
        })
        context = repository.run_context(state.claim_id, state.run_id)
        if not context or raw != {
            "claim_id": state.claim_id, "run_id": str(state.run_id),
            "revision": context["revision"], "checkpoint_id": context["checkpoint_id"],
        } or not context.get("recoverable"):
            raise ValueError("Recovery resume must match the persisted checkpoint")
        item = dependency_event(state, "WORKFLOW_RECOVERY_RESUMED", "erp", "write_erp", 0,
                                checkpoint_id=context["checkpoint_id"])
        return {
            "errors": [e for e in state.errors if e.node != "write_erp"],
            "revision": context["revision"] + 1, "erp_status": ERPStatus.PENDING,
            "audit_context": {**state.audit_context, "interrupted": False,
                "events": [*state.audit_context.get("events", []), item],
                "resume_context": {"from_checkpoint_id": context["checkpoint_id"],
                                   "thread_id": str(state.run_id), "run_id": str(state.run_id)},
            },
        }

    async def audit_log(state):
        if state.decision is None or state.errors or state.erp_status == ERPStatus.FAILED:
            status = ClaimStatus.FAILED
        elif state.requires_human:
            status = ClaimStatus.PENDING_HUMAN
        elif state.decision == Decision.APPROVE and state.erp_status != ERPStatus.SUCCEEDED:
            status = ClaimStatus.FAILED
        else:
            status = ClaimStatus.COMPLETED
        return {"status": status, "revision": state.revision + 1}

    async def prepare_human_review(state):
        return {
            "status": ClaimStatus.PENDING_HUMAN,
            "revision": 1,
            "audit_context": {
                "workflow_id": str(state.run_id),
                "thread_id": str(state.run_id),
                "interrupted": True,
                "interrupt_reason": state.llm_analysis.summary if state.llm_analysis else "MEDIUM",
                "pre_interrupt_nodes": [n[0] for n in nodes[:8]] + ["prepare_human_review"],
                "post_interrupt_nodes": ["human_review", "write_erp", "audit_log"],
            },
        }

    async def human_review(state):
        # This node restarts on resume. Before interrupt: pure payload construction only.
        raw = interrupt(
            {
                "claim_id": state.claim_id,
                "workflow_id": str(state.run_id),
                "thread_id": str(state.run_id),
                "decision": str(state.decision),
                "risk_level": str(state.risk_level),
                "reason": state.audit_context.get("interrupt_reason"),
                "actions": ["APPROVE", "REJECT"],
            }
        )
        persisted = repository.run_context(state.claim_id, state.run_id)
        payload = validate_resume_payload(state, raw, persisted)
        result = human_decision_result(state, payload.review, catalog)
        events = repository.events(state.claim_id)
        events.extend(
            [
                human_event(
                    state,
                    "WORKFLOW_RESUMED",
                    payload.review,
                    checkpoint_id=payload.checkpoint_id,
                    thread_id=str(state.run_id),
                ),
                human_event(
                    state,
                    "HUMAN_APPROVED" if payload.review.decision == "APPROVE" else "HUMAN_REJECTED",
                    payload.review,
                ),
            ]
        )
        return {
            "human_review": payload.review,
            "errors": [e for e in state.errors if not (
                payload.review.decision == "REJECT"
                and e.node in ("llm_analysis", "retrieve_policy")
            )],
            "decision": result.decision,
            "risk_level": result.risk_level,
            "requires_human": False,
            "status": ClaimStatus.PENDING_ERP,
            "erp_status": ERPStatus.PENDING,
            "revision": payload.revision,
            "audit_context": {
                **state.audit_context,
                "interrupted": False,
                "ambiguity_resolution": payload.review.model_dump(mode="json"),
                "resume_context": {
                    "from_checkpoint_id": payload.checkpoint_id,
                    "thread_id": str(state.run_id),
                    "run_id": str(state.run_id),
                },
                "events": [e.model_dump(mode="json") for e in events],
                "decision_result": result.model_dump(mode="json"),
            },
        }

    graph = StateGraph(ClaimState)
    nodes = [
        ("load_claim", ["CLAIM_LOADED"], load_claim),
        ("fetch_erp_context", ["ERP_CONTEXT_FETCHED"], fetch_erp_context),
        ("parse_documents", ["DOCUMENTS_PARSED"], parse_documents),
        ("rule_check", ["HARD_GATE_CHECKED", "RULES_CHECKED"], rule_check),
        ("retrieve_policy", ["POLICY_RETRIEVED"], retrieve_policy),
        ("llm_analysis", ["LLM_ANALYSIS_COMPLETED"], llm_analysis),
        ("risk_assessment", ["RISK_ASSESSED"], risk_assessment),
        ("decision_router", ["DECISION_MADE"], decision_router),
        ("auto_approve", ["AUTO_APPROVED"], auto_approve),
        ("write_erp", ["ERP_UPDATED"], write_erp),
        ("audit_log", ["WORKFLOW_COMPLETED"], audit_log),
    ]
    if shadow_boundary:
        # Compile only analysis nodes. Business side-effect nodes are absent, not mocked.
        nodes = nodes[:8]
    for name, kinds, action in nodes:
        graph.add_node(name, instrument(name, kinds, action))
    graph.add_edge(START, "load_claim")
    for a, b in zip(nodes[:7], nodes[1:8], strict=True):
        graph.add_edge(a[0], b[0])
    if shadow_boundary:
        graph.add_edge("decision_router", END)
        return graph.compile()
    if human_in_loop:
        graph.add_node("prepare_erp_recovery", instrument(
            "prepare_erp_recovery", ["RECOVERY_REQUIRED"], prepare_erp_recovery,
        ))
        graph.add_node("erp_recovery", erp_recovery)
        graph.add_edge("prepare_erp_recovery", "erp_recovery")
        graph.add_edge("erp_recovery", "write_erp")
        graph.add_node(
            "prepare_human_review",
            instrument("prepare_human_review", ["HUMAN_REVIEW_REQUIRED"], prepare_human_review),
        )
        graph.add_node("human_review", human_review)
        graph.add_edge("prepare_human_review", "human_review")
        if mandatory_finance_review:
            graph.add_conditional_edges(
                "human_review", lambda state: "write" if state.decision == Decision.APPROVE
                else "finish", {"write": "write_erp", "finish": "audit_log"},
            )
        else:
            graph.add_edge("human_review", "write_erp")
    graph.add_conditional_edges(
        "decision_router",
        lambda state: (
            "approve"
            if state.decision == Decision.APPROVE
            else "human"
            if human_in_loop and state.requires_human
            else "stop"
        ),
        {
            "approve": "auto_approve",
            "stop": "audit_log",
            **({"human": "prepare_human_review"} if human_in_loop else {}),
        },
    )
    graph.add_edge("auto_approve", "write_erp")
    if human_in_loop:
        graph.add_conditional_edges(
            "write_erp", lambda state: "recover" if state.audit_context.get(
                "recovery_required"
            ) else "finish", {"recover": "prepare_erp_recovery", "finish": "audit_log"},
        )
    else:
        graph.add_edge("write_erp", "audit_log")
    graph.add_edge("audit_log", END)
    return graph.compile(checkpointer=checkpointer)


def build_happy_path(
    repository,
    *,
    erp: ERPAdapter | None = None,
    retriever: PolicyRetriever | None = None,
    provider: LLMProvider | None = None,
    catalog: PolicyCatalog | None = None,
    parser: MockDocumentParser | None = None,
    checkpointer=None,
    human_in_loop: bool = False,
    retry_policies: dict[str, RetryPolicy] | None = None,
    sleeper=asyncio.sleep,
    business_date_provider=None,
    mandatory_finance_review: bool = False,
):
    """Preserve the business factory interface; use the snapshot factory for shadow."""
    return _build_audit_graph(
        repository, erp=erp, retriever=retriever, provider=provider, catalog=catalog,
        parser=parser, checkpointer=checkpointer, human_in_loop=human_in_loop,
        retry_policies=retry_policies, sleeper=sleeper,
        business_date_provider=business_date_provider,
        mandatory_finance_review=mandatory_finance_review,
    )
