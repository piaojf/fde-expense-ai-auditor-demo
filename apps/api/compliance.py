"""Opt-in single-source P4B orchestration and immutable independent audit records."""

import json
from datetime import UTC, date, datetime
from hashlib import sha256

from sqlalchemy.orm import Session

from agent.routing.compliance import aggregate
from apps.api.errors import APIError
from database.models import PolicyVersion
from domain.compliance import (
    ComplianceAnalysis,
    ComplianceRequest,
    SourceEvidence,
    VerifiedFacts,
)
from domain.compliance_v2 import CompactSemanticAnalysis
from domain.positive_evidence import build_contract, required_facts
from domain.schemas import LLMAnalysis, PolicyEvidence, RuleResult
from integrations.llm.compliance import (
    OfflineComplianceProvider,
    validate_policy_references,
    validate_semantics,
)
from integrations.retry import DependencyFailure
from rag.retrieval.compliance import ComplianceRetriever, rank_chunks
from rag.retrieval.compliance_context import minimal_evidence
from rules.compliance import calculate
from rules.hard_gates import check_hard_gates


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ComplianceStore:
    def __init__(self, enterprise):
        self.enterprise, self.db, self.lock = enterprise, enterprise.db, enterprise.lock
        with self.lock, self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS p4b_records (kind TEXT, key TEXT, revision INTEGER, "
                "body TEXT NOT NULL, PRIMARY KEY(kind,key,revision))"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS p4b_requests (scope TEXT, key TEXT, payload TEXT, "
                "result TEXT, PRIMARY KEY(scope,key))"
            )
            for table in ("p4b_records", "p4b_requests"):
                for action in ("UPDATE", "DELETE"):
                    self.db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()} "
                        f"BEFORE {action} ON {table} BEGIN "
                        "SELECT RAISE(ABORT, 'P4B immutable evidence'); END"
                    )

    def read(self, kind, key):
        with self.lock:
            row = self.db.execute(
                "SELECT revision,body FROM p4b_records WHERE kind=? AND key=? "
                "ORDER BY revision DESC LIMIT 1",
                (kind, key),
            ).fetchone()
        return (row[0], json.loads(row[1])) if row else (0, None)

    def append(self, kind, key, value):
        revision = self.read(kind, key)[0] + 1
        self.db.execute(
            "INSERT INTO p4b_records VALUES(?,?,?,?)",
            (kind, key, revision, json.dumps(value, ensure_ascii=False)),
        )
        return revision

    def replay(self, scope, key, payload):
        row = self.db.execute(
            "SELECT payload,result FROM p4b_requests WHERE scope=? AND key=?", (scope, key)
        ).fetchone()
        if not row:
            return None
        if row[0] != payload:
            raise APIError(409, "IDEMPOTENCY_CONFLICT", "相同请求编号用于不同内容")
        return json.loads(row[1])

    def remember(self, scope, key, payload, result):
        self.db.execute(
            "INSERT INTO p4b_requests VALUES(?,?,?,?)",
            (scope, key, payload, json.dumps(result, ensure_ascii=False)),
        )


