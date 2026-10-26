"""Explicit human review of advisory reports within the enterprise security boundary."""

from datetime import UTC, datetime

from fastapi import Request

from apps.api.compliance import fingerprint
from apps.api.enterprise import COOKIE
from apps.api.errors import APIError
from domain.advisory import ManualAdvisoryReview

PREFIX = "/p4b/v2"


def install_advisory_api(app, catalog, service):
    store = catalog.store.enterprise

    def current(cid, user):
        row = store.claim_row(cid, user)
        claim = catalog.repo.get(cid)
        report = catalog.advisory(cid, claim.active_run_id)
        if not report:
            raise APIError(404, "ADVISORY_NOT_FOUND", "当前申请没有新版AI建议报告")
        return row, claim, report

    @app.get(PREFIX + "/claims/{cid}")
    def report(cid: str, request: Request):
        user = store.live_actor(request.cookies.get(COOKIE))
        return current(cid, user)[2]

    @app.post(PREFIX + "/claims/{cid}/manual-review")
    async def manual_review(cid: str, body: ManualAdvisoryReview, request: Request):
        token = request.cookies.get(COOKIE)
        user = store.live_actor(token, "FINANCE")
        generation = store.authority(user["id"])
        async with app.state.work_lock, service.lock:
            with catalog.store.lock, catalog.store.db:
                # Acquire write transaction before final live authorization, matching P1.
                store.db.execute("UPDATE enterprise_users SET id=id WHERE id=?", (user["id"],))
                user = store.live_actor(token, "FINANCE", expected_generation=generation,
                                        company_id=user["company_id"])
                row, claim, report = current(cid, user)
                scope = user["id"] + ":manual-advisory:" + cid
                replay = catalog.store.replay(scope, body.idempotency_key, body.model_dump_json())
                if replay is not None:
                    return replay
                if row["owner_id"] == user["id"]:
                    raise APIError(403, "SELF_REVIEW_FORBIDDEN", "不能核查本人的申请")
                if row["stage"] != "FINANCE_REVIEW" or row["revision"] != body.expected_revision:
                    raise APIError(409, "REVISION_CONFLICT", "申请状态或版本已变化")
                binding = report["binding"]
                if (body.report_digest != report["report_digest"]
                        or binding["claim_version"] != row["version"]
                        or binding["candidate_digest"] != catalog.candidate_digest(cid)
                        or binding["fact_revision"] != catalog.store.read("FACTS", cid)[0]):
                    raise APIError(409, "ADVISORY_STALE", "资料或审核已变化，请重新审核")
                context = catalog.repo.run_context(cid, claim.active_run_id) or {}
                if context.get("human_review") or catalog.repo.erp_write_count(cid):
                    raise APIError(409, "INVALID_TRANSITION", "最终决定已存在")
                record = dict(
                    **body.model_dump(mode="json"), binding=binding, actor_id=user["id"],
                    company_id=user["company_id"], generation=generation,
                    at=datetime.now(UTC).isoformat(),
                )
                key = cid + ":" + str(claim.active_run_id)
                catalog.store.append("MANUAL_ADVISORY", key, record)
                catalog.store.remember(scope, body.idempotency_key, body.model_dump_json(), record)
                store.event(user, "ADVISORY_MANUALLY_REVIEWED", cid,
                            run_id=str(claim.active_run_id), outcome=body.outcome,
                            report_digest=body.report_digest, review_digest=fingerprint(record))
                return record
