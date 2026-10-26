"""Real Uvicorn process restart, original-checkpoint resume and durable replay acceptance."""

import argparse
import json
import os
import sqlite3
import urllib.error
from pathlib import Path
from uuid import uuid4

from apps.api.repository import MockClaimRepository
from scripts.smoke_m1 import request, start, stop

C4, C5 = "CLM-2026-00004", "CLM-2026-00005"
PRE = [
    "CLAIM_LOADED",
    "ERP_CONTEXT_FETCHED",
    "DOCUMENTS_PARSED",
    "HARD_GATE_CHECKED",
    "RULES_CHECKED",
    "POLICY_RETRIEVED",
    "LLM_ANALYSIS_COMPLETED",
    "RISK_ASSESSED",
    "DECISION_MADE",
    "HUMAN_REVIEW_REQUIRED",
    "WORKFLOW_INTERRUPTED",
]


def stored(store, claim, run):
    repo = MockClaimRepository(store_path=str(store))
    try:
        return {
            "context": repo.run_context(claim, run),
            "erp": repo.erp_status(claim),
            "erp_write_count": repo.erp_write_count(claim),
            "events": [event.model_dump(mode="json") for event in repo.events(claim)],
        }
    finally:
        repo.close()


def checkpoint(store, context):
    with sqlite3.connect(str(store) + ".checkpoints.sqlite3") as connection:
        rows = connection.execute(
            "SELECT checkpoint_id, parent_checkpoint_id FROM checkpoints WHERE thread_id=?",
            (context["thread_id"],),
        ).fetchall()
        channels = connection.execute(
            "SELECT channel FROM writes WHERE thread_id=? AND checkpoint_id=?",
            (context["thread_id"], context["checkpoint_id"]),
        ).fetchall()
    assert "__interrupt__" in {row[0] for row in channels}
    parents = dict(rows)
    current = context.get("completed_checkpoint_id", context["checkpoint_id"])
    ancestry = []
    while current:
        ancestry.append(current)
        current = parents[current]
    assert context["checkpoint_id"] in ancestry
    return {
        "checkpoint_id": context["checkpoint_id"],
        "ancestry": ancestry,
        "persisted_interrupt_channel": True,
        "checkpoint_count": len(rows),
    }


