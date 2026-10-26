"""Compact protocol/source/real read-only replay tests, zero remote transport."""

import asyncio
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from p4b_fix2_support import authored_review, finding, positive_output, reference
from pydantic import ValidationError
from test_p4b_compliance import chunks
from test_p4b_fix import contract_input

from domain.compliance import ComplianceRequest
from domain.compliance_v2 import CompactOutput, CompactSemanticAnalysis
from domain.positive_evidence import build_contract
from integrations.llm.compliance import ComplianceDeepSeekProvider, validate_semantics
from integrations.llm.compliance_v2 import CompactComplianceDeepSeekProvider, input_projection
from integrations.llm.deepseek import DeepSeekOutputError
from rag.retrieval.compliance import SOURCE_SHA256
from rag.retrieval.compliance_context import CATEGORY, COMMON, minimal_evidence

REPLAY = Path(__file__).parent / "fixtures/p4b_replay_20261012"


def compact_request(kind="ALLOWANCE"):
    args = contract_input()
    args["kind"] = kind
    args["contract_version"] = "p4b.positive.2"
    trusted = args["trusted"]
    trusted["expense_type"] = kind
    trusted["department"] = "华南销售部"
    trusted["expense_mapping"]["expense_type"] = kind
    trusted["policy_verification"].update(
        document_sha256=SOURCE_SHA256, version="2026-11-V2", incurred_on="2026-11-02"
    )
    args["candidate"]["city"] = "深圳"
    args["candidate"]["notes"] = "独立合成契约输入"
    if kind != "ALLOWANCE":
        args["verified"]["invoice_verified"] = True
        args["record"]["field_attestations"]["invoice_verified"] = deepcopy(
            args["record"]["field_attestations"]["business_related"]
        )
        args.update(invoice={"attachment": {"sha256": "b" * 64}}, invoice_integrity=True)
    if kind == "HOTEL":
        master = trusted["employee_fact"]
        trusted["stay_fact"] = dict(
            **master,
            value=dict(
                employee_id="employee-1",
                city_key="shenzhen",
                check_in="2026-11-02",
                check_out="2026-11-03",
            ),
        )
        args["candidate"]["trip_end"] = "2026-11-03"
    if kind == "ENTERTAINMENT":
        args["verified"].update(
            participants=4, customer_identified=True, entertainment_category="GENERAL"
        )
        for name in ("participants", "customer_identified", "entertainment_category"):
            args["record"]["field_attestations"][name] = deepcopy(
                args["record"]["field_attestations"]["business_related"]
            )
    contract = build_contract(**args)
    trusted["positive_scope"] = contract.scope
    trusted["positive_required"] = contract.required
    return ComplianceRequest(
        claim_id=args["claim_id"],
        policy_version_id="V2",
        facts=dict(
            candidate=args["candidate"],
            verified=args["verified"],
            trusted=trusted,
            positive={k: v.model_dump(mode="json") for k, v in contract.entries.items()},
        ),
        rule_results=args["rules"],
        evidence=minimal_evidence(
            chunks(),
            kind=kind,
            grade="D",
            city_tier="I",
            amount="680" if kind == "HOTEL" else "36",
            department="华南销售部",
            entertainment_category="GENERAL",
        ),
    )


def normalized(data):
    return CompactSemanticAnalysis(**data, provider="deepseek", model="offline-fixture")


@pytest.mark.parametrize("kind", CATEGORY)
def test_minimal_context_is_complete_unique_category_specific_and_bounded(kind):
    req = compact_request(kind)
    assert COMMON | CATEGORY[kind] <= {e.clause_id for e in req.evidence}
    assert len(req.evidence) == len({e.chunk_id for e in req.evidence})
    wire = input_projection(req)
    by_id = {e.chunk_id: e for e in chunks()}
    assert all(item["text"] == by_id[item["chunk_id"]].source_text for item in wire["evidence"])
    assert all(e.document_sha256 == SOURCE_SHA256 for e in req.evidence)
    tables = {e.source_position.get("table_index") for e in req.evidence}
    assert ({5, 6} if kind == "HOTEL" else {10} if kind == "ALLOWANCE" else {11}) <= tables
    assert (
        not ({10, 11} if kind == "HOTEL" else {5, 6, 11} if kind == "ALLOWANCE" else {5, 6, 10})
        & tables
    )
    body = CompactComplianceDeepSeekProvider(api_key="synthetic-key", max_tokens=4096)._body(req)
    assert len(httpx.Request("POST", "https://example.invalid", json=body).content) <= 32768
    assert body["max_tokens"] == 2048
    assert validate_semantics(normalized(positive_output(req)), req).findings == []


