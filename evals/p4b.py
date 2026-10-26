"""Offline P4B golden evaluation. Semantic accuracy requires separately authorized live runs."""

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from agent.routing.compliance import aggregate
from domain.compliance import ComplianceRequest
from domain.hotel_matrix import HotelFields
from domain.schemas import Expense, HardGateResult
from integrations.llm.compliance import OfflineComplianceProvider
from integrations.policies.hotel_matrix import extract_matrix
from integrations.policies.parser import PolicyParser
from rag.retrieval.compliance import SOURCE_SHA256, extraction, original_chunks, rank_chunks
from rules.compliance import calculate
from rules.hotel_matrix import calculate as hotel_calculate
from rules.hotel_matrix import frozen_result

ROOT = Path(__file__).resolve().parents[1]


def evaluate():
    raw = (ROOT / "tests/fixtures/policies/p4a-222.docx").read_bytes()
    chunks = original_chunks(
        raw,
        document_id="synthetic-source",
        policy_version_id="V2",
        effective_from=date(2026, 11, 1),
        effective_to=date(2027, 1, 1),
    )
    parser = PolicyParser(SimpleNamespace(binary="", languages="eng", tessdata=None))
    matrix = extract_matrix(raw, parser.parse(raw, "222.docx"))
    policy = SimpleNamespace(
        authoritative_rules=dict(
            rule_type="HOTEL_LIMIT_MATRIX",
            matrix=matrix,
            upload_id="synthetic-source",
            file_sha256=SOURCE_SHA256,
        ),
        policy_version_id="V2",
        source_uri="fixture://222.docx",
        title="222.docx",
        version="2026-11-V2",
    )
    source = dict(policy_version_id="V2", rules=extraction(chunks))
    goldens = json.loads(
        (ROOT / "evals/datasets/p4b/golden_cases.json").read_text(encoding="utf-8")
    )
    rows, cited, decisions, false_releases = [], [], [], 0
    for case in goldens["cases"]:
        evidence = rank_chunks(chunks, case.get("notes", case["source"]), expense_type=case["kind"])
        cited.extend(evidence)
        expense = Expense(
            category="HOTEL" if case["kind"] == "HOTEL" else "OTHER",
            city="深圳",
            incurred_on="2026-11-02",
            amount=case["amount"],
        )
        if case["kind"] == "HOTEL":
            fields = HotelFields(
                applicant_name="合成员工",
                department="合成部门",
                position="员工",
                city="深圳",
                amount=case["amount"],
                incurred_on=expense.incurred_on,
                trip_start=expense.incurred_on,
                trip_end=expense.incurred_on + timedelta(days=1),
                nights=1,
                purpose="合成客户现场工作",
                seller="合成酒店",
            )
            facts = dict(
                employee={"value": {"grade": "D"}},
                city={"value": {"name": "深圳", "tier": "I", "level": "PREFECTURE"}},
            )
            rule = frozen_result(hotel_calculate(fields, facts, policy))
        else:
            rule = calculate(expense, case["kind"], case.get("verified", {}), source, "I", 1)
        req = ComplianceRequest(
            claim_id=case["id"],
            policy_version_id="V2",
            evidence=evidence,
            facts={
                "candidate": {"notes": case.get("notes", "")},
                "verified": case.get("verified", {}),
            },
            rule_results=[rule.model_dump(mode="json")],
        )
        semantic = asyncio.run(OfflineComplianceProvider().analyze(req))
        status, action, *_ = aggregate(
            rules=[rule],
            gates=[HardGateResult(gate_id="synthetic", outcome="PASS", reason="fixture")],
            semantic=semantic,
            missing=[],
            conflicts=[],
            retrieval_valid=True,
        )
        if case.get("expected_decision"):
            decisions.append(action == case["expected_decision"])
        false_releases += int(action == "CHECK_PASSED")
        fragments = case["source"].replace("、", "，").split("，")
        rows.append(
            dict(
                id=case["id"],
                rule=rule.outcome.value,
                limit=str(rule.limit) if rule.limit is not None else None,
                rule_match=rule.outcome.value == case["expected_rule"]
                and ("expected_limit" not in case or str(rule.limit) == case["expected_limit"]),
                retrieval_hit=all(
                    any(f in c.section or f == c.clause_id for c in evidence) for f in fragments
                ),
                offline_status=status,
                offline_action=action,
            )
        )
    originals = {c.chunk_id: c for c in chunks}
    valid = sum(
        c == originals.get(c.chunk_id) and c.document_sha256 == SOURCE_SHA256 for c in cited
    )
    return dict(
        scope="manually reviewed synthetic golden / offline only",
        cases=rows,
        retrieval_clause_hit_rate=sum(r["retrieval_hit"] for r in rows) / len(rows),
        citation_source_valid_rate=valid / len(cited),
        rule_consistency=sum(r["rule_match"] for r in rows) / len(rows),
        declared_hard_decision_consistency=sum(decisions) / len(decisions),
        false_automatic_releases=false_releases,
        model_failure_degradation="UNKNOWN / HUMAN_REVIEW; never CHECK_PASSED",
        semantic_risk_accuracy="NOT_RUN",
        missing_evidence_accuracy="NOT_RUN",
        real_llm_requests=0,
        real_embedding_requests=0,
    )


if __name__ == "__main__":
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))