def expected_error(base, path, body, status, error):
    try:
        request(base, path, body)
    except urllib.error.HTTPError as exc:
        response = json.load(exc)
        assert exc.code == status and response["error"]["code"] == error
        return {"status": status, "body": response}
    raise AssertionError("Request unexpectedly succeeded")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    runtime = args.output.parent / "http-runs" / str(uuid4())
    runtime.mkdir(parents=True)
    store = runtime / "mock.sqlite3"
    environment = {
        **os.environ,
        "APP_ENV": "development",
        "LLM_PROVIDER": "mock",
        "MOCK_STORE_PATH": str(store),
    }
    report = {"store": str(store), "cases": {}, "processes": []}
    process, base = start(root, environment)
    report["processes"].append(process.pid)
    print("UVICORN_START=PASS")
    try:
        _, schema = request(base, "/openapi.json")
        assert schema == json.loads((root / "docs/openapi.json").read_text(encoding="utf-8"))
        report["contract_unchanged"] = True
        for claim in [C4, C5]:
            code, operation = request(
                base, f"/claims/{claim}/audit", {"idempotency_key": "m2-http-audit", "mode": "LIVE"}
            )
            assert code == 202 and operation["status"] == "PENDING_HUMAN"
            result = operation["decision_result"]
            assert result["hard_gate_passed"] and result["requires_human"]
            assert result["decision"] == "HUMAN_REVIEW" and result["risk_level"] == "MEDIUM"
            assert result["rule_results"][0]["outcome"] == "PASS"
            assert result["rule_results"][0]["actual"] == "680.00"
            assert result["rule_results"][0]["limit"] == "700.00"
            assert [p["version"] for p in result["policy_evidence"]] == ["2026"]
            state = stored(store, claim, operation["run_id"])
            assert state["erp"] is None and state["erp_write_count"] == 0
            assert [e["event_type"] for e in state["events"]] == PRE
            hard = next(e for e in state["events"] if e["event_type"] == "HARD_GATE_CHECKED")
            assert all(g["outcome"] == "PASS" for g in hard["payload"]["hard_gate_results"])
            analysis = next(
                e for e in state["events"] if e["event_type"] == "LLM_ANALYSIS_COMPLETED"
            )
            assert (
                "STAY_OUTSIDE_APPROVED_TRIP_DATES" in analysis["payload"]["analysis"]["anomalies"]
            )
            report["cases"][claim] = {
                "audit": operation,
                "before_restart": state,
                "initial_checkpoint": checkpoint(store, state["context"]),
            }
            print(
                f"{claim}: AUDIT=202 PENDING_HUMAN; RULE=PASS 680<=700; "
                "POLICY=2026; RISK=MEDIUM; REAL_INTERRUPT=YES; ERP_WRITES=0"
            )
        initial = report["cases"][C4]["audit"]
        report["resume_before_review"] = expected_error(
            base,
            f"/claims/{C4}/resume",
            {"run_id": initial["run_id"], "expected_revision": 1, "idempotency_key": "early"},
            409,
            "INVALID_TRANSITION",
        )
        _, blocked = request(
            base, "/claims/CLM-2026-001/audit", {"idempotency_key": "m2-governance-block"}
        )
        assert blocked["decision_result"]["decision"] == "REJECT"
        assert blocked["decision_result"]["risk_level"] == "HIGH"
        report["block_approve_rejected"] = expected_error(
            base,
            "/claims/CLM-2026-001/human-review",
            {
                "run_id": blocked["run_id"],
                "expected_revision": blocked["revision"],
                "decision": "APPROVE",
                "reviewer_id": "finance_reviewer_01",
                "comment": "Attempted hard block override",
                "idempotency_key": "blocked-review",
            },
            409,
            "INVALID_TRANSITION",
        )
        assert stored(store, "CLM-2026-001", blocked["run_id"])["erp_write_count"] == 0
        print("GOVERNANCE_BLOCK_HUMAN_APPROVE=HTTP409; OVERRIDE=NO")
    finally:
        stop(process)
        print("UVICORN_STOP_AFTER_INTERRUPT=PASS")
    process, base = start(root, environment)
    report["processes"].append(process.pid)
    assert report["processes"][0] != process.pid
    try:
        for claim, action, erp in [(C4, "APPROVE", "APPROVED"), (C5, "REJECT", "REJECTED")]:
            case = report["cases"][claim]
            operation = case["audit"]
            _, detail = request(base, f"/claims/{claim}")
            assert detail["status"] == "PENDING_HUMAN"
            state = stored(store, claim, operation["run_id"])
            assert state == case["before_restart"]
            assert checkpoint(store, state["context"]) == case["initial_checkpoint"]
            print(f"{claim}: RESTART_PERSISTENCE=PASS; ORIGINAL_CHECKPOINT=PRESERVED")
            review_body = {
                "run_id": operation["run_id"],
                "expected_revision": 1,
                "decision": action,
                "reviewer_id": "finance_reviewer_01",
                "idempotency_key": "m2-http-review",
                "comment": "航班取消材料有效，额外住宿具有合理业务原因，批准报销。"
                if action == "APPROVE"
                else "所提交证明无法确认该住宿与航班取消直接相关，拒绝此次异常费用。",
            }
            code, reviewed = request(base, f"/claims/{claim}/human-review", review_body)
            assert code == 200 and reviewed["status"] == "PENDING_HUMAN"
            assert reviewed["revision"] == 2
            assert stored(store, claim, operation["run_id"])["erp_write_count"] == 0
            assert request(base, f"/claims/{claim}/human-review", review_body)[1] == reviewed
            resume_body = {
                "run_id": operation["run_id"],
                "expected_revision": 2,
                "idempotency_key": "m2-http-resume",
            }
            code, resumed = request(base, f"/claims/{claim}/resume", resume_body)
            assert code == 202 and resumed["status"] == "COMPLETED"
            assert resumed["run_id"] == operation["run_id"] and resumed["revision"] == 3
            assert resumed["decision_result"]["decision"] == action
            _, detail = request(base, f"/claims/{claim}")
            _, log = request(base, f"/claims/{claim}/audit-log")
            final = stored(store, claim, operation["run_id"])
            assert detail["status"] == "COMPLETED" and detail["erp_status"] == "SUCCEEDED"
            assert final["erp"] == erp and final["erp_write_count"] == 1
            assert log["total"] == 16
            assert [e["event_type"] for e in log["items"]] == PRE + [
                "HUMAN_REVIEW_SUBMITTED",
                "WORKFLOW_RESUMED",
                "HUMAN_APPROVED" if action == "APPROVE" else "HUMAN_REJECTED",
                "ERP_UPDATED",
                "WORKFLOW_COMPLETED",
            ]
            assert log["items"][:11] == case["before_restart"]["events"]
            original_checkpoint = case["initial_checkpoint"]["checkpoint_id"]
            assert final["context"]["resume_context"]["from_checkpoint_id"] == original_checkpoint
            ancestry = checkpoint(store, final["context"])
            assert ancestry["ancestry"][0] != original_checkpoint
            assert request(base, f"/claims/{claim}/resume", resume_body)[1] == resumed
            assert request(base, f"/claims/{claim}/human-review", review_body)[1] == reviewed
            assert stored(store, claim, operation["run_id"]) == final
            case.update(
                review_body=review_body,
                review=reviewed,
                resume_body=resume_body,
                resume=resumed,
                detail=detail,
                audit_log=log,
                final=final,
                completed_checkpoint=ancestry,
                restart_persistence=True,
                duplicate_review=True,
                duplicate_resume=True,
            )
            print(
                f"{claim}: HUMAN={action}; RESUME=202 SAME_WORKFLOW; ERP={erp}; "
                "STATUS=COMPLETED; AUDIT_EVENTS=16; ERP_WRITES=1; DUPLICATES=PASS"
            )
    finally:
        stop(process)
        print("UVICORN_STOP_AFTER_RESUME=PASS")
    process, base = start(root, environment)
    report["processes"].append(process.pid)
    try:
        for claim, case in report["cases"].items():
            assert (
                request(base, f"/claims/{claim}/resume", case["resume_body"])[1] == case["resume"]
            )
            assert (
                request(base, f"/claims/{claim}/human-review", case["review_body"])[1]
                == case["review"]
            )
            assert stored(store, claim, case["audit"]["run_id"]) == case["final"]
        print("POST_COMPLETION_RESTART_REPLAYS=PASS; ERP_WRITES_PER_CASE=1")
    finally:
        stop(process)
        print("UVICORN_FINAL_STOP=PASS")
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("M2_HTTP=PASS; REAL_PGVECTOR_VERIFIED=false; REAL_LLM_VERIFIED=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
