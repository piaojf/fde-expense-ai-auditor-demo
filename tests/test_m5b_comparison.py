"""Isolated comparator adversaries; never alter the 72-case Golden Dataset."""

import asyncio
import copy
import csv
import json
import sqlite3
from uuid import uuid4

import pytest

from apps.api.repository import MockClaimRepository
from apps.api.workflow import AuditService
from evals.comparison.comparator import compare_case
from evals.comparison.report import render_report
from evals.comparison.runner import compare_run, main, read_shadow_store, write_outputs
from evals.dataset import DEFAULT_DATASET, ROOT, load_dataset
from evals.metrics import summarize
from evals.runner import sha256
from evals.shadow_mode.runner import run_shadow
from evals.shadow_mode.store import ShadowStore
from integrations.erp.mock import MockERP


@pytest.fixture(scope="module")
def evidence(tmp_path_factory):
    path = tmp_path_factory.mktemp("comparison") / "shadow.sqlite3"
    shadow = asyncio.run(run_shadow(store_path=path))
    return shadow, path


@pytest.fixture
def shadow(evidence):
    return copy.deepcopy(evidence[0])


def case_and_result(shadow, case_id):
    case = next(c for c in load_dataset().cases if c.case_id == case_id)
    result = next(r for r in shadow["cases"] if r["case_id"] == case_id)
    return case, result


def compare(shadow, case, result):
    manifest = shadow["manifest"]
    return compare_case(
        case,
        result,
        dataset_version=manifest["dataset_version"],
        policy_sha256=manifest["policy_fixture_sha256"],
        prompt_version=manifest["prompt_version"],
        run_id=shadow["shadow_run_id"],
    )


def set_actual(result, decision=None, risk=None, policy=None):
    actual = result["evaluation_row"]["actual"]
    if decision:
        result["agent_decision"] = actual["decision"] = decision
        result["requires_human"] = actual["requires_human"] = decision == "HUMAN_REVIEW"
    if risk:
        result["agent_risk"] = actual["risk"] = risk
    if policy is not None:
        actual["policy_evidence"] = policy
        actual["authoritative_version_id"] = policy[0] if policy else None
        result["policy_version"] = actual["authoritative_version_id"]


@pytest.mark.parametrize(
    "case_id,decision,risk,policy,category,severity",
    [
        ("hard_block-01", "APPROVE", "LOW", None, "FALSE_APPROVAL", "CRITICAL"),
        ("compliant-01", "REJECT", "HIGH", None, "FALSE_REJECTION", "MEDIUM"),
        ("special_stay-01", "APPROVE", "LOW", None, "MISSED_HUMAN_REVIEW", "MEDIUM"),
        ("compliant-01", "HUMAN_REVIEW", "MEDIUM", None, "UNNECESSARY_HUMAN_REVIEW", "LOW"),
        ("hard_block-01", None, None, ["POL-SZ-2025"], "POLICY_MISMATCH", "HIGH"),
        ("compliant-01", None, "MEDIUM", None, "RISK_LEVEL_MISMATCH", "LOW"),
    ],
)
def test_required_isolated_mismatch_fixtures(
    shadow, case_id, decision, risk, policy, category, severity
):
    case, result = case_and_result(shadow, case_id)
    set_actual(result, decision, risk, policy)
    outcome = compare(shadow, case, result)
    assert outcome["agent_result_valid"]
    assert category in {d["category"] for d in outcome["disagreements"]}
    assert outcome["severity"] == severity
    assert outcome["match_status"] == "MISMATCH"
    assert outcome["reason"]


def test_missing_result_is_seventh_required_fixture(shadow):
    case, _ = case_and_result(shadow, "compliant-01")
    outcome = compare(shadow, case, None)
    assert outcome["disagreements"] == [
        {
            "category": "EVALUATION_ERROR",
            "severity": "HIGH",
            "reason": "AGENT_RESULT_MISSING",
        }
    ]
    assert not any(outcome[k] for k in ("decision_match", "risk_match", "policy_match"))


@pytest.mark.parametrize("decision,risk", [("APPROVE", "LOW"), ("REJECT", "HIGH")])
def test_human_reference_is_not_rejection(shadow, decision, risk):
    case, result = case_and_result(shadow, "special_stay-01")
    assert case.expected_decision.value == "HUMAN_REVIEW"
    set_actual(result, decision, risk)
    outcome = compare(shadow, case, result)
    categories = {d["category"] for d in outcome["disagreements"]}
    assert "MISSED_HUMAN_REVIEW" in categories
    assert "FALSE_REJECTION" not in categories
    assert ("FALSE_APPROVAL" in categories) == (decision == "APPROVE")
    metrics = compare_run(shadow)["metrics"]
    assert metrics["false_rejection_rate"]["numerator"] == 0


