"""Default-off advisory mode reusing original source/rule/finance/ERP authority."""

from datetime import datetime, timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.compliance import HybridCatalog, fingerprint
from apps.api.enterprise_store import finance_execution
from domain.advisory_v2 import parse_advisory
from domain.compliance import ComplianceAnalysis, ComplianceRequest, SourceEvidence
from domain.enterprise import FinanceAction
from domain.hotel_matrix import HotelFields
from domain.positive_evidence import build_contract
from domain.schemas import RuleResult
from integrations.llm.advisory import OfflineAdvisoryProvider, validate_advisory
from integrations.retry import DependencyFailure
from rules.hard_gates import check_hard_gates
from rules.hotel_matrix import calculate as calculate_hotel


class AdvisoryCatalog(HybridCatalog):
    def __init__(self, legacy, store, repo, provider=None):
        super().__init__(legacy, store, repo, provider or OfflineAdvisoryProvider())

    def evaluate(self, state):
        try:
            result = super().evaluate(state)
            if result is not None:
                result["audit_context"]["p4b_advisory_mode"] = True
            return result
        except (SQLAlchemyError, OSError, ValueError):
            # A source/rule infrastructure fault is neither a PASS nor model compliance.
            if not self.is_hybrid(state.claim_id):
                return None
            revision, record = self.store.read("FACTS", state.claim_id)
            return dict(
                hard_gate_results=check_hard_gates(state),
                rule_results=[RuleResult(rule_id="ADVISORY_DEPENDENCY", outcome="UNKNOWN",
                                         severity="REVIEW", reason="规则或制度任务失败",
                                         actual=state.expense.amount)],
                audit_context=dict(
                    p4b_enabled=True, p4b_advisory_mode=True,
                    p4b_kind="HOTEL" if self.legacy.is_hotel_matrix(state.claim_id)
                    else self.store.read("SUBMISSION", state.claim_id)[1]["expense_type"],
                    p4b_fact_revision=revision, p4b_verification_record=record,
                    p4b_verified={}, p4b_trusted={}, p4b_evidence=[],
                    p4b_invoice_digest=fingerprint(self.repo.invoice_store.evidence(state.claim_id)),
                    p4b_source_error="RULE_OR_RETRIEVAL_FAILURE", authoritative_version_id="",
                ),
            )

    def declared_hotel_amount(self, state):
        """Compare declared nights/amount with governed limits, without verifying a stay."""
        ctx = state.audit_context
        if ctx["p4b_kind"] != "HOTEL" or ctx.get("p4b_source_error"):
            return {}
        snapshot = self.legacy.facts.read(state.claim_id)[0]
        fields = HotelFields.model_validate(self.repo.submission(state.claim_id))
        local = {**snapshot, "blockers": [b for b in snapshot["blockers"]
                                         if b != "STAY_UNVERIFIED"]}
        started = datetime.fromisoformat(ctx["started_at"])
        with Session(self.legacy.engine) as session:
            policy = self.legacy.policy(session, fields, snapshot["company"], started)
            last = self.legacy.policy(session, fields.model_copy(update={
                "incurred_on": fields.trip_end - timedelta(days=1)}),
                snapshot["company"], started)
            if (not policy or not last or policy.policy_version_id != last.policy_version_id
                    or policy.policy_version_id != ctx["authoritative_version_id"]
                    or policy.authoritative_rules.get("file_sha256")
                    != ctx["p4b_trusted"].get("policy_verification", {}).get("document_sha256")):
                local["blockers"].append("POLICY_UNKNOWN_OR_CONFLICT")
            result = calculate_hotel(fields, local, policy)
        return {**result, "basis": "EMPLOYEE_DECLARATION", "human_verification": "NOT_ASSERTED"}

    def snapshot(self, state):
        ctx = state.audit_context
        row = self.store.enterprise.claim_row(
            state.claim_id, {"company_id": state.employee.legal_entity, "role": "FINANCE"}
        )
        invoice = self.repo.invoice_store.evidence(state.claim_id)
        hints = self.repo.invoice_store.workflow_hints(state.claim_id) if invoice else None
        rules = [r.model_dump(mode="json") for r in state.rule_results]
        contract = build_contract(
            claim_id=state.claim_id, kind=ctx["p4b_kind"],
            candidate=self.repo.submission(state.claim_id), trusted=ctx["p4b_trusted"],
            verified=ctx["p4b_verified"], record=ctx.get("p4b_verification_record"), rules=rules,
            invoice=invoice, invoice_integrity=bool(hints and hints["integrity"]),
            candidate_digest=self.candidate_digest(state.claim_id), claim_version=row["version"],
            started_at=ctx["started_at"], contract_version="p4b.positive.2",
        )
        request = ComplianceRequest(
            claim_id=state.claim_id, policy_version_id=ctx["authoritative_version_id"],
            facts=dict(candidate=self.repo.submission(state.claim_id), verified=ctx["p4b_verified"],
                       positive={k: v.model_dump(mode="json") for k, v in contract.entries.items()},
                       trusted={**ctx["p4b_trusted"], "expense_type": ctx["p4b_kind"],
                                "positive_scope": contract.scope,
                                "positive_required": contract.required}),
            rule_results=rules,
            evidence=[SourceEvidence.model_validate(e) for e in ctx["p4b_evidence"]],
        )
        if getattr(self.provider, "protocol_version", None) == "p4b.advisory.4":
            # Keep selector validation aligned with evaluate(): V4 retrieves the
            # full city-classification table for model judgment, then cross-checks
            # that judgment against this server-owned fact.
            request.facts["trusted"]["city_classification_by_model"] = True
            request.facts["invoice_fields"] = (invoice or {}).get("confirmed_fields", {})
            request.facts["declared_amount_check"] = self.declared_hotel_amount(state)
        binding = dict(
            run_id=str(state.run_id), claim_version=row["version"],
            candidate_digest=self.candidate_digest(state.claim_id),
            fact_revision=ctx["p4b_fact_revision"],
            input_digest=fingerprint(dict(request=request.model_dump(mode="json"),
                                         gates=[g.model_dump(mode="json")
                                                for g in state.hard_gate_results],
                                         invoice_digest=ctx["p4b_invoice_digest"])),
        )
        return contract, request, binding

    async def analyze_hybrid(self, state, dependency):
        ctx = state.audit_context
        key = state.claim_id + ":" + str(state.run_id)
        old_report = self.store.read("REPORT", key)[1]
        if old_report and not self.store.read("ADVISORY", key)[1]:
            # Opt-in never rewrites an already pinned historical semantic report.
            return self.report_result(ComplianceAnalysis.model_validate(old_report), ctx, [])
        if ctx.get("p4b_advisory_mode") is not True:
            raise ValueError("Mode change requires an explicit new audit")
        contract, request, binding = self.snapshot(state)
        prior = self.store.read("ADVISORY", key)[1]
        if prior:
            if prior["binding"] != binding:
                raise ValueError("ADVISORY_INPUT_CHANGED")
            return self.result(state, prior, [])
        rules = state.rule_results
        if (any(g.outcome.value == "FAIL" for g in state.hard_gate_results)
                or any(r.outcome.value == "FAIL" for r in rules)):
            deterministic = "FAIL"
        elif (not state.hard_gate_results or not rules or contract.missing
              or ctx.get("p4b_source_error")
              or any(g.outcome.value != "PASS" for g in state.hard_gate_results)
              or any(r.outcome.value != "PASS" for r in rules)):
            deterministic = "UNKNOWN"
        else:
            deterministic = "PASS"
        if request.facts.get("declared_amount_check", {}).get("decision") == "FAIL":
            deterministic = "FAIL"
        raw, valid, errors, events, calls = None, [], [], [], []
        raw_responses = []
        status = "NOT_RUN"
        if ctx.get("p4b_source_error") or not request.evidence:
            errors = [dict(code=ctx.get("p4b_source_error") or "NO_VALID_POLICY",
                           field="retrieval")]
            status = "FAILED"
        elif isinstance(self.provider, OfflineAdvisoryProvider):
            errors = [dict(code="ADVISORY_NOT_RUN", field="provider")]
        else:
            async def call():
                nonlocal raw, valid, errors
                try:
                    output = await self.provider.analyze(request)
                    raw = (output.model_dump(mode="json", exclude={"provider", "model"})
                           if hasattr(output, "model_dump") else output)
                    output = parse_advisory(raw)
                    if output.schema_version != getattr(self.provider, "protocol_version", None):
                        raise ValueError("ADVISORY_PROTOCOL_MISMATCH")
                    valid, errors = validate_advisory(output, request)
                    return output
                finally:
                    response = request._provider_evidence.pop("_raw_response", None)
                    if response is not None:
                        raw_responses.append(response)
                    if request._provider_evidence:
                        calls.append(dict(request._provider_evidence))

            try:
                _, events, _ = await dependency(state, "llm", "ai_advisory", call)
            except DependencyFailure as exc:
                events = exc.events
                status = "FAILED"
                cause = exc.__cause__
                errors = [getattr(cause, "details", dict(code="ADVISORY_PROVIDER_OR_DTO_FAILURE",
                                                       field="provider"))]
            else:
                status = "PARTIAL" if errors and valid else "FAILED" if errors else "SUCCEEDED"
        report = dict(
            schema_version="p4b.advisory.report.1", claim_id=state.claim_id, binding=binding,
            deterministic_status=deterministic, ai_advisory_status=status,
            recommendation=raw["recommendation"] if status == "SUCCEEDED" else "UNDETERMINED",
            risk_level=raw["risk_level"] if status == "SUCCEEDED" else "UNDETERMINED",
            issues=valid, missing_evidence=raw["missing_evidence"] if status == "SUCCEEDED" else [],
            original_output=raw, raw_responses=raw_responses,
            validation_errors=errors, requires_human_review=True,
            policy_version_id=request.policy_version_id or None,
            source_status="FAILED" if ctx.get("p4b_source_error") else "SUCCEEDED",
            rule_results=request.rule_results, positive_evidence=contract.model_dump(mode="json"),
            supporting_evidence=[e.model_dump(mode="json") for e in request.evidence],
            input_facts=request.facts,
            provider_calls=calls, model=self.provider.model,
            summary="AI建议仅供参考，最终审批由授权财务人员负责。"
            if status == "SUCCEEDED" else (
                "AI审核建议存在依据不足或无法核验的内容，不能作为合规结论；需要独立人工核查。"),
        )
        if getattr(self.provider, "protocol_version", None) == "p4b.advisory.4":
            report.update(
                analysis_basis="CURRENT_DATA_WITH_RAG",
                advisory_protocol="p4b.advisory.4",
                declared_amount_check=request.facts.get("declared_amount_check", {}),
                manual_verification_required=False,
            )
            if status == "SUCCEEDED":
                report["summary"] = raw["summary"]
        if request._evidence_selector is not None:
            import json

            # Separate audit provenance; original_output/raw_responses are never rewritten.
            report["evidence_selector"] = json.loads(request._evidence_selector)
            report["evidence_selector_attempts"] = [
                json.loads(item) for item in request._evidence_selector_attempts]
        with self.store.lock, self.store.db:
            # Append-only; same run cannot replace either advice or legacy history.
            if self.store.read("ADVISORY", key)[1] is not None:
                raise ValueError("ADVISORY_ALREADY_PINNED")
            self.store.append("ADVISORY", key, report)
            self.store.append(
                "REPORT", key, self.legacy_report(state, report).model_dump(mode="json")
            )
            self.store.enterprise.event(
                {"id": "server:advisory", "name": "AI建议记录服务", "role": "SYSTEM",
                 "company_id": state.employee.legal_entity},
                "AI_ADVISORY_RECORDED", state.claim_id, run_id=str(state.run_id),
                status=status, deterministic_status=deterministic,
            )
        return self.result(state, report, events)

    def legacy_report(self, state, report):
        ctx = state.audit_context
        # A compatibility envelope, never a newly defined CHECK_PASSED or approval certificate.
        return ComplianceAnalysis(
            claim_id=state.claim_id, run_id=str(state.run_id),
            policy_version_id=report["policy_version_id"], fact_revision=ctx["p4b_fact_revision"],
            expense_type=ctx["p4b_kind"], policy_title=ctx.get("p4b_policy_title"),
            trusted_facts=ctx["p4b_trusted"], semantic_assessment=None,
            rule_results=report["rule_results"], semantic_findings=[],
            supporting_evidence=report["supporting_evidence"],
            missing_evidence=report["positive_evidence"]["missing"], conflicts=[], exceptions=[],
            suggested_risk="MEDIUM", recommended_action="HUMAN_REVIEW", compliance_status="UNKNOWN",
            explanation=report["summary"], provider="advisory", model=self.provider.model,
            analysis_status="UNSUPPORTED", positive_evidence=report["positive_evidence"],
        )

    def result(self, state, report, events):
        result = self.report_result(self.legacy_report(state, report), state.audit_context, events)
        result["audit_context"]["p4b_advisory"] = report
        return result

    def advisory(self, cid, run_id):
        report = self.store.read("ADVISORY", cid + ":" + str(run_id))[1] if run_id else None
        if not report:
            return None
        row = self.store.enterprise.db.execute(
            "SELECT stage FROM enterprise_claims WHERE claim_id=?", (cid,)
        ).fetchone()
        review = self.store.read("MANUAL_ADVISORY", cid + ":" + str(run_id))[1]
        finance = {"APPROVED": "APPROVED", "ERP_SYNC_PENDING": "APPROVED",
                   "REJECTED": "REJECTED", "RETURNED": "RETURNED"}.get(row[0], "PENDING")
        if finance == "PENDING" and review and review["outcome"] == "ESCALATE":
            finance = "ESCALATED"
        fresh = bool(review and review["binding"] == report["binding"]
                     and review["report_digest"] == fingerprint(report)
                     and report["binding"]["fact_revision"] == self.store.read("FACTS", cid)[0]
                     and report["binding"]["candidate_digest"] == self.candidate_digest(cid)
                     and review["generation"] == self.store.enterprise.authority(
                         review["actor_id"]))
        return {**report, "report_digest": fingerprint(report), "finance_decision_status": finance,
                "manual_review": review, "manual_review_current": fresh}

    def validate_report_authority(self, report, state):
        key = state.claim_id + ":" + str(state.run_id)
        advisory = self.store.read("ADVISORY", key)[1]
        if not advisory:
            # Historical runs retain the exact old semantic gate after opting in.
            return super().validate_report_authority(report, state)
        if report != state.audit_context.get("p4b_report"):
            raise ValueError("Advisory compatibility report changed")
        if advisory != state.audit_context.get("p4b_advisory"):
            raise ValueError("Pinned advisory report changed")
        if advisory.get("declared_amount_check", {}).get("decision") == "FAIL":
            raise ValueError("Declared hotel amount exceeds the governed policy limit")
        contract, _, binding = self.snapshot(state)
        if binding != advisory["binding"]:
            raise ValueError("Deterministic evidence/version changed")
        if (any(i["basis"] == "EXCEPTION" for i in advisory["issues"])
                or any(state.audit_context["p4b_verified"].get(k) is True
                       for k in ("cancellation_notice", "itinerary_verified",
                                 "exception_authorized"))):
            raise ValueError("Exception payment channel is not authorized")
        review = self.store.read("MANUAL_ADVISORY", key)[1]
        execution = finance_execution.get()
        if not execution or execution["claim_id"] != state.claim_id:
            raise ValueError("Explicit finance decision required")
        actor = self.store.enterprise.authorize_finance(
            execution["scope"], execution["key"], execution["token"]
        )
        intent = self.store.enterprise.db.execute(
            "SELECT payload FROM enterprise_requests WHERE scope=? AND key=?",
            (execution["scope"], execution["key"]),
        ).fetchone()
        decision = FinanceAction.model_validate_json(intent[0])
        if decision.advisory_report_digest is not None:
            # One explicit finance decision replaces the separate attestation form.
            # The persisted intent still revalidates live authority at each mutation.
            if (decision.action != "APPROVE"
                    or decision.advisory_report_digest != fingerprint(advisory)
                    or advisory["deterministic_status"] not in {"PASS", "UNKNOWN"}
                    or any(g.outcome.value != "PASS" for g in state.hard_gate_results)
                    or any(r.outcome.value == "FAIL" and r.severity.value == "BLOCK"
                           for r in state.rule_results)
                    or self.store.enterprise.claim_row(state.claim_id, actor)["owner_id"]
                    == actor["id"]):
                raise ValueError("Finance decision is bound to a different advisory report")
            # UNKNOWN and missing attestations may be resolved by this explicit,
            # report-bound human decision. Keep the rule/fact/AI snapshots intact;
            # validate_hybrid_authority still rechecks sources and original bytes.
            return
        if contract.missing or advisory["deterministic_status"] != "PASS":
            raise ValueError("Deterministic evidence/version unresolved")
        if not review:
            raise ValueError("Independent finance manual review required")
        if (review["binding"] != binding or review["report_digest"] != fingerprint(advisory)
                or review["outcome"] != "CLEAR" or review["exception_required"]
                or review["actor_id"] != actor["id"]
                or review["generation"] != self.store.enterprise.authority(actor["id"])):
            raise ValueError("Manual review is stale, unauthorized or unresolved")
