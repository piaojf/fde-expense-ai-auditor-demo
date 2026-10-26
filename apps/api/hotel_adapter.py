"""Opt-in workflow dispatch; old claims retain their original catalog and parser."""

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import PolicyChunk, PolicyPublicationEvent, PolicyUpload, PolicyVersion
from domain.hotel_matrix import HotelFields
from domain.schemas import PolicyEvidence
from integrations.policies.hotel_matrix import extract_matrix
from rag.retrieval.pgvector import scope_rows
from rules.hotel_matrix import calculate, frozen_result


class HotelCatalog:
    def __init__(self, legacy, facts, engine):
        self.legacy, self.facts, self.engine = legacy, facts, engine
        self.facts.policy_city_resolver = self.policy_city

    def policy_city(self, company, fields):
        """Named cities are source-derived facts, never inferred from a model or address."""
        from integrations.policies.hotel_cities import named_city

        started = datetime.now(UTC)
        with Session(self.engine.execution_options(isolation_level="REPEATABLE READ")) as s:
            policy = self.policy(s, fields, company, started)
            last = self.policy(s, fields.model_copy(update={
                "incurred_on": fields.trip_end - timedelta(days=1)}), company, started)
            if not policy or not last or policy.policy_version_id != last.policy_version_id:
                return None
            rules = policy.authoritative_rules
            if rules.get("rule_type") != "HOTEL_LIMIT_MATRIX":
                return None
            source = s.get(PolicyUpload, rules.get("upload_id"))
            if (not source or not source.original
                    or hashlib.sha256(source.original).hexdigest() != rules.get("file_sha256")
                    or source.sha256 != rules.get("file_sha256")
                    or extract_matrix(source.original, source.parsed) != rules.get("matrix")):
                return None
            city = named_city(rules["matrix"], fields.city)
            if city is None:
                return None
            row = city.pop("source_row")
            return dict(
                id=f"POLICY_CITY:{policy.policy_version_id}:{city['name']}",
                key="policy-city:" + city["name"], kind="CITY", company=company,
                effective_from=policy.effective_from.isoformat(),
                effective_to=(policy.effective_to.isoformat() if policy.effective_to
                              else "9999-12-31"), value=city,
                source=f"upload://policies/{source.upload_id}#table-{row['source_table']}"
                       f"-row-{row['source_row']}",
                version=policy.version, actor="server:published-city-policy",
                recorded_at=started.isoformat(), policy_version_id=policy.policy_version_id,
                file_sha256=source.sha256, original_cells=row["original_cells"],
            )

    def __getattr__(self, name):
        return getattr(self.legacy, name)

    def is_hotel_matrix(self, cid):
        return self.facts.read(cid) is not None

    def parse_hotel_declaration(self, state, raw):
        f = HotelFields.model_validate(raw)
        matches = (
            f.amount == state.expense.amount
            and f.city == state.expense.city
            and f.nights == state.expense.nights
            and f.incurred_on == state.expense.incurred_on
            and len(state.documents) == 1
            and state.documents[0].sha256
            == hashlib.sha256(f.model_dump_json().encode()).hexdigest()
            and f.applicant_name == state.employee.name
            and f.department == state.employee.department
        )
        invoice = self.facts.repo.invoice_store.evidence(state.claim_id)
        # A declaration/verified booking alone does not prove invoice authenticity.
        warnings = ["INVOICE_REQUIRED"] if not invoice else []
        if f.notes:
            warnings.append("SPECIAL_CIRCUMSTANCES")
        return dict(
            documents=[
                d.model_copy(update={"parse_status": "PARSED" if matches else "FAILED"})
                for d in state.documents
            ],
            audit_context=dict(
                document_facts_match=matches,
                document_parser="p4a-v1 structured declaration",
                submitted_facts=f.model_dump(mode="json"),
                business_analysis=dict(
                    ambiguity=bool(warnings),
                    flight_cancellation=False,
                    risk_factors=warnings,
                    automatic_exception_rule=False,
                ),
            ),
        )

    def policy(self, s, fields, company, started):
        filters = dict(
            legal_entity=company,
            category="HOTEL",
            city=fields.city,
            incurred_on=fields.incurred_on,
            audit_started_at=started,
        )
        candidates = scope_rows(s, **filters)
        official = [p for p in candidates if p.authority_level >= 100]
        # One source of authority for P4A rules; ambiguity never gets date-based precedence.
        if len(official) == 1:
            return official[0]
        if official:
            return None
        # No fallback to FAQ after the published matrix expires or retires.
        governed = s.scalar(
            select(PolicyPublicationEvent.event_id)
            .join(
                PolicyVersion,
                PolicyPublicationEvent.policy_version_id == PolicyVersion.policy_version_id,
            )
            .where(
                PolicyVersion.legal_entity == company,
                PolicyVersion.city == "*",
                PolicyPublicationEvent.effective_on <= fields.incurred_on,
                PolicyPublicationEvent.recorded_at <= started,
            )
            .limit(1)
        )
        if governed:
            return None
        return candidates[0] if len(candidates) == 1 else None

    def check_hotel(self, state):
        stored = self.facts.read(state.claim_id)
        if stored is None:
            return None
        facts, _ = stored
        f = HotelFields.model_validate(facts["declaration"]["fields"])
        started = datetime.fromisoformat(state.audit_context["started_at"])
        with Session(self.engine.execution_options(isolation_level="REPEATABLE READ")) as s:
            p = self.policy(s, f, facts["company"], started)
            # A stay spanning different policy intervals cannot use a single nightly limit.
            local = dict(facts, blockers=list(facts["blockers"]))
            last = self.policy(
                s,
                f.model_copy(update={"incurred_on": f.trip_end - timedelta(days=1)}),
                facts["company"],
                started,
            )
            if p and (last is None or last.policy_version_id != p.policy_version_id):
                local["blockers"].append("POLICY_CHANGES_DURING_STAY")
            if p and p.authoritative_rules.get("rule_type") == "HOTEL_LIMIT_MATRIX":
                r = p.authoritative_rules
                source = s.get(PolicyUpload, r.get("upload_id", ""))
                if (
                    not source
                    or source.original is None
                    or (
                        hashlib.sha256(source.original).hexdigest() != r.get("file_sha256")
                        or extract_matrix(source.original, source.parsed) != r["matrix"]
                    )
                ):
                    local["blockers"].append("PUBLISHED_SOURCE_INTEGRITY")
                expected_cells = [
                    dict(
                        **cell,
                        legal_entity=p.legal_entity,
                        policy_version_id=p.policy_version_id,
                        effective_from=p.effective_from.isoformat(),
                        effective_to=p.effective_to.isoformat() if p.effective_to else None,
                        source_document_id=r["upload_id"],
                    )
                    for cell in r["matrix"]["rules"]
                ]
                if r.get("cells") != expected_cells:
                    local["blockers"].append("MATRIX_CELL_SCOPE_INTEGRITY")
            report = calculate(f, local, p)
        report["audit_started_at"] = started.isoformat()
        self.facts.save_report(state.claim_id, report, state.run_id)
        return frozen_result(report)

    def validate_hotel_authority(self, state):
        if not self.is_hotel_matrix(state.claim_id):
            return False
        saved = self.facts.read(state.claim_id)[1]
        checked = self.check_hotel(state)
        facts = self.facts.read(state.claim_id)[0]
        if (state.audit_context.get("p4b_advisory_mode")
                and facts.get("employee_stay", {}).get("status") == "PENDING"
                and checked.outcome.value == "UNKNOWN"
                and checked.policy_version_id is None):
            # Hybrid evaluation pins the known policy independently of unresolved
            # lodging facts. Recheck that same binding without upgrading the rule.
            checked = checked.model_copy(update={
                "policy_version_id": state.audit_context.get("authoritative_version_id") or None,
            })
        if saved != self.facts.read(state.claim_id)[1] or checked != state.rule_results[0]:
            raise ValueError("Pinned P4A rule/facts changed")
        # Missing invoice or special circumstances may never be resolved by a monetary PASS.
        if not self.facts.repo.invoice_store.evidence(state.claim_id):
            raise ValueError("Verified invoice required")
        if not state.policy_evidence or any(
            p.policy_version_id != checked.policy_version_id
            or (p.effective_to is not None and state.expense.incurred_on >= p.effective_to)
            for p in state.policy_evidence
        ):
            raise ValueError("RAG and Rule Engine version conflict")
        return True

    def hotel_evidence(self, state, evidence):
        if not self.is_hotel_matrix(state.claim_id):
            return evidence
        report = self.facts.read(state.claim_id)[1]
        pid = report["policy_version_id"] if report else None
        if not evidence or any(e.policy_version_id != pid for e in evidence):
            return []
        source = report.get("source_reference") or {}
        if not source.get("original_row"):
            return evidence
        quote = "\n".join(source["original_row"])
        with Session(self.engine) as s:
            p = s.get(PolicyVersion, pid)
            chunk = s.scalar(
                select(PolicyChunk)
                .where(
                    PolicyChunk.policy_version_id == pid,
                    PolicyChunk.content.contains(quote, autoescape=True),
                )
                .order_by(PolicyChunk.chunk_id)
            )
            if not p or not chunk:
                return []
            exact = PolicyEvidence(
                policy_version_id=pid,
                policy_key=p.policy_key,
                version=p.version,
                effective_from=p.effective_from,
                effective_to=p.effective_to,
                chunk_id=chunk.chunk_id,
                source_uri=p.source_uri,
                quote=quote,
            )
        return [exact, *evidence]