def test_hard_block_review_and_unblocked_wrong_approval_severity(shadow):
    case, result = case_and_result(shadow, "hard_block-01")
    set_actual(result, "HUMAN_REVIEW", "MEDIUM")
    assert compare(shadow, case, result)["severity"] == "HIGH"
    # A valid non-Hard-Block reference REJECT would be HIGH rather than CRITICAL.
    case = case.model_copy(update={"hard_block": False})
    result["evaluation_row"]["expected"]["hard_block"] = False
    result["evaluation_row"]["actual"]["hard_block"] = False
    for r in result["evaluation_row"]["actual"]["rule_results"]:
        r["outcome"] = "PASS"
    for g in result["evaluation_row"]["actual"]["hard_gate_results"]:
        g["outcome"] = "PASS"
    set_actual(result, "APPROVE", "LOW")
    assert compare(shadow, case, result)["severity"] == "HIGH"


def test_empty_policy_is_explicit_and_missing_is_invalid(shadow):
    case, result = case_and_result(shadow, "no_valid_policy-01")
    assert compare(shadow, case, result)["policy_match"] is True
    del result["evaluation_row"]["actual"]["policy_evidence"]
    outcome = compare(shadow, case, result)
    assert outcome["agent_result_valid"] is False
    assert outcome["policy_match"] is False


def test_authority_mismatch_with_matching_evidence_is_detected(shadow):
    case, result = case_and_result(shadow, "compliant-01")
    result["evaluation_row"]["actual"]["authoritative_version_id"] = "POL-SZ-2025"
    result["policy_version"] = "POL-SZ-2025"
    outcome = compare(shadow, case, result)
    assert outcome["decision_match"]
    assert not outcome["policy_match"]
    assert outcome["severity"] == "HIGH"


@pytest.mark.parametrize("mode", ["absent", "expert", "historical", "label", "policy"])
def test_reference_unavailable_and_provenance_conflict(shadow, mode):
    case, result = case_and_result(shadow, "compliant-01")
    if mode == "absent":
        case = None
    elif mode in ("expert", "historical"):
        result["reference_source"] = (
            "EXPERT_REVIEWED" if mode == "expert" else "HISTORICAL_HUMAN_DECISION"
        )
    elif mode == "label":
        result["evaluation_row"]["expected"]["risk"] = "HIGH"
    else:
        result["evaluation_row"]["applicable_policy"]["version"] = "2025"
    outcome = compare(shadow, case, result)
    assert outcome["reference_available"] is False
    assert outcome["match_status"] == "REFERENCE_UNAVAILABLE"
    assert outcome["disagreements"][0]["category"] == "REFERENCE_UNAVAILABLE"
    assert not any(outcome[k] for k in ("decision_match", "risk_match", "policy_match"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("dataset_version", "old"),
        ("policy_fixture_sha256", "old"),
        ("shadow_run_id", str(uuid4())),
        ("case_id", "other"),
        ("agent_decision", "REJECT"),
        ("agent_risk", "HIGH"),
        ("requires_human", True),
        ("policy_version", "POL-SZ-2025"),
        ("prompt_version", "other"),
        ("model_provider", "deepseek"),
        ("execution_status", "FAILED"),
        ("latency_ms", -1),
    ],
)
def test_invalid_versions_and_projections_are_not_matches(shadow, field, value):
    case, result = case_and_result(shadow, "compliant-01")
    result[field] = value
    outcome = compare(shadow, case, result)
    assert not outcome["agent_result_valid"]
    assert not outcome["decision_match"]
    assert outcome["disagreements"][0]["category"] == "EVALUATION_ERROR"
    # Invalid records must not crash M5A aggregation, even with malformed latency.
    if field != "case_id":
        assert compare_run(shadow)["comparison_status"] == "FAIL"


@pytest.mark.parametrize("mode", ["duplicate", "foreign", "selection", "dataset", "source"])
def test_run_identity_and_provenance_rejected(shadow, mode):
    if mode == "duplicate":
        shadow["cases"].append(shadow["cases"][0])
    elif mode == "foreign":
        shadow["cases"][0]["case_id"] = "foreign"
    elif mode == "selection":
        shadow["manifest"]["case_ids"].append("foreign")
    elif mode == "dataset":
        shadow["manifest"]["dataset_version"] = "old"
    else:
        shadow["reference_source"] = "EXPERT_REVIEWED"
    with pytest.raises(ValueError):
        compare_run(shadow)


