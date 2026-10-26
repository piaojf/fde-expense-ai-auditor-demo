"""Request-local, immutable evidence identities. Never infer or repair model keys."""

import hashlib
import json
import re
from uuid import uuid4

from integrations.llm.compliance_v2 import AttributionError, input_projection
from rag.retrieval.compliance_context import minimal_evidence


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _checked_scope(request):
    # Reuse authoritative SHA/version/integrity/completeness checks, without pruning text.
    input_projection(request)
    for e in request.evidence:
        position = e.source_position
        if position.get("table_index"):
            identity = f"TABLE-{position['table_index']}-ROW-{position['row_index']}"
            text = " | ".join(position["header_row"]) + "\n" + " | ".join(position["cells"])
        else:
            match = re.match(r"^(第[一二三四五六七八九十百]+条)", e.source_text)
            identity = match[1] if match else None
            text = e.source_text
        if (position.get("part") != "word/document.xml" or identity != e.clause_id
                or text != e.source_text):
            raise AttributionError("P4B_CONTEXT_CHUNK_IDENTITY", "evidence_selector")
    trusted = request.facts["trusted"]
    incurred = request.facts["candidate"]["incurred_on"]
    policy = trusted["policy_verification"]
    if policy["incurred_on"] != incurred or any(
        (trusted["expense_type"] != "HOTEL" and incurred < e.effective_from.isoformat())
        or e.effective_to and incurred >= e.effective_to.isoformat()
        for e in request.evidence
    ):
        raise AttributionError("POLICY_VERSION_REFERENCE_MISMATCH", "evidence_selector")
    city = (trusted.get("city_fact") or {}).get("value", {}).get(
        "name", request.facts["candidate"].get("city", ""))
    applicable = minimal_evidence(
        request.evidence, kind=trusted["expense_type"], grade=trusted["grade"],
        city_tier=("UNKNOWN" if trusted.get("city_classification_by_model")
                   and trusted["expense_type"] == "HOTEL" else trusted["city_tier"]),
        amount=request.rule_results[0]["actual"],
        department=trusted.get("department", ""), city=city,
        entertainment_category=request.facts.get("verified", {}).get("entertainment_category"),
    )
    # Every candidate must belong to the current governed scope, including table rows.
    if {e.chunk_id for e in applicable} != {e.chunk_id for e in request.evidence}:
        raise AttributionError("INAPPLICABLE_EVIDENCE", "evidence_selector")
    return dict(expense_type=trusted["expense_type"], grade=trusted["grade"], city=city,
                city_tier=trusted["city_tier"], department=trusted.get("department", ""),
                incurred_on=incurred, positive_scope=trusted["positive_scope"])


def prepare_selector(request):
    """Pin exactly one invocation, even when two requests have identical candidates."""
    scope = _checked_scope(request)
    payload = dict(
        selector_version="p4b.evidence-selector.1", invocation_id=uuid4().hex,
        request=request.model_dump(mode="json"),
        entries=[dict(short_id=f"E{n:02d}", original_chunk_id=e.chunk_id,
                      **e.model_dump(mode="json", exclude={"chunk_id"}), applicable_scope=scope)
                 for n, e in enumerate(request.evidence, 1)],
    )
    manifest = dict(evidence_snapshot_id=digest(payload), **payload)
    # Immutable string, separate from model data and transport telemetry.
    request._evidence_selector = canonical(manifest)
    request._evidence_selector_attempts += (request._evidence_selector,)
    return manifest


def selector_manifest(request):
    if request._evidence_selector is None:
        raise AttributionError("EVIDENCE_SELECTOR_NOT_PINNED", "evidence_selector")
    manifest = json.loads(request._evidence_selector)
    payload = {k: v for k, v in manifest.items() if k != "evidence_snapshot_id"}
    if (digest(payload) != manifest["evidence_snapshot_id"]
            or canonical(request.model_dump(mode="json")) != canonical(manifest["request"])):
        raise AttributionError("EVIDENCE_SNAPSHOT_MISMATCH", "evidence_selector")
    _checked_scope(request)
    return manifest


def resolve_keys(keys, manifest, field):
    by_key = {e["short_id"]: e["original_chunk_id"] for e in manifest["entries"]}
    if (not keys or len(keys) != len(set(keys)) or not set(keys) <= by_key.keys()
            or len(by_key) != len(manifest["entries"])
            or len(set(by_key.values())) != len(by_key)):
        raise AttributionError("UNSUPPORTED_EVIDENCE_KEY", field, refs=keys)
    return [by_key[key] for key in keys]