@pytest.mark.parametrize(
    "clause",
    sorted(
        COMMON
        | CATEGORY["HOTEL"]
        | {"第五十五条", "TABLE-15-ROW-1", "TABLE-15-ROW-5", "TABLE-15-ROW-6"}
    ),
)
def test_missing_essential_evidence_is_never_deleted_to_fit_budget(clause):
    source = [e for e in chunks() if e.clause_id != clause]
    with pytest.raises(ValueError, match="ESSENTIAL_EVIDENCE_MISSING"):
        minimal_evidence(source, kind="HOTEL", grade="D", city_tier="I", amount="680")


@pytest.mark.parametrize("mutation", ["text", "sha", "version", "duplicate", "appendix"])
def test_source_mapping_integrity_and_appendix_rejection(mutation):
    source = chunks()
    updates = {
        "text": {"source_text": "篡改"},
        "sha": {"document_sha256": "0" * 64},
        "version": {"policy_version_id": "other"},
        "appendix": {"section": "附录A"},
    }
    if mutation == "duplicate":
        source.append(source[0])
    else:
        source[0] = source[0].model_copy(update=updates[mutation])
    with pytest.raises(ValueError):
        minimal_evidence(source, kind="HOTEL", grade="D", city_tier="I", amount="680")


def test_context_overflow_keeps_all_required_evidence_and_makes_zero_http_calls():
    req = compact_request()
    req.facts["candidate"]["notes"] = "必要且未截断的业务说明" * 4000
    before = req.model_dump_json()
    calls = []
    provider = CompactComplianceDeepSeekProvider(
        api_key="synthetic-key", transport=httpx.MockTransport(lambda r: calls.append(r))
    )
    with pytest.raises(DeepSeekOutputError, match="CONTEXT_TOO_LARGE"):
        asyncio.run(provider.analyze(req))
    assert not calls and before == req.model_dump_json()
    assert req._provider_evidence["request_bytes"] > 32768


@pytest.mark.parametrize(
    "mutation",
    [
        "version",
        "missing_check",
        "missing_explanation",
        "provider_field",
        "extra_approval",
        "bad_state",
    ],
)
def test_new_schema_is_strict_and_never_repairs_missing_fields(mutation):
    data = positive_output(compact_request())
    if mutation == "version":
        data["schema_version"] = "p4b.semantic.1"
    if mutation == "missing_check":
        data["checks"].pop("EXCEPTION_REASON")
    if mutation == "missing_explanation":
        data.pop("explanation")
    if mutation == "provider_field":
        data["provider"] = "deepseek"
    if mutation == "extra_approval":
        data["approved"] = True
    if mutation == "bad_state":
        data["checks"]["AUTHORIZATION"] = "NOT_APPLICABLE"
    with pytest.raises(ValidationError):
        CompactOutput.model_validate(data, strict=True)