def test_full_golden_run_reuses_m5a_without_fixture_contamination(shadow):
    before = json.dumps(shadow, sort_keys=True)
    dataset_hash = sha256(DEFAULT_DATASET)
    outcome = compare_run(shadow)
    assert outcome["comparison_status"] == "PASS"
    assert outcome["selected_case_count"] == outcome["persisted_case_count"] == 72
    for value in outcome["agreement"].values():
        assert value == {"numerator": 72, "denominator": 72, "value": 1.0}
    assert outcome["metrics"] == summarize([c["evaluation_row"] for c in shadow["cases"]])
    assert outcome["total_disagreements"] == outcome["coverage"]["evaluation_failure_count"] == 4
    assert outcome["business_disagreements"] == 0
    assert outcome["disagreement_severities"] == {
        "CRITICAL": 0,
        "HIGH": 0,
        "MEDIUM": 4,
        "LOW": 0,
    }
    assert outcome["coverage"]["valid_agent_output"]["numerator"] == 72
    assert outcome["coverage"]["successful_dependency_execution"]["numerator"] == 68
    assert outcome["metrics"]["false_approval_unsafe_opportunity"]["denominator"] == 57
    assert outcome["governance_certification"] == "PASS"
    assert json.dumps(shadow, sort_keys=True) == before
    assert sha256(DEFAULT_DATASET) == dataset_hash
    assert all(c["match_status"] == "MATCH_WITH_EVALUATION_ERROR" for c in outcome["cases"][:4])
    assert all(c["reason"] for c in outcome["cases"])
    assert outcome["cases"][0]["dependency_diagnostics"] == [
        {"node": "retrieve_policy", "error_type": "TimeoutError"},
    ]


def test_missing_output_keeps_all_denominators_and_fails_isolation(shadow):
    shadow["cases"] = [c for c in shadow["cases"] if c["case_id"] != "compliant-01"]
    outcome = compare_run(shadow)
    assert outcome["agreement"]["decision"] == {
        "numerator": 71,
        "denominator": 72,
        "value": 71 / 72,
    }
    assert outcome["metrics"]["false_approval_unsafe_opportunity"]["denominator"] == 57
    assert outcome["metrics"]["false_rejection_rate"]["denominator"] == 15
    assert outcome["metrics"]["automation_rate"] == {
        "numerator": 38,
        "denominator": 72,
        "value": 38 / 72,
    }
    assert outcome["metrics"]["confusion_matrix"]["APPROVE"]["ERROR"] == 1
    assert outcome["coverage"]["evaluation_failure_count"] == 5
    assert outcome["side_effect_isolation"] == "NOT_CERTIFIED"
    assert all(v is None for v in outcome["side_effects"].values())
    assert outcome["comparison_status"] == "FAIL"


def test_invalid_output_never_inflates_accuracy(shadow):
    _, result = case_and_result(shadow, "compliant-01")
    result["evaluation_row"]["decision_schema_valid"] = False
    assert compare_run(shadow)["metrics"]["decision_accuracy"]["numerator"] == 71


def test_business_fixture_metrics_and_severity_sort(shadow):
    for case_id, decision, risk in (
        ("hard_block-01", "APPROVE", "LOW"),
        ("compliant-01", "REJECT", "HIGH"),
    ):
        _, result = case_and_result(shadow, case_id)
        set_actual(result, decision, risk)
    outcome = compare_run(shadow)
    assert outcome["comparison_status"] == "FAIL"
    assert outcome["total_disagreements"] == 6
    assert outcome["business_disagreements"] == 2
    assert outcome["cases"][0]["severity"] == "CRITICAL"
    assert outcome["metrics"]["hard_block_violations"] == 1
    assert outcome["metrics"]["false_approval_unsafe_opportunity"] == {
        "numerator": 1,
        "denominator": 57,
        "value": 1 / 57,
    }
    assert outcome["metrics"]["false_rejection_rate"]["numerator"] == 1


@pytest.mark.parametrize(
    "key", ["erp_write_count", "claim_mutation_count", "human_review_action_count"]
)
def test_nonzero_side_effect_cannot_pass(shadow, key):
    shadow["cases"][0]["side_effects"][key] = 1
    outcome = compare_run(shadow)
    assert outcome["comparison_status"] == "FAIL"
    assert outcome["side_effect_isolation"] == "NOT_CERTIFIED"
    assert outcome["side_effects"][key] == 1


