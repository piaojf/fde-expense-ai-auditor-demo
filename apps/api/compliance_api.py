"""Independent P4B v1 routes inside the existing authenticated enterprise boundary."""

import json
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

from fastapi import BackgroundTasks, Request

from apps.api.compliance import HybridCatalog
from apps.api.enterprise import COOKIE
from apps.api.enterprise_store import timestamp
from apps.api.errors import APIError
from domain.compliance import ConfirmScope, HybridInvoice, HybridSubmission, Reaudit, VerifyFacts
from domain.schemas import ClaimDetail, ClaimStatus, Document, Employee, ERPStatus, Expense
from integrations.llm.compliance_v2 import CompactComplianceDeepSeekProvider

PREFIX = "/p4b/v1"


def install_compliance_api(app, settings, repo, service, provider=None, business_clock=None):
    enterprise = app.state.store
    catalog_type = HybridCatalog
    if settings.p4b_advisory_enabled:
        from apps.api.advisory import AdvisoryCatalog
        from integrations.llm.advisory import AdvisoryDeepSeekProvider
        from integrations.llm.advisory_v2 import AdvisoryDeepSeekProviderV2
        from integrations.llm.advisory_v3 import AdvisoryDeepSeekProviderV3
        from integrations.llm.advisory_v4 import AdvisoryDeepSeekProviderV4

        advisory_provider = {"p4b.advisory.1": AdvisoryDeepSeekProvider,
                             "p4b.advisory.2": AdvisoryDeepSeekProviderV2,
                             "p4b.advisory.3": AdvisoryDeepSeekProviderV3,
                             "p4b.advisory.4": AdvisoryDeepSeekProviderV4}[
                                 settings.p4b_advisory_protocol]

        catalog_type = AdvisoryCatalog
    if settings.p4b_live_llm:
        if provider is not None or settings.llm_provider != "deepseek":
            raise ValueError(
                "P4B live semantics requires an explicitly configured DeepSeek provider"
            )
        provider_type = (advisory_provider if settings.p4b_advisory_enabled
                         else CompactComplianceDeepSeekProvider)
        provider = provider_type(
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
            base_url=settings.deepseek_base_url,
            timeout_seconds=settings.deepseek_timeout_seconds,
            max_tokens=settings.p4b_max_tokens,
        )
        if settings.p4b_live_budget_path is not None:
            from integrations.llm.budgeted import BudgetedLLM

            provider = BudgetedLLM(
                provider, settings.p4b_live_budget_path, settings.p4b_live_max_requests,
            )
    hybrid = catalog_type(service.catalog, enterprise, repo, provider)
    service.catalog = hybrid
    service.graph_factory.keywords["catalog"] = hybrid
    app.state.compliance = hybrid
    if settings.p4b_advisory_enabled:
        from apps.api.advisory_api import install_advisory_api

        install_advisory_api(app, hybrid, service)

    def actor(request, *roles):
        return enterprise.live_actor(request.cookies.get(COOKIE), *roles)

    def own(cid, user):
        enterprise.claim_row(cid, user)
        if not hybrid.is_hybrid(cid):
            raise APIError(404, "P4B_CLAIM_NOT_FOUND", "此申请未使用混合审核")

    @app.get(PREFIX + "/runtime")
    def runtime(request: Request):
        actor(request)
        return dict(
            schema_version=getattr(
                hybrid.provider, "protocol_version", settings.p4b_advisory_protocol)
            if settings.p4b_advisory_enabled else "p4b.1",
            live_llm=settings.p4b_live_llm,
            semantic_model=hybrid.provider.model,
            retrieval="LOCAL_TEXT_ORIGINAL_DOCX",
            real_semantic_retrieval="NOT_RUN",
        )

    @app.get(PREFIX + "/policies/{pid}")
    def preview(pid: str, request: Request):
        user = actor(request, "ADMIN")
        source = hybrid.scope_source(pid, user["company_id"])
        return dict(
            policy_version_id=pid,
            title=source["title"],
            version=source["version"],
            rules=source["rules"],
            evidence=[c.model_dump(mode="json") for c in source["chunks"]],
        )

    @app.post(PREFIX + "/policies/{pid}/confirm-scope")
    async def confirm_scope(pid: str, body: ConfirmScope, request: Request):
        user = actor(request, "ADMIN")
        generation = enterprise.authority(user["id"])
        async with app.state.work_lock:
            source = hybrid.scope_source(pid, user["company_id"])
            if body.extraction_digest != source["rules"]["extraction_digest"]:
                raise APIError(409, "EXTRACTION_CHANGED", "请重新核对原文和规则摘要")
            key = user["company_id"] + ":" + pid
            scope = user["id"] + ":p4b-scope:" + pid
            with hybrid.store.lock, hybrid.store.db:
                hybrid.store.db.execute(
                    "UPDATE enterprise_users SET id=id WHERE id=?", (user["id"],)
                )
                user = enterprise.live_actor(request.cookies.get(COOKIE), "ADMIN",
                                             expected_generation=generation,
                                             company_id=user["company_id"])
                replay = hybrid.store.replay(scope, body.idempotency_key, body.model_dump_json())
                if replay is not None:
                    return replay
                if hybrid.store.read("SCOPE", key)[1]:
                    raise APIError(409, "P4B_SCOPE_ALREADY_CONFIRMED", "该不可变范围已确认")
                record = dict(
                    policy_version_id=pid,
                    actor_id=user["id"],
                    extraction_digest=body.extraction_digest,
                    at=datetime.now(UTC).isoformat(),
                    reason=body.reason,
                    scope=[
                        "SEMANTIC_BODY_CHAPTERS_1_17",
                        "FULL_DAY_ALLOWANCE",
                        "GENERAL_CUSTOMER_ENTERTAINMENT",
                    ],
                    rules=source["rules"],
                )
                hybrid.store.append("SCOPE", key, record)
                hybrid.store.remember(scope, body.idempotency_key, body.model_dump_json(), record)
                enterprise.event(
                    user,
                    "P4B_SCOPE_CONFIRMED",
                    policy_version_id=pid,
                    extraction_digest=body.extraction_digest,
                    reason=body.reason,
                )
            return record

    @app.get(PREFIX + "/claims/{cid}")
    def report(cid: str, request: Request):
        user = actor(request)
        own(cid, user)
        claim = repo.get(cid)
        return dict(
            analysis=hybrid.report(cid, claim.active_run_id),
            fact_revision=hybrid.store.read("FACTS", cid)[0],
            verification=hybrid.store.read("FACTS", cid)[1],
        )

    @app.get(PREFIX + "/claims/{cid}/evidence/{chunk_id}")
    def evidence(cid: str, chunk_id: str, request: Request):
        user = actor(request)
        own(cid, user)
        claim = repo.get(cid)
        current = hybrid.report(cid, claim.active_run_id)
        if not current:
            raise APIError(404, "P4B_REPORT_NOT_FOUND", "审核报告不存在")
        chosen = next(
            (c for c in current["supporting_evidence"] if c["chunk_id"] == chunk_id), None
        )
        if chosen is None:
            raise APIError(404, "P4B_EVIDENCE_NOT_FOUND", "引用不存在")
        context = repo.run_context(cid, claim.active_run_id)
        with_source = hybrid.retriever.read(
            company=user["company_id"],
            city=claim.expense.city,
            incurred_on=claim.expense.incurred_on,
            started=datetime.fromisoformat(context["p4b_started_at"]),
            expected_id=current["policy_version_id"],
        )
        actual = next(
            (c.model_dump(mode="json") for c in with_source["chunks"] if c.chunk_id == chunk_id),
            None,
        )
        if actual != chosen:
            raise APIError(409, "P4B_SOURCE_CHANGED", "原文引用无法重新核验")
        return actual

    @app.post(PREFIX + "/claims/{cid}/verify")
    async def verify(cid: str, body: VerifyFacts, request: Request):
        user = actor(request, "FINANCE", "ADMIN")
        own(cid, user)
        generation = enterprise.authority(user["id"])
        async with app.state.work_lock, service.lock:
            return hybrid.verify(user, cid, body, token=request.cookies.get(COOKIE),
                                 generation=generation)

    @app.post(PREFIX + "/claims", status_code=202)
    async def submit(body: HybridSubmission, request: Request, background: BackgroundTasks):
        user = actor(request, "EMPLOYEE")
        today = (
            business_clock()
            if business_clock
            else datetime.now(UTC).astimezone(timezone(timedelta(hours=8))).date()
        )
        if body.incurred_on > today:
            raise APIError(422, "FUTURE_EXPENSE", "费用日期不能晚于当前业务日期")
        scope, payload = user["id"] + ":p4b-create", body.model_dump_json()
        async with app.state.work_lock:
            with hybrid.store.lock, hybrid.store.db:
                hybrid.store.db.execute("BEGIN IMMEDIATE")
                replay = hybrid.store.replay(scope, body.idempotency_key, payload)
                if replay is not None:
                    return replay
                cid, now = "CLM-" + str(today.year) + "-" + uuid4().hex, datetime.now(UTC)
                fields = dict(
                    applicant_name=user["name"],
                    department=user["department"],
                    position=user["position"],
                    category="OTHER",
                    amount=f"{body.amount:.2f}",
                    incurred_on=str(body.incurred_on),
                    purpose=body.purpose,
                    city=body.city,
                    trip_start=str(body.trip_start),
                    trip_end=str(body.trip_end),
                    nights=None,
                    seller=body.seller,
                    notes=body.notes,
                )
                raw = json.dumps(fields, ensure_ascii=False, sort_keys=True)
                claim = ClaimDetail(
                    claim_id=cid,
                    employee=Employee(
                        employee_id=user["id"],
                        name=user["name"],
                        department=user["department"],
                        legal_entity=user["company_id"],
                    ),
                    expense=Expense(
                        category="OTHER",
                        amount=body.amount,
                        city=body.city,
                        incurred_on=body.incurred_on,
                    ),
                    status=ClaimStatus.NEW,
                    created_at=now,
                    updated_at=now,
                    documents=[
                        Document(
                            document_id="DECL-" + uuid4().hex,
                            file_name="p4b-declaration.json",
                            content_type="application/json",
                            sha256=sha256(raw.encode()).hexdigest(),
                        )
                    ],
                )
                hybrid.store.db.execute(
                    "INSERT INTO claims VALUES(?,?)", (cid, claim.model_dump_json())
                )
                if body.invoice:
                    token = enterprise.resource(
                        "invoice", body.invoice.draft_id, user, personal=True
                    )
                    invoice = HybridInvoice(
                        **body.invoice.model_dump(),
                        amount=body.amount,
                        seller=body.seller,
                        incurred_on=body.incurred_on,
                        token=token,
                        idempotency_key=body.idempotency_key,
                    )
                    company_ids = {
                        r[0]
                        for r in hybrid.store.db.execute(
                            "SELECT claim_id FROM enterprise_claims WHERE company_id=?",
                            (user["company_id"],),
                        )
                    }
                    repo.invoice_store.bind(invoice, claim, duplicate_claim_ids=company_ids)
                hybrid.store.db.execute(
                    "INSERT INTO submissions VALUES(?,?,?)",
                    (cid, "p4b:" + scope + ":" + body.idempotency_key, raw),
                )
                hybrid.store.db.execute(
                    "INSERT INTO enterprise_claims VALUES(?,?,?,'AI_AUDITING',1,0,'',?)",
                    (cid, user["company_id"], user["id"], timestamp()),
                )
                version = dict(
                    version=1,
                    fields=fields,
                    submitted_at=timestamp(),
                    stage="AI_AUDITING",
                    p4b=True,
                    expense_type=body.expense_type,
                )
                hybrid.store.db.execute(
                    "INSERT INTO enterprise_versions VALUES(?,?,?)",
                    (cid, 1, json.dumps(version, ensure_ascii=False)),
                )
                hybrid.store.append(
                    "SUBMISSION",
                    cid,
                    dict(
                        fields=fields,
                        expense_type=body.expense_type,
                        candidate_meals=body.model_dump(
                            include={
                                "candidate_full_days",
                                "candidate_lunches",
                                "candidate_dinners",
                            }
                        ),
                    ),
                )
                result = dict(claim_id=cid, schema_version="p4b.1")
                hybrid.store.remember(scope, body.idempotency_key, payload, result)
                enterprise.event(user, "P4B_SUBMITTED", cid, version=1)
            background.add_task(app.state.run_audit, cid, 1)
            return result

    @app.post(PREFIX + "/claims/{cid}/reaudit", status_code=202)
    async def reaudit(cid: str, body: Reaudit, request: Request, background: BackgroundTasks):
        user = actor(request, "FINANCE")
        own(cid, user)
        generation = enterprise.authority(user["id"])
        scope, payload = user["id"] + ":p4b-reaudit:" + cid, body.model_dump_json()
        async with app.state.work_lock, service.lock:
            with hybrid.store.lock, hybrid.store.db:
                hybrid.store.db.execute(
                    "UPDATE enterprise_claims SET claim_id=claim_id WHERE claim_id=?", (cid,)
                )
                user = enterprise.live_actor(request.cookies.get(COOKIE), "FINANCE",
                                             expected_generation=generation,
                                             company_id=user["company_id"])
                replay = hybrid.store.replay(scope, body.idempotency_key, payload)
                if replay is not None:
                    return replay
                row, claim = enterprise.claim_row(cid, user), repo.get(cid)
                context = repo.run_context(cid, claim.active_run_id)
                if (
                    row["stage"] not in {"FINANCE_REVIEW", "RETURNED"}
                    or row["revision"] != body.expected_revision
                    or not context
                    or context.get("human_review")
                    or (context.get("superseded") and row["stage"] != "RETURNED")
                    or repo.erp_write_count(cid)
                ):
                    raise APIError(409, "INVALID_TRANSITION", "仅可重审未作最终决定的申请")
                # Preserve the old run/checkpoint/report; supersede its authority to resume.
                context.update(
                    superseded=True,
                    interrupted=False,
                    superseded_reason="P4B evidence verification; new audit",
                )
                hybrid.store.db.execute(
                    "UPDATE runs SET body=? WHERE claim_id=? AND run_id=?",
                    (json.dumps(context), cid, str(claim.active_run_id)),
                )
                claim.status, claim.active_run_id, claim.decision_result = (
                    ClaimStatus.NEW,
                    None,
                    None,
                )
                claim.erp_status, claim.updated_at = ERPStatus.NOT_STARTED, datetime.now(UTC)
                hybrid.store.db.execute(
                    "UPDATE claims SET body=? WHERE id=?", (claim.model_dump_json(), cid)
                )
                version = row["version"] + 1
                if hybrid.legacy.is_hotel_matrix(cid):
                    refresh = getattr(hybrid.legacy.facts, "refresh_pending", None)
                    if refresh is not None:
                        refresh(cid, version)
                previous = enterprise.history(cid)[-1]
                snapshot = {
                    k: v
                    for k, v in previous.items()
                    if k
                    not in {
                        "ai_result",
                        "run_id",
                        "finance_action",
                        "finance_reason",
                        "reviewer_id",
                        "reviewed_at",
                    }
                }
                snapshot.update(
                    version=version,
                    stage="AI_AUDITING",
                    submitted_at=timestamp(),
                    fact_revision=hybrid.store.read("FACTS", cid)[0],
                )
                hybrid.store.db.execute(
                    "INSERT INTO enterprise_versions VALUES(?,?,?)",
                    (cid, version, json.dumps(snapshot, ensure_ascii=False)),
                )
                hybrid.store.db.execute(
                    "UPDATE enterprise_claims SET stage='AI_AUDITING',"
                    "version=?,revision=revision+1 WHERE claim_id=?",
                    (version, cid),
                )
                result = dict(claim_id=cid, version=version, schema_version="p4b.1")
                hybrid.store.remember(scope, body.idempotency_key, payload, result)
                enterprise.event(user, "P4B_REAUDIT_REQUESTED", cid, version=version)
            background.add_task(app.state.run_audit, cid, version)
            return result
