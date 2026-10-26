"""Server-owned P4B evidence snapshot; model output never creates verification."""

from typing import Any, Literal

from pydantic import Field, model_validator

from domain.schemas import DTO


class EvidenceItem(DTO):
    evidence_id: str
    status: Literal["VERIFIED", "DECLARED", "MISSING", "CONFLICTED"]
    value: Any = None
    source_reference: str | None = None
    verified_by: str | None = None
    verified_at: str | None = None
    scope: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def provenance_required(self):
        if self.status == "VERIFIED" and (
            self.value is None
            or not self.source_reference
            or not self.verified_by
            or not self.verified_at
            or not self.scope
        ):
            raise ValueError("VERIFIED_REQUIRES_PROVENANCE_AND_SCOPE")
        return self


class PositiveEvidenceContract(DTO):
    contract_version: Literal["p4b.positive.1", "p4b.positive.2"] = "p4b.positive.1"
    scope: dict
    entries: dict[str, EvidenceItem]
    required: list[str]
    missing: list[str]
    invoice_requirement: Literal["ORIGINAL_AND_FINANCE_VERIFICATION", "ALLOWANCE_BUSINESS_RECORDS"]


def required_facts(kind):
    names = ["business_related", "prior_approval", "duplicate_clear"]
    if kind != "ALLOWANCE":
        names.append("invoice_verified")
    if kind == "ALLOWANCE":
        names += ["full_days", "lunches", "dinners"]
    if kind == "ENTERTAINMENT":
        names += ["participants", "customer_identified", "entertainment_category"]
    return names


def build_contract(
    *,
    claim_id,
    kind,
    candidate,
    trusted,
    verified,
    record,
    rules,
    invoice,
    invoice_integrity,
    candidate_digest,
    claim_version,
    started_at,
    contract_version="p4b.positive.1",
):
    """Called only after authority checks; never trusts employee status/source labels."""
    scope = dict(
        claim_id=claim_id,
        claim_version=claim_version,
        candidate_digest=candidate_digest,
        incurred_on=candidate["incurred_on"],
        trip_start=candidate["trip_start"],
        trip_end=candidate["trip_end"],
    )
    shared_scope = scope
    scope = {"contract_scope": "trusted.positive_scope"}
    entries = {}

    def put(
        key,
        value,
        *,
        source=None,
        actor=None,
        at=None,
        valid_scope=None,
        conflicted=False,
        declared=False,
    ):
        status = "DECLARED" if declared and value is not None else "MISSING"
        if conflicted:
            status = "CONFLICTED"
        elif value is not None and source and actor and at and valid_scope:
            status = "VERIFIED"
        entries[key] = EvidenceItem(
            evidence_id=f"FACT:{claim_id}:{key}",
            status=status,
            value=value,
            source_reference=source,
            verified_by=actor,
            verified_at=at,
            scope=valid_scope or scope,
        )

    employee = trusted.get("employee_fact") or {}
    city = trusted.get("city_fact") or {}
    for key, value, master in [
        ("employee", employee.get("key"), employee),
        ("grade", trusted.get("grade"), employee),
        ("city_tier", trusted.get("city_tier"), city),
    ]:
        put(
            key,
            value,
            source=master.get("source"),
            actor=master.get("actor"),
            at=master.get("recorded_at"),
            valid_scope={
                **scope,
                "effective_from": master.get("effective_from"),
                "effective_to": master.get("effective_to"),
                "master_id": master.get("id"),
            },
            conflicted=value == "UNKNOWN",
        )
    if kind == "HOTEL":
        stay = trusted.get("stay_fact") or {}
        value = stay.get("value")
        put(
            "stay",
            value,
            source=stay.get("source"),
            actor=stay.get("actor"),
            at=stay.get("recorded_at"),
            valid_scope={
                **scope,
                "master_id": stay.get("id"),
                "effective_from": stay.get("effective_from"),
                "effective_to": stay.get("effective_to"),
            },
        )
    mapping = trusted.get("expense_mapping") or {}
    put(
        "expense_type",
        kind,
        source="SERVER:P4B_BOUND_SUBMISSION",
        actor="server:p4b",
        at=started_at,
        valid_scope=scope,
        conflicted=not mapping.get("binding_checked"),
    )
    put(
        "category_mapping",
        mapping,
        source="SERVER:P4B_CATEGORY_ADAPTER",
        actor="server:p4b",
        at=started_at,
        valid_scope=scope,
        conflicted=not mapping.get("binding_checked"),
    )
    policy = trusted.get("policy_verification") or {}
    confirmation = policy.get("scope_confirmation") or {}
    pid = policy.get("policy_version_id")
    valid_policy = policy.get("publication_and_authority_checked") is True
    put(
        "policy_version",
        pid,
        source=policy.get("document_sha256") if valid_policy else None,
        actor=confirmation.get("actor_id"),
        at=confirmation.get("at"),
        valid_scope={
            **scope,
            "policy_version_id": pid,
            "effective_from": policy.get("effective_from"),
            "effective_to": policy.get("effective_to"),
        },
    )
    rule_conflict = not pid or any(r.get("policy_version_id") != pid for r in rules)
    put(
        "rule_results",
        rules,
        source="SERVER:DETERMINISTIC_RULE_ENGINE",
        actor="server:rules",
        at=started_at,
        valid_scope={**scope, "policy_version_id": pid},
        conflicted=rule_conflict or not rules,
    )
    record = record or {}
    fresh = record.get("candidate_digest") == candidate_digest
    attestations = record.get("field_attestations", {}) if fresh else {}
    for key in sorted(set(required_facts(kind)) | set(verified)):
        attestation = attestations.get(key, {})
        # Historical snapshots without per-field provenance cannot be upgraded here.
        value = verified.get(key)
        valid = (
            fresh
            and attestation.get("candidate_digest") == candidate_digest
            and type(attestation.get("claim_version")) is int
            and 1 <= attestation["claim_version"] <= claim_version
            and attestation.get("role") in {"FINANCE", "ADMIN"}
        )
        put(
            key,
            value,
            source=attestation.get("source_reference") if valid else None,
            actor=attestation.get("actor_id") if valid else None,
            at=attestation.get("at") if valid else None,
            valid_scope={
                **scope,
                "basis": attestation.get("basis"),
                "attested_claim_version": attestation.get("claim_version"),
            },
            conflicted=value is False or (value is not None and not valid),
        )
    if kind != "ALLOWANCE":
        put(
            "original_invoice",
            invoice.get("attachment", {}).get("sha256") if invoice else None,
            source="SERVER:BOUND_INVOICE_BYTES" if invoice else None,
            actor="server:invoice",
            at=started_at,
            valid_scope=scope,
            conflicted=bool(invoice and not invoice_integrity),
        )
    for key in ("purpose", "notes"):
        put("declared_" + key, candidate.get(key), declared=True)
    required = [
        "employee",
        "grade",
        "city_tier",
        "expense_type",
        "category_mapping",
        "policy_version",
        "rule_results",
        *required_facts(kind),
    ]
    if kind != "ALLOWANCE":
        required.append("original_invoice")
    if kind == "HOTEL" and contract_version == "p4b.positive.2":
        required.append("stay")
    return PositiveEvidenceContract(
        contract_version=contract_version,
        scope=shared_scope,
        entries=entries,
        required=required,
        missing=[key for key in required if entries[key].status != "VERIFIED"],
        invoice_requirement="ALLOWANCE_BUSINESS_RECORDS"
        if kind == "ALLOWANCE"
        else "ORIGINAL_AND_FINANCE_VERIFICATION",
    )