def test_formal_operations_and_source_store_are_untouched(evidence, tmp_path, monkeypatch):
    shadow, path = evidence
    formal = MockClaimRepository(store_path=str(tmp_path / "formal.sqlite3"))
    before = list(formal._db.iterdump())
    hashes = {p: sha256(p) for p in (path, DEFAULT_DATASET, ROOT / "evals/metrics/__init__.py")}
    calls = []

    def trap(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Comparator reached formal operation")

    for method in ("__init__", "write_erp", "save_operation", "append_events"):
        monkeypatch.setattr(MockClaimRepository, method, trap)
    for method in ("audit", "persist", "human_review", "resume"):
        monkeypatch.setattr(AuditService, method, trap)
    monkeypatch.setattr(MockERP, "__init__", trap)
    monkeypatch.setattr(MockERP, "write_decision", trap)
    monkeypatch.setattr(ShadowStore, "__init__", trap)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "COMPARISON_SECRET_SENTINEL")
    try:
        result = compare_run(read_shadow_store(path, shadow["shadow_run_id"]))
        write_outputs(
            result, tmp_path / "result.json", tmp_path / "report.md", tmp_path / "rows.csv"
        )
        assert result["side_effect_isolation"] == "PASS"
        assert set(result["side_effects"].values()) == {0}
        assert not calls
        assert list(formal._db.iterdump()) == before
        assert hashes == {p: sha256(p) for p in hashes}
        assert "COMPARISON_SECRET_SENTINEL" not in (tmp_path / "result.json").read_text(
            encoding="utf-8"
        )
        assert result["cases"] == compare_run(shadow)["cases"]
    finally:
        formal.close()


def test_missing_or_business_store_is_not_created_or_changed(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        read_shadow_store(missing, str(uuid4()))
    assert not missing.exists()
    path = tmp_path / "formal.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE claims(id TEXT)")
    before = sha256(path)
    with pytest.raises(ValueError):
        read_shadow_store(path, str(uuid4()))
    assert sha256(path) == before


def test_partial_store_is_reported_with_missing_cases(evidence, tmp_path):
    shadow, _ = evidence
    path = tmp_path / "partial.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE shadow_runs(run_id TEXT,manifest TEXT,summary TEXT)")
        db.execute("CREATE TABLE shadow_results(run_id TEXT,case_id TEXT,body TEXT)")
        db.execute(
            "INSERT INTO shadow_runs VALUES(?,?,?)",
            (
                shadow["shadow_run_id"],
                json.dumps(shadow["manifest"]),
                json.dumps({"status": "RUNNING"}),
            ),
        )
        c = shadow["cases"][0]
        db.execute(
            "INSERT INTO shadow_results VALUES(?,?,?)",
            (shadow["shadow_run_id"], c["case_id"], json.dumps(c)),
        )
    outcome = compare_run(read_shadow_store(path, shadow["shadow_run_id"]))
    assert outcome["selected_case_count"] == 72
    assert outcome["coverage"]["unavailable_agent_results"] == 71
    assert outcome["comparison_status"] == "FAIL"