class HybridCatalog:
    def __init__(self, legacy, store, repo, provider=None):
        self.legacy, self.store, self.repo = legacy, ComplianceStore(store), repo
        self.provider = provider or OfflineComplianceProvider()
        self.retriever = ComplianceRetriever(legacy.engine)

    def __getattr__(self, name):
        return getattr(self.legacy, name)

    def is_hybrid(self, cid):
        return self.legacy.is_hotel_matrix(cid) or self.store.read("SUBMISSION", cid)[1] is not None

    def parse_hybrid(self, state, raw):
        submission = self.store.read("SUBMISSION", state.claim_id)[1]
        if submission is None:
            return None
        expected = submission["fields"]
        matches = (
            expected == raw
            and str(state.expense.amount) == raw["amount"]
            and state.expense.incurred_on.isoformat() == raw["incurred_on"]
            and state.expense.city == raw["city"]
            and state.expense.category == "OTHER"
            and len(state.documents) == 1
            and state.documents[0].sha256
            == sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        )
        return {
            "documents": [
                d.model_copy(update={"parse_status": "PARSED" if matches else "FAILED"})
                for d in state.documents
            ],
            "audit_context": {
                "document_facts_match": matches,
                "submitted_facts": raw,
                "document_parser": "p4b.1 declaration",
                "business_analysis": {
                    "ambiguity": True,
                    "risk_factors": [],
                    "flight_cancellation": False,
                },
            },
        }

    def scope_source(self, pid, company):
        with Session(self.retriever.engine) as session:
            policy = session.get(PolicyVersion, pid)
            if not policy or policy.legal_entity != company:
                raise APIError(404, "POLICY_NOT_FOUND", "制度不存在")
            on = policy.effective_from
        try:
            return self.retriever.read(
                company=company,
                city="深圳",
                incurred_on=on,
                started=datetime.now(UTC),
                expected_id=pid,
            )
        except ValueError as exc:
            raise APIError(409, "P4B_POLICY_UNAVAILABLE", str(exc)) from None

    def scope_confirmed(self, company, source, started):
        _, row = self.store.read("SCOPE", company + ":" + source["policy_version_id"])
        return bool(
            row
            and row["extraction_digest"] == source["rules"]["extraction_digest"]
            and row["at"] <= started.isoformat()
        )

    def trusted(self, state, fields):
        company, employee = state.employee.legal_entity, state.employee.employee_id
        grade = self.legacy.facts.resolve(company, "EMPLOYEE", employee, state.expense.incurred_on)
        cities = [
            r
            for r in self.legacy.facts.list(company)
            if r["kind"] == "CITY"
            and r["effective_from"] <= fields["incurred_on"] < r["effective_to"]
            and fields["city"] in [r["value"]["name"], *r["value"].get("aliases", [])]
        ]
        city = cities[0] if len(cities) == 1 else None

        stored = self.legacy.facts.read(state.claim_id)

        def valid(r):
            return bool(
                r
                # A policy-derived city classification follows the selected published
                # hotel version, even when that version's start date is later than the
                # expense date. The policy cutoff remains enforced below.
                and (
                    r.get("policy_version_id")
                    or r["effective_from"] <= fields["trip_start"]
                )
                # Only HOTEL excludes checkout; other expenses include their last day.
                and (fields["trip_end"] <= r["effective_to"] if stored
                     else fields["trip_end"] < r["effective_to"])
            )

        snapshot = stored[0] if stored else {}
        # A published policy's exact city table is authoritative for hotel declarations.
        if snapshot.get("city", {}) and snapshot["city"].get("policy_version_id"):
            city = snapshot["city"]
        stay = snapshot.get("stay") or {}
        value = stay.get("value") or {}
        stay_valid = bool(
            stored
            and not snapshot.get("blockers")
            and snapshot.get("company") == company
            and value.get("employee_id") == employee
            and value.get("check_in") == fields["trip_start"]
            and value.get("check_out") == fields["trip_end"]
            and fields["trip_start"] < fields["trip_end"]
            and (snapshot.get("city") or {}).get("key") == value.get("city_key")
        )
        return {
            "stay_fact": stay if stay_valid else None,
            "department": state.employee.department,
            "grade": grade["value"]["grade"] if valid(grade) else "UNKNOWN",
            "city_tier": city["value"]["tier"] if valid(city) else "UNKNOWN",
            "employee_fact": grade,
            "city_fact": city,
        }

    def candidate_digest(self, cid):
        payload = dict(
            fields=self.repo.submission(cid),
            submission=self.store.read("SUBMISSION", cid)[1],
            invoice=self.repo.invoice_store.evidence(cid),
        )
        hotel = self.legacy.facts.read(cid)
        if hotel and hotel[0].get("employee_stay"):
            # An address-only correction must invalidate previous human attestations.
            payload["employee_hotel_declaration"] = hotel[0]["declaration"]
        return fingerprint(payload)

    def evaluate(self, state):
        if not self.is_hybrid(state.claim_id):
            return None
        fields = self.repo.submission(state.claim_id)
        submission = self.store.read("SUBMISSION", state.claim_id)[1]
        kind = submission["expense_type"] if submission else "HOTEL"
        revision, verified_record = self.store.read("FACTS", state.claim_id)
        verified = (
            (verified_record or {}).get("facts", {})
            if (verified_record or {}).get("candidate_digest")
            == self.candidate_digest(state.claim_id)
            else {}
        )
        trusted = self.trusted(state, fields)
        trusted["expense_mapping"] = {
            "storage_category": state.expense.category,
            "expense_type": kind,
            "authority": "SERVER_BOUND_P4B_SUBMISSION" if submission else "P4A_HOTEL_ADAPTER",
            "binding_checked": bool(
                (
                    submission
                    and submission["fields"] == fields
                    and state.expense.category == "OTHER"
                    and kind in {"ALLOWANCE", "ENTERTAINMENT"}
                )
                or (
                    not submission
                    and kind == "HOTEL"
                    and self.legacy.is_hotel_matrix(state.claim_id)
                )
            ),
        }
        started = datetime.fromisoformat(state.audit_context["started_at"])
        original = self.legacy.check_hotel(state) if kind == "HOTEL" else None
        source, evidence, error = None, [], None
        try:
            source = self.retriever.read(
                company=state.employee.legal_entity,
                city=state.expense.city,
                incurred_on=state.expense.incurred_on,
                started=started,
                expected_id=original.policy_version_id if original else None,
            )
            # A published hotel matrix was already source-checked and explicitly
            # confirmed through the P4A matrix workflow. It does not need the
            # separate generic P4B scope attestation used by other policy types.
            published_hotel_matrix = (
                kind == "HOTEL"
                and source.get("policy_rule_type") == "HOTEL_LIMIT_MATRIX"
                and getattr(self.provider, "protocol_version", None) == "p4b.advisory.4"
            )
            if (not published_hotel_matrix
                    and not self.scope_confirmed(state.employee.legal_entity, source, started)):
                raise ValueError("P4B_SCOPE_NOT_CONFIRMED")
            trusted["policy_verification"] = {
                "policy_version_id": source["policy_version_id"],
                "version": source["version"],
                "publication_and_authority_checked": True,
                "document_sha256": source["chunks"][0].document_sha256,
                "effective_from": source["chunks"][0].effective_from.isoformat(),
                "effective_to": source["chunks"][0].effective_to.isoformat(),
                "incurred_on": fields["incurred_on"],
                "scope_confirmation": (
                    {"authority": "P4A_PUBLISHED_HOTEL_MATRIX"}
                    if published_hotel_matrix
                    else self.store.read(
                        "SCOPE", state.employee.legal_entity + ":" + source["policy_version_id"]
                    )[1]
                ),
            }
            if kind == "ALLOWANCE" and (
                fields["trip_start"] < source["chunks"][0].effective_from.isoformat()
                or fields["trip_end"] >= source["chunks"][0].effective_to.isoformat()
            ):
                raise ValueError("P4B_TRIP_POLICY_INTERVAL_CONFLICT")
            query = " ".join(str(fields.get(k, "")) for k in ("purpose", "notes", "city"))
            evidence = rank_chunks(source["chunks"], query, expense_type=kind)
            if not evidence:
                raise ValueError("P4B_RETRIEVAL_EMPTY")
            required = [source["rules"]["meal_rates"]["chunk_id"]] if kind == "ALLOWANCE" else []
            if kind == "HOTEL":
                required.extend(
                    c.chunk_id
                    for c in source["chunks"]
                    if c.clause_id in {"第十三条", "第十六条"}
                    or (
                        c.source_position.get("table_index") == 5
                        and c.source_position.get("cells", [""])[0].startswith(
                            trusted["grade"] + " "
                        )
                    )
                )
            if kind == "ALLOWANCE" and trusted["city_tier"] in source["rules"]["allowances"]:
                required.append(source["rules"]["allowances"][trusted["city_tier"]]["chunk_id"])
            if kind == "ENTERTAINMENT":
                required.append(source["rules"]["entertainment"]["chunk_id"])
            evidence = [
                *{
                    c.chunk_id: c
                    for c in [*[c for c in source["chunks"] if c.chunk_id in required], *evidence]
                }.values()
            ]
            if getattr(self.provider, "protocol_version", None) in {
                "p4b.semantic.2", "p4b.advisory.1", "p4b.advisory.2", "p4b.advisory.3",
                "p4b.advisory.4",
            }:
                model_classifies_city = (
                    getattr(self.provider, "protocol_version", None) == "p4b.advisory.4"
                )
                evidence = minimal_evidence(
                    source["chunks"],
                    kind=kind,
                    grade=trusted["grade"],
                    # Current-data advice must let the model assess the submitted
                    # city against the full retrieved classification table. The
                    # server-resolved tier remains private and is used only to
                    # cross-check the model and calculate the limit.
                    city_tier=("UNKNOWN" if kind == "HOTEL" and model_classifies_city
                               else trusted["city_tier"]),
                    amount=state.expense.amount,
                    department=trusted["department"],
                    city=(trusted.get("city_fact") or {})
                    .get("value", {})
                    .get("name", fields["city"]),
                    entertainment_category=verified.get("entertainment_category"),
                )
        except (ValueError, ConnectionError) as exc:
            error = str(exc)
        if original is not None:
            rule = original
            hotel = self.legacy.facts.read(state.claim_id)
            if (hotel and hotel[0].get("employee_stay", {}).get("status") == "PENDING"
                    and original.outcome.value == "UNKNOWN" and source and not error):
                # The policy is known even while employee lodging evidence is pending.
                # Keep UNKNOWN and no limit; binding a version grants no compliance authority.
                rule = rule.model_copy(update={"policy_version_id": source["policy_version_id"]})
        elif (
            source
            and not error
            and trusted["grade"] != "UNKNOWN"
            and trusted["city_tier"] != "UNKNOWN"
        ):
            trip_days = (
                date.fromisoformat(fields["trip_end"]) - date.fromisoformat(fields["trip_start"])
            ).days + 1
            rule = calculate(state.expense, kind, verified, source, trusted["city_tier"], trip_days)
        else:
            rule = RuleResult(
                rule_id="P4B_" + kind,
                outcome="UNKNOWN",
                severity="REVIEW",
                reason=error or "可信职级或城市未知",
                actual=state.expense.amount,
                limit=None,
                policy_version_id=None,
            )
        metadata = {
            "p4b_enabled": True,
            "p4b_kind": kind,
            "p4b_policy_title": source["title"] if source else None,
            "p4b_fact_revision": revision,
            "p4b_verified": verified,
            "p4b_verification_record": verified_record,
            "p4b_invoice_digest": fingerprint(self.repo.invoice_store.evidence(state.claim_id)),
            "p4b_trusted": trusted,
            "p4b_source_error": error,
            "p4b_evidence": [e.model_dump(mode="json") for e in evidence],
            "authoritative_version_id": rule.policy_version_id or "",
        }
        if kind == "HOTEL":
            metadata["hotel_rule_result"] = self.legacy.facts.read(state.claim_id)[1]
        return {
            "hard_gate_results": check_hard_gates(state),
            "rule_results": [rule],
            "audit_context": metadata,
        }

    async def analyze_hybrid(self, state, dependency):
        context = state.audit_context
        key = state.claim_id + ":" + str(state.run_id)
        input_digest = fingerprint(
            dict(
                candidate=self.repo.submission(state.claim_id),
                verification=context.get("p4b_verification_record"),
                trusted=context["p4b_trusted"],
                evidence=context["p4b_evidence"],
                rules=[r.model_dump(mode="json") for r in state.rule_results],
                gates=[g.model_dump(mode="json") for g in state.hard_gate_results],
                invoice_digest=context.get("p4b_invoice_digest"),
                model=self.provider.model,
                protocol=getattr(self.provider, "protocol_version", "p4b.semantic.1"),
            )
        )
        prior = self.store.read("REPORT", key)[1]
        if prior is not None:
            if self.store.read("REPORT_INPUT", key)[1] != {"digest": input_digest}:
                raise ValueError("P4B_REPORT_INPUT_CHANGED")
            return self.report_result(ComplianceAnalysis.model_validate(prior), context, [])
        evidence = [SourceEvidence.model_validate(e) for e in context["p4b_evidence"]]
        verified, trusted = context["p4b_verified"], context["p4b_trusted"]
        required = required_facts(context["p4b_kind"])
        invoice = self.repo.invoice_store.evidence(state.claim_id)
        hints = self.repo.invoice_store.workflow_hints(state.claim_id) if invoice else None
        # A contract is a snapshot of existing authoritative evidence, never model text.
        contract = build_contract(
            claim_id=state.claim_id,
            kind=context["p4b_kind"],
            candidate=self.repo.submission(state.claim_id),
            trusted=trusted,
            verified=verified,
            record=context.get("p4b_verification_record"),
            rules=[r.model_dump(mode="json") for r in state.rule_results],
            invoice=invoice,
            invoice_integrity=bool(hints and hints["integrity"]),
            candidate_digest=self.candidate_digest(state.claim_id),
            claim_version=self.store.enterprise.claim_row(
                state.claim_id, {"company_id": state.employee.legal_entity, "role": "FINANCE"}
            )["version"],
            started_at=context["started_at"],
            contract_version="p4b.positive.2"
            if getattr(self.provider, "protocol_version", None) == "p4b.semantic.2"
            else "p4b.positive.1",
        )
        missing = [
            "VERIFIED_" + k.upper()
            for k in required
            if verified.get(k) is None or verified.get(k) is False
        ]
        if context["p4b_kind"] != "ALLOWANCE" and not self.repo.invoice_store.evidence(
            state.claim_id
        ):
            missing.append("ORIGINAL_INVOICE_REQUIRED")
        missing.extend("POSITIVE_" + key.upper() for key in contract.missing)
        semantic, events, calls, failure = None, [], [], context.get("p4b_source_error")
        semantic_validation = {}
        pid = context["authoritative_version_id"] or None
        if evidence and not failure and pid:
            try:
                validate_policy_references(
                    ComplianceRequest(
                        claim_id=state.claim_id,
                        policy_version_id=pid,
                        facts={},
                        rule_results=[r.model_dump(mode="json") for r in state.rule_results],
                        evidence=evidence,
                    )
                )
            except ValueError:
                failure = "P4B_RAG_RULE_VERSION_CONFLICT"
        if evidence and not failure and pid:

            async def call():
                nonlocal evidence
                candidate = self.repo.submission(state.claim_id)
                submitted = self.store.read("SUBMISSION", state.claim_id)[1]
                if submitted:
                    candidate = {**candidate, "claimed_meals": submitted.get("candidate_meals", {})}
                invoice = self.repo.invoice_store.evidence(state.claim_id)
                if fingerprint(invoice) != context.get("p4b_invoice_digest"):
                    raise ValueError("P4B_INVOICE_CHANGED")
                if invoice:
                    candidate = {
                        **candidate,
                        "invoice_evidence": {
                            "confirmed_fields": invoice["confirmed_fields"],
                            "ocr_fields": invoice["attachment"]["ocr"]["fields"],
                            "warnings": invoice["warnings"],
                            "discrepancy_reason": invoice["discrepancy_reason"],
                            "file_sha256": invoice["attachment"]["sha256"],
                            "authority": "NON_AUTHORITATIVE_OCR_AND_EMPLOYEE_CONFIRMATION",
                        },
                    }
                compact = getattr(self.provider, "protocol_version", None) == "p4b.semantic.2"
                if compact and invoice:
                    candidate["invoice_evidence"] = {
                        "confirmed_fields": invoice["confirmed_fields"],
                        "ocr_values": {
                            k: v.get("value")
                            for k, v in invoice["attachment"]["ocr"]["fields"].items()
                        },
                        "warnings": invoice["warnings"],
                        "discrepancy_reason": invoice["discrepancy_reason"],
                        "authority": "NON_AUTHORITATIVE_OCR_AND_EMPLOYEE_CONFIRMATION",
                    }
                record = {
                    k: v
                    for k, v in (context.get("p4b_verification_record") or {}).items()
                    if k != "field_attestations"
                } or None
                request = ComplianceRequest(
                    claim_id=state.claim_id,
                    policy_version_id=pid,
                    facts={
                        "candidate": candidate,
                        "verified": verified,
                        "positive": {
                            k: v.model_dump(mode="json") for k, v in contract.entries.items()
                        },
                        "trusted": {
                            **trusted,
                            "expense_type": context["p4b_kind"],
                            "human_verification_record": record,
                            "positive_scope": contract.scope,
                            "positive_required": contract.required,
                        },
                    },
                    rule_results=[r.model_dump(mode="json") for r in state.rule_results],
                    evidence=evidence,
                )
                protected = {
                    "第十三条",
                    "第十六条",
                    "第二十五条",
                    "第二十六条",
                    "第三十八条",
                    "第三十九条",
                    "第四十条",
                    "第四十一条",
                    "第四十三条",
                    "第四十四条",
                    "第四十五条",
                    "第五十一条",
                    "第五十二条",
                    "第五十三条",
                    "第五十四条",
                    "第五十六条",
                    "第五十七条",
                }
                while not compact and len(request.model_dump_json().encode()) > 32768:
                    removable = [
                        e
                        for e in request.evidence
                        if e.clause_id not in protected and not e.source_position.get("cells")
                    ]
                    if not removable:
                        raise ValueError("P4B_ESSENTIAL_CONTEXT_TOO_LARGE")
                    request.evidence.remove(removable[-1])
                evidence = request.evidence
                try:
                    validate_policy_references(request)
                    result = await self.provider.analyze(request)
                    if isinstance(result, CompactSemanticAnalysis):
                        semantic_validation.update(
                            status="PENDING", original_analysis=result.model_dump(mode="json")
                        )
                    if compact and not isinstance(result, CompactSemanticAnalysis):
                        raise ValueError("OUTPUT_PROTOCOL_MISMATCH")
                    validated = validate_semantics(result, request)
                    if isinstance(validated, CompactSemanticAnalysis):
                        semantic_validation["status"] = "VALIDATED"
                    return validated
                except ValueError as exc:
                    if semantic_validation:
                        semantic_validation.update(
                            status="REJECTED",
                            error=getattr(exc, "details", {"code": str(exc).split(":", 1)[0]}),
                        )
                    # Semantic validation carries only safe field names/codes, never output text.
                    code = str(exc).split(":", 1)[0]
                    request._provider_evidence["semantic_error"] = (
                        str(exc)[:200]
                        if code
                        in {
                            "UNSUPPORTED_EVIDENCE_REFERENCE",
                            "UNSUPPORTED_FACT_REFERENCE",
                            "UNVERIFIED_POSITIVE_REFERENCE",
                            "UNSUPPORTED_POLICY_CLAUSE",
                            "UNSUPPORTED_SEMANTIC_TOPIC",
                            "UNKNOWN_FACT_IS_NOT_COMPLIANT",
                            "POLICY_VERSION_REFERENCE_MISMATCH",
                            "DUPLICATE_FINDING_IDS",
                            "UNCERTAINTY_REQUIRED",
                            "EMPLOYEE_DECLARATION_IS_NOT_VERIFIED_FACT",
                            "SEMANTIC_COVERAGE_INCOMPLETE",
                            "RULE_ECHO_IS_NOT_SEMANTIC_REVIEW",
                            "CITATION_ATTRIBUTION_FAILURE",
                            "SEMANTIC_COVERAGE_INCONSISTENT",
                            "FACT_CONFLICT_IS_NOT_POLICY_CONFLICT",
                            "UNSUPPORTED_EVIDENCE_REQUIREMENT",
                            "REDUNDANT_VERIFIED_EVIDENCE_REQUEST",
                            "ABSENCE_IS_NOT_UNSUPPORTED_EXCEPTION",
                            "UNRESOLVED_CHECK_CANNOT_PASS",
                            "RULE_FAILURE_CANNOT_PASS",
                            "CATEGORY_CITATION_REQUIRED",
                            "OUTPUT_PROTOCOL_MISMATCH",
                            "UNSUPPORTED_ABSOLUTE_CLAIM",
                            "UNSUPPORTED_FACT_CLAIM",
                            "UNSUPPORTED_RULE_CLAIM",
                            "SEMANTIC_EXPLANATION_CONTRADICTION",
                            "RULE_EXPLANATION_CONTRADICTION",
                        }
                        else "SEMANTIC_VALIDATION_FAILED"
                    )
                    raise
                finally:
                    if request._provider_evidence:
                        calls.append(dict(request._provider_evidence))

            try:
                semantic, events, _ = await dependency(state, "llm", "p4b_semantics", call)
            except DependencyFailure as exc:
                events, failure = exc.events, "SEMANTIC_PROVIDER_FAILURE"
                if calls and calls[-1].get("error_code") == "CONTEXT_TOO_LARGE":
                    failure = "CONTEXT_TOO_LARGE"
        conflicts = (
            [failure] if failure and ("CONFLICT" in failure or "MISMATCH" in failure) else []
        )
        if semantic and semantic.exceptions:
            missing.extend(
                "VERIFIED_" + k.upper()
                for k in ("cancellation_notice", "itinerary_verified", "exception_authorized")
                if verified.get(k) is not True
            )
        status, action, risk, missing, conflicts = aggregate(
            rules=state.rule_results,
            gates=state.hard_gate_results,
            semantic=semantic,
            missing=missing,
            conflicts=conflicts,
            retrieval_valid=bool(evidence and not failure),
        )
        analysis_status = (
            "NO_POLICY"
            if not evidence
            else "FAILED"
            if semantic is None
            else "UNSUPPORTED"
            if semantic.provider == "mock"
            else "SUCCEEDED"
        )
        by_id = {e.chunk_id: e for e in evidence}
        explanation = semantic.explanation if semantic else failure or "缺少有效制度原文"
        summary_bindings = {}
        if isinstance(semantic, CompactSemanticAnalysis):
            from domain.compliance_summary import compose_summary

            explanation, summary_bindings = compose_summary(
                semantic,
                contract,
                [r.model_dump(mode="json") for r in state.rule_results],
                status,
                action,
            )
        report = ComplianceAnalysis(
            claim_id=state.claim_id,
            run_id=str(state.run_id),
            policy_version_id=pid,
            fact_revision=context["p4b_fact_revision"],
            expense_type=context["p4b_kind"],
            policy_title=context.get("p4b_policy_title"),
            trusted_facts=trusted,
            semantic_assessment=semantic,
            rule_results=[r.model_dump(mode="json") for r in state.rule_results],
            semantic_findings=semantic.semantic_findings if semantic else [],
            supporting_evidence=evidence,
            missing_evidence=list(dict.fromkeys(missing)),
            conflicts=list(dict.fromkeys(conflicts)),
            exceptions=semantic.exceptions if semantic else [],
            suggested_risk=risk,
            recommended_action=action,
            compliance_status=status,
            explanation=explanation,
            semantic_validation=semantic_validation,
            summary_bindings=summary_bindings,
            provider=semantic.provider if semantic else "unavailable",
            model=semantic.model if semantic else "unavailable",
            analysis_status=analysis_status,
            provider_calls=calls,
            positive_evidence=contract,
            semantic_coverage={
                "protocol": "p4b.semantic.2",
                "model": semantic.checks.model_dump(),
                "server": {
                    "positive": {k: v.status for k, v in contract.entries.items()},
                    "required": contract.required,
                    "rules": [r.outcome.value for r in state.rule_results],
                    "hard_gates": [g.outcome.value for g in state.hard_gate_results],
                },
            }
            if isinstance(semantic, CompactSemanticAnalysis)
            else {},
            citation_bindings=[
                {"finding_id": owner, **by_id[ref].model_dump(mode="json")}
                for owner, references in (
                    [("REVIEW", semantic.evidence_refs)]
                    + [(f.finding_id, f.evidence_refs) for f in semantic.semantic_findings]
                )
                for ref in references
            ]
            if isinstance(semantic, CompactSemanticAnalysis)
            else [],
        )
        data = report.model_dump(mode="json")
        with self.store.lock, self.store.db:
            prior = self.store.read("REPORT", key)[1]
            if prior is not None and prior != data:
                raise ValueError("P4B_REPORT_ALREADY_PINNED")
            if prior is None:
                self.store.append("REPORT", key, data)
                self.store.append("REPORT_INPUT", key, {"digest": input_digest})
        return self.report_result(report, context, events)

    @staticmethod
    def report_result(report, context, events):
        legacy = LLMAnalysis(
            summary=report.explanation,
            anomalies=[]
            if report.compliance_status == "CHECK_PASSED"
            else [report.compliance_status],
            suggested_risk=report.suggested_risk,
            confidence=0.0,
            provider=report.provider,
            model=report.model,
        )
        return {
            "llm_analysis": legacy,
            "audit_context": {
                "p4b_report": report.model_dump(mode="json"),
                "events": [*context.get("events", []), *events],
            },
        }

    def validate_hybrid_authority(self, state):
        if not state.audit_context.get("p4b_enabled"):
            return False
        key = state.claim_id + ":" + str(state.run_id)
        report = self.store.read("REPORT", key)[1]
        self.validate_report_authority(report, state)
        if self.store.read("FACTS", state.claim_id)[0] != report["fact_revision"]:
            raise ValueError("P4B verified facts changed; new audit required")
        invoice = self.repo.invoice_store.evidence(state.claim_id)
        hints = self.repo.invoice_store.workflow_hints(state.claim_id) if invoice else None
        if fingerprint(invoice) != state.audit_context.get("p4b_invoice_digest") or (
            hints and not hints["integrity"]
        ):
            raise ValueError("P4B invoice evidence changed or original bytes unavailable")
        checked = self.evaluate(state)
        if (
            checked["rule_results"] != state.rule_results
            or checked["audit_context"]["p4b_evidence"] != state.audit_context["p4b_evidence"]
            or checked["audit_context"]["p4b_trusted"] != state.audit_context["p4b_trusted"]
            or checked["audit_context"]["p4b_verified"] != state.audit_context["p4b_verified"]
            or checked["audit_context"]["p4b_source_error"]
        ):
            raise ValueError("P4B pinned rule/source/facts changed")
        if self.legacy.is_hotel_matrix(state.claim_id):
            self.legacy.validate_hotel_authority(state)
        return True

    def validate_report_authority(self, report, state):
        if (
            not report
            or report != state.audit_context.get("p4b_report")
            or report["analysis_status"] != "SUCCEEDED"
            or report["compliance_status"] != "CHECK_PASSED"
            or report["missing_evidence"]
            or report["conflicts"]
            or report["exceptions"]
            or report["recommended_action"] != "CHECK_PASSED"
            or any(f["conclusion"] != "COMPLIANT" for f in report["semantic_findings"])
        ):
            raise ValueError("P4B semantic/evidence review is unresolved")
        if report.get("semantic_coverage", {}).get("protocol") == "p4b.semantic.2":
            coverage = report["semantic_coverage"]
            from integrations.llm.compliance import SEMANTIC_TOPICS

            if (
                set(coverage["model"]) != SEMANTIC_TOPICS
                or any(v != "CLEAR" for v in coverage["model"].values())
                or any(
                    coverage["server"]["positive"].get(k) != "VERIFIED"
                    for k in coverage["server"]["required"]
                )
            ):
                raise ValueError("P4B compact semantic coverage unresolved")

    def report(self, cid, run_id):
        return self.store.read("REPORT", cid + ":" + str(run_id))[1] if run_id else None

    def verify(self, actor, cid, body, *, token, generation):
        with self.store.lock, self.store.db:
            self.store.db.execute(
                "UPDATE enterprise_claims SET claim_id=claim_id WHERE claim_id=?", (cid,)
            )
            actor = self.store.enterprise.live_actor(
                token, "FINANCE", "ADMIN", expected_generation=generation,
                company_id=actor["company_id"],
            )
            row = self.store.enterprise.claim_row(cid, actor)
            if row["owner_id"] == actor["id"]:
                raise APIError(403, "SELF_VERIFICATION_FORBIDDEN", "不能核实本人的申请")
            claim = self.repo.get(cid)
            context = self.repo.run_context(cid, claim.active_run_id) if claim.active_run_id else {}
            if (
                row["stage"] not in {"FINANCE_REVIEW", "AI_AUDITING", "RETURNED"}
                or (context or {}).get("human_review")
                or self.repo.erp_write_count(cid)
            ):
                raise APIError(409, "INVALID_TRANSITION", "已有决定或审核状态不可修改事实")
            scope, payload = actor["id"] + ":facts:" + cid, body.model_dump_json()
            replay = self.store.replay(scope, body.idempotency_key, payload)
            if replay is not None:
                return replay
            revision, old = self.store.read("FACTS", cid)
            if revision != body.expected_revision:
                raise APIError(409, "REVISION_CONFLICT", "核实记录已变化")
            digest = self.candidate_digest(cid)
            previous = (
                (old or {}).get("facts", {})
                if (old or {}).get("candidate_digest") == digest
                else {}
            )
            facts = {**previous, **body.facts.model_dump(exclude_unset=True)}
            VerifiedFacts.model_validate(facts)
            at = datetime.now(UTC).isoformat()
            attestations = dict((old or {}).get("field_attestations", {})) if previous else {}
            for field in body.facts.model_fields_set:
                attestations[field] = dict(
                    actor_id=actor["id"],
                    role=actor["role"],
                    at=at,
                    source_reference=body.source_reference,
                    basis=body.basis,
                    claim_version=row["version"],
                    candidate_digest=digest,
                )
            record = dict(
                facts=facts,
                field_attestations=attestations,
                actor_id=actor["id"],
                role=actor["role"],
                at=at,
                source_reference=body.source_reference,
                basis=body.basis,
                claim_version=row["version"],
                candidate_digest=digest,
            )
            revision = self.store.append("FACTS", cid, record)
            result = dict(revision=revision, record=record, requires_new_audit=True)
            self.store.remember(scope, body.idempotency_key, payload, result)
            self.store.enterprise.event(
                actor,
                "P4B_FACTS_VERIFIED",
                cid,
                fact_revision=revision,
                source_reference=body.source_reference,
                basis=body.basis,
            )
        return result

    @staticmethod
    def legacy_evidence(context):
        return [
            PolicyEvidence(
                policy_version_id=e["policy_version_id"],
                policy_key=e["document_id"],
                version="2026-11-V2",
                effective_from=e["effective_from"],
                effective_to=e["effective_to"],
                chunk_id=e["chunk_id"],
                source_uri="policy-original://" + e["document_id"],
                quote=e["source_text"],
            )
            for e in context["p4b_evidence"]
        ]