@pytest.mark.parametrize(
    "mutation",
    [
        "fake_id",
        "wrong_clause",
        "unrelated_real_id",
        "false_fact",
        "candidate_only",
        "missing_finding",
        "redundant_proof",
        "fact_as_policy",
        "unsupported_no_exception",
    ],
)
def test_semantic_attribution_unknown_fact_and_redundant_evidence_are_rejected(mutation):
    req = compact_request("HOTEL")
    data = positive_output(req)
    f = finding(
        req,
        topic="EVIDENCE_SUFFICIENCY",
        basis="HOTEL",
        clause="第十八条",
        text="住宿核实仍有具体未决事项。",
    )
    data = authored_review(req, [f])
    if mutation == "fake_id":
        f["evidence_refs"] = ["fake"]
    if mutation == "wrong_clause":
        f["explanation"] = "第十六条明确此项。"
    if mutation == "unrelated_real_id":
        f["explanation"] = "招待人均标准为250元，供餐扣减40%。"
    if mutation == "false_fact":
        data = positive_output(req)
        req.facts["positive"]["business_related"]["status"] = "CONFLICTED"
    if mutation == "candidate_only":
        data["factual_basis"] = ["candidate.notes"]
    if mutation == "missing_finding":
        data["findings"] = []
    if mutation == "redundant_proof":
        f["required_evidence"] = ["stay"]
    if mutation == "fact_as_policy":
        f.update(
            topic="FACT_POLICY_CONFLICT",
            basis="BUSINESS",
            conclusion="POLICY_CONFLICT",
            evidence_refs=[reference(req, "第五十一条")],
            explanation="存在私人目的矛盾。",
        )
    if mutation == "unsupported_no_exception":
        # An UNKNOWN topic cannot simultaneously assert CLEAR/no unresolved issue.
        f.update(
            topic="EXCEPTION_REASON",
            basis="EXCEPTION",
            conclusion="UNSUPPORTED",
            evidence_refs=[reference(req, "第十三条")],
        )
    if mutation in {"fact_as_policy", "unsupported_no_exception"}:
        data["checks"]["EVIDENCE_SUFFICIENCY"] = "CLEAR"
    with pytest.raises(ValueError):
        validate_semantics(normalized(data), req)


@pytest.mark.parametrize("attempt", range(1, 10))
def test_actual_responses_are_read_only_and_not_repaired_or_reinterpreted_as_v2(attempt):
    name = f"attempt-{attempt:02d}-response.json"
    raw = (REPLAY / name).read_bytes()
    item = next(
        f
        for f in json.loads((REPLAY / "manifest.json").read_text(encoding="utf-8"))["files"]
        if f["file"] == name
    )
    assert hashlib.sha256(raw).hexdigest() == item["sha256"]
    envelope = json.loads(raw)
    assert envelope["choices"][0]["finish_reason"] == "stop"
    req = compact_request()
    legacy = ComplianceDeepSeekProvider(api_key="synthetic-key", model="deepseek-flash")
    if attempt in {1, 2, 3, 4, 6}:
        with pytest.raises(DeepSeekOutputError):
            legacy._decode(raw, req)
        if attempt != 6:
            assert req._provider_evidence["validation_errors"] == [
                {"loc": ["provider"], "type": "missing"},
                {"loc": ["model"], "type": "missing"},
            ]
        else:
            assert req._provider_evidence["validation_errors"][0]["offset"] == 5674
    else:
        assert legacy._decode(raw, req).provider == "deepseek"  # DTO only, no semantic PASS claim.
    new = CompactComplianceDeepSeekProvider(api_key="synthetic-key", model="deepseek-flash")
    with pytest.raises(DeepSeekOutputError):
        new._decode(raw, req)
    assert (REPLAY / name).read_bytes() == raw


@pytest.mark.parametrize("sample,attempt,index", [(1, 1, 7), (3, 3, 7), (4, 4, 3)])
def test_actual_invalid_dto_clause_mismatches_stay_rejected_in_read_only_diagnostics(
    sample, attempt, index
):
    from datetime import date

    from integrations.llm.compliance_v2 import _validate_citations
    from rag.retrieval.compliance import original_chunks

    meta = json.loads(
        (REPLAY / f"live-sample-{sample:02d}-input-metadata.json").read_text(encoding="utf-8")
    )
    envelope = json.loads(
        (REPLAY / f"attempt-{attempt:02d}-response.json").read_text(encoding="utf-8")
    )
    data = json.loads(envelope["choices"][0]["message"]["content"])
    original = original_chunks(
        Path("tests/fixtures/policies/p4a-222.docx").read_bytes(),
        document_id="diagnostic-only",
        policy_version_id=meta["policy_version_id"],
        effective_from=date(2026, 11, 1),
        effective_to=date(2027, 1, 1),
    )
    f = data["semantic_findings"][index]
    with pytest.raises(ValueError, match="UNSUPPORTED_POLICY_CLAUSE"):
        _validate_citations(
            f["evidence_refs"],
            f["explanation"] + " " + f["uncertainty"],
            {e.chunk_id: e for e in original},
        )
    assert "provider" not in data and "model" not in data  # No fields supplied to make DTO valid.