def test_cli_report_csv_and_no_overwrite(shadow, tmp_path):
    source = tmp_path / "source.json"
    source.write_text(json.dumps(shadow), encoding="utf-8")
    args = [
        "--input",
        str(source),
        "--output",
        str(tmp_path / "result.json"),
        "--report",
        str(tmp_path / "report.md"),
        "--csv",
        str(tmp_path / "rows.csv"),
    ]
    before = sha256(source)
    assert main(args) == 0
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "Synthetic + Mock" in report and "EXPERT_REVIEWED" in report
    assert "72/72 = 100.00%" in report and "68/72 = 94.44%" in report
    assert "EVALUATION_ERROR" in report and "STOP" in report
    with (tmp_path / "rows.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 72 and rows[0]["severity"] == "MEDIUM"
    assert b"\r" not in (tmp_path / "rows.csv").read_bytes()
    hashes = {p: sha256(p) for p in tmp_path.iterdir()}
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2
    assert hashes == {p: sha256(p) for p in hashes}
    assert sha256(source) == before
    with pytest.raises(ValueError):
        write_outputs(result, tmp_path / "same.json", tmp_path / "same.json")
    assert not (tmp_path / "same.json").exists()
    # Missing records yield exit 1 with an honest failure report.
    shadow["cases"].pop()
    source.write_text(json.dumps(shadow), encoding="utf-8")
    assert main(["--input", str(source), "--output", str(tmp_path / "failure.json")]) == 1


def test_zero_denominator_is_undefined_and_subset_not_governance_certified(shadow):
    _, result = case_and_result(shadow, "hard_block-01")
    shadow["manifest"]["case_ids"] = ["hard_block-01"]
    shadow["selected_case_count"] = 1
    shadow["cases"] = [result]
    outcome = compare_run(shadow)
    assert outcome["metrics"]["false_rejection_rate"]["value"] is None
    assert outcome["governance_certification"] == "NOT_CERTIFIED"
    assert "undefined" in render_report(outcome, "result.json")


@pytest.mark.parametrize(
    "field,value",
    [
        ("decision", []),
        ("risk", {}),
        ("authoritative_version_id", []),
        ("errors", [{}]),
        ("rule_results", None),
        ("hard_gate_results", "invalid"),
    ],
)
def test_malformed_output_is_an_error_not_a_crash(shadow, field, value):
    case, result = case_and_result(shadow, "compliant-01")
    result["evaluation_row"]["actual"][field] = value
    assert compare(shadow, case, result)["disagreements"][0]["category"] == "EVALUATION_ERROR"
    assert compare_run(shadow)["comparison_status"] == "FAIL"


def test_unexpected_dependency_failure_cannot_hide_in_fault_fixture(shadow):
    _, result = case_and_result(shadow, "dependency_failure-01")
    result["evaluation_row"]["actual"]["errors"][0]["node"] = "parse_documents"
    outcome = compare_run(shadow)
    assert outcome["comparison_status"] == "FAIL"
    assert outcome["coverage"]["expected_injected_dependency_failures"] == 3
    assert outcome["coverage"]["dependency_failures"] == 4


def test_source_store_failure_not_hidden_by_matching_decisions(shadow):
    shadow["shadow_status"] = "FAIL"
    shadow["evaluation_store_failure"] = "OperationalError"
    outcome = compare_run(shadow)
    assert outcome["comparison_status"] == "FAIL"
    assert outcome["source_store_failure"] == "OperationalError"
    assert outcome["agreement"]["decision"]["value"] == 1


def test_invalid_golden_label_is_rejected_instead_of_relabelled(shadow, tmp_path):
    dataset = json.loads(DEFAULT_DATASET.read_text(encoding="utf-8"))
    del dataset["cases"][0]["expected_decision"]
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(dataset), encoding="utf-8")
    with pytest.raises(ValueError):
        compare_run(shadow, path)


def test_missing_side_effect_counter_is_unknown(shadow):
    del shadow["cases"][0]["side_effects"]["erp_write_count"]
    outcome = compare_run(shadow)
    assert outcome["comparison_status"] == "FAIL"
    assert outcome["side_effects"]["erp_write_count"] is None


def test_observed_hard_block_cannot_hide_behind_projection_flag(shadow):
    case, result = case_and_result(shadow, "compliant-01")
    assert result["evaluation_row"]["actual"]["hard_block"] is False
    rule = result["evaluation_row"]["actual"]["rule_results"][0]
    rule.update(outcome="FAIL", severity="BLOCK")
    compared = compare(shadow, case, result)
    assert compared["decision_match"] is True
    assert compared["severity"] == "CRITICAL"
    assert compared["disagreements"][0]["category"] == "FALSE_APPROVAL"
    outcome = compare_run(shadow)
    assert outcome["metrics"]["hard_block_violations"] == 1
    # Label-based M5A FAR remains unchanged; rule violation is a separate gate.
    assert outcome["metrics"]["false_approval_unsafe_opportunity"]["numerator"] == 0
    assert outcome["comparison_status"] == "FAIL"


@pytest.mark.parametrize("probe", [{"unexpected": 1}, {"old_similarity": "bad"}])
def test_invalid_probe_cannot_crash_governance(shadow, probe):
    _, result = case_and_result(shadow, "policy_version_conflict-05")
    result["evaluation_row"]["probe_result"] = probe
    assert compare_run(shadow)["comparison_status"] == "FAIL"


def test_full_run_cannot_certify_missing_governance_probe(shadow):
    for c in shadow["cases"]:
        c["evaluation_row"]["probe_result"] = None
    outcome = compare_run(shadow)
    assert outcome["governance_certification"] == "NOT_CERTIFIED"
    assert outcome["comparison_status"] == "FAIL"