def test_missing_evidence_blocked_before_transport_and_wire_paths_have_scope_resolution():
    req = compact_request("HOTEL")
    projection = input_projection(req)
    assert projection["facts"]["scope"] == req.facts["trusted"]["positive_scope"]
    req.evidence = [e for e in req.evidence if e.clause_id != "第十三条"]
    calls = []
    provider = CompactComplianceDeepSeekProvider(
        api_key="synthetic-key", transport=httpx.MockTransport(lambda r: calls.append(r))
    )
    with pytest.raises(ValueError, match="ESSENTIAL_EVIDENCE_MISSING"):
        asyncio.run(provider.analyze(req))
    assert not calls


@pytest.mark.parametrize(
    "status,value",
    [("VERIFIED", False), ("VERIFIED", "UNKNOWN"), ("DECLARED", True), ("MISSING", None)],
)
def test_claimed_clear_cannot_use_false_unknown_or_declared_verified_facts(status, value):
    req = compact_request()
    req.facts["positive"]["business_related"].update(status=status, value=value)
    with pytest.raises(ValueError):
        validate_semantics(normalized(positive_output(req)), req)


@pytest.mark.parametrize("grade", list("ABCD"))
@pytest.mark.parametrize(
    "tier,city", [("I", "深圳"), ("II", "杭州"), ("III", "济南"), ("IV", "合成县")]
)
def test_hotel_context_selects_each_grade_city_without_unrelated_shenzhen_transition(
    grade, tier, city
):
    result = minimal_evidence(
        chunks(),
        kind="HOTEL",
        grade=grade,
        city_tier=tier,
        amount="680",
        department="研发部",
        city=city,
    )
    matrix = [e for e in result if e.source_position.get("table_index") == 5]
    assert len(matrix) == 1 and matrix[0].source_position["cells"][0].startswith(grade + " ")
    transition = grade == "D" and city == "深圳"
    assert any(e.clause_id == "TABLE-6-ROW-2" for e in result) is transition
    assert any(e.clause_id == "第五十五条" for e in result) is transition


@pytest.mark.parametrize("mutation", ["blank_explanation", "blank_exception", "absent_exception"])
def test_blank_or_invented_exception_absence_is_not_valid_review(mutation):
    req = compact_request("HOTEL")
    data = positive_output(req)
    if mutation == "blank_explanation":
        data["explanation"] = " "
    elif mutation == "blank_exception":
        data["exceptions"] = [" "]
    else:
        f = finding(
            req,
            topic="EXCEPTION_REASON",
            basis="EXCEPTION",
            clause="第十三条",
            text="没有申请例外，仍然要求无依据的补充证明。",
        )
        data = authored_review(req, [f])
    with pytest.raises(ValueError):
        validate_semantics(normalized(data), req)


def test_v2_positive_stay_is_required_without_changing_legacy_contract():
    args = contract_input()
    args["kind"] = "HOTEL"
    legacy = build_contract(**args)
    args["contract_version"] = "p4b.positive.2"
    updated = build_contract(**args)
    assert legacy.contract_version == "p4b.positive.1" and "stay" not in legacy.required
    assert updated.contract_version == "p4b.positive.2" and "stay" in updated.required
    assert "stay" in updated.missing


@pytest.mark.parametrize("text", ["明天海上天气晴朗。", "Dividends belong to shareholders."])
def test_completely_unrelated_explanation_cannot_borrow_a_real_evidence_id(text):
    req = compact_request("HOTEL")
    f = finding(req, topic="EVIDENCE_SUFFICIENCY", basis="HOTEL", clause="第十八条", text=text)
    with pytest.raises(ValueError, match="CITATION_ATTRIBUTION_FAILURE"):
        validate_semantics(normalized(authored_review(req, [f])), req)
