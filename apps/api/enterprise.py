"""Authenticated v1.5 portal. All ownership checks precede business adapter calls."""

import asyncio
import json
import secrets
from datetime import UTC, datetime
from hashlib import sha256
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from apps.api.enterprise_policy_publication import EnterprisePolicyPublicationStore
from apps.api.enterprise_store import EnterpriseStore, digest, finance_execution, timestamp
from apps.api.enterprise_travel import EnterpriseTravel, TravelCatalog
from apps.api.errors import APIError, install_error_handlers
from apps.api.invoices import UploadLimit
from apps.api.policy_library import owned_file_ids, uploaded_files
from database.models import PolicyVersion
from domain.enterprise import (
    CreateUser,
    FinanceAction,
    LoginRequest,
    PortalSubmission,
    ResubmitRequest,
    RetryRequest,
    SetUserEnabled,
)
from domain.enterprise_policies import (
    EnterpriseConfirmPolicy,
    EnterprisePreviewPolicy,
    EnterpriseRuleConfirmation,
)
from domain.enterprise_travel import (
    PublishTravelPolicy,
    RetireTravelPolicy,
    TravelDetails,
    TravelProfile,
)
from domain.invoices import ConfirmedFields, DraftRequest, InvoiceClaimRequest
from domain.policies import LocalCapability
from domain.policy_publication import (
    ChangeLifecycle,
    LifecyclePreview,
    PublicationPreview,
    PublishPolicy,
)
from domain.schemas import (
    AuditRequest,
    ClaimStatus,
    Employee,
    ERPStatus,
    Expense,
    HumanReviewRequest,
    ResumeRequest,
)
from domain.submission import CreateClaimRequest

COOKIE = "fde_enterprise_session"
CAPTCHA_COOKIE = "fde_login_challenge"


class EnterpriseBoundary:
    """Close legacy unowned routes only when enterprise mode is explicitly enabled."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not (
            scope["path"] == "/health" or scope["path"].startswith("/api/v1.5/")
        ):
            return await JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "ENTERPRISE_ROUTE_REQUIRED",
                        "message": "请从已登录的企业工作区访问",
                        "details": {},
                    },
                    "request_id": str(uuid4()),
                },
            )(scope, receive, send)
        return await self.app(scope, receive, send)


def create_enterprise_app(settings, repo, service, invoice_app, policy_app, publication_app):
    app = FastAPI(
        title="Enterprise Expense Portal",
        version="1.5.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_error_handlers(app)
    app.add_middleware(UploadLimit)
    store = EnterpriseStore(repo)
    invoice_store = repo.invoice_store
    work_lock = asyncio.Lock()
    app.state.store = store
    app.state.work_lock = work_lock

    def travel_publisher():
        policy_store()
        base = publication_app.state.store
        return EnterprisePolicyPublicationStore(
            base.draft, base.embedding, app_env=base.app_env, clock=base.clock
        )

    travel = EnterpriseTravel(
        store, travel_publisher,
        facts=service.catalog.facts if settings.p4a_enabled else None,
    )
    app.state.travel = travel
    service.catalog = TravelCatalog(service.catalog, travel)
    service.graph_factory.keywords["catalog"] = service.catalog

    def travel_snapshot(user, body):
        details = body.travel_details
        if settings.p4a_enabled and (body.fields.category == "HOTEL"
                                    or (details and details.expense_type == "HOTEL")):
            raise APIError(409, "P4A_ROUTE_REQUIRED", "请从住宿职级标准页面提交住宿费")
        if details is None and body.fields.category in {"HOTEL", "TRANSPORT"}:
            if settings.p4a_enabled or travel.publications(user["company_id"]):
                details = TravelDetails(expense_type=body.fields.category)
        if details:
            travel.require_schema()
            expected = "OTHER" if details.expense_type == "ALLOWANCE" else details.expense_type
            if body.fields.category != expected:
                raise APIError(422, "TRAVEL_CATEGORY_MISMATCH", "费用类型与差旅明细不符")
        return dict(
            travel_details=details.model_dump(mode="json") if details else None,
            employee_snapshot=travel.employee_snapshot(user, body.fields),
        )

    @app.get("/travel/runtime")
    def travel_runtime(request: Request):
        principal(request)
        provider = service.graph_factory.keywords.get("provider")
        return {"provider": settings.llm_provider,
                "model": settings.deepseek_model if settings.llm_provider == "deepseek" else "mock",
                "budget": provider.usage() if hasattr(provider, "usage") else None}

    @app.get("/travel/profiles")
    def travel_profiles(request: Request):
        actor = principal(request, "ADMIN")
        return {"items": [travel.profile(u, datetime.now(UTC).date())
                          for u in store.users(actor["company_id"])]}

    @app.post("/users/{user_id}/travel-profile")
    def set_travel_profile(user_id: str, body: TravelProfile, request: Request):
        return travel.set_profile(principal(request, "ADMIN"), user_id, body)

    @app.get("/travel/policies/{upload_id}")
    def read_travel(upload_id: UUID, request: Request):
        return travel.read(upload_id, principal(request, "ADMIN"))

    @app.post("/travel/policies/{upload_id}/publish")
    def publish_travel(upload_id: UUID, body: PublishTravelPolicy, request: Request):
        return travel.publish(upload_id, principal(request, "ADMIN"), body)

    @app.post("/travel/policies/{upload_id}/retire")
    def retire_travel(upload_id: UUID, body: RetireTravelPolicy, request: Request):
        return travel.retire(upload_id, principal(request, "ADMIN"), body)


    @app.middleware("http")
    async def session_guard(request, call_next):
        try:
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                origin = request.headers.get("origin")
                same_origin = f"{request.url.scheme}://{request.url.netloc}"
                if (origin and origin != same_origin and origin not in settings.cors_origins) or (
                    request.headers.get("sec-fetch-site") == "cross-site"
                ):
                    raise APIError(403, "ORIGIN_REJECTED", "请求来源不受信任")
            if request.url.path.endswith(("/auth/login", "/auth/captcha")):
                pass
            else:
                request.state.principal = store.principal(request.cookies.get(COOKIE))
            response = await call_next(request)
        except APIError as exc:
            response = JSONResponse(
                status_code=exc.status,
                content={
                    "error": {"code": exc.code, "message": exc.message, "details": {}},
                    "request_id": str(uuid4()),
                },
            )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def principal(request, *roles):
        user = store.principal(request.cookies.get(COOKIE))
        if roles and user["role"] not in roles:
            raise APIError(403, "ROLE_FORBIDDEN", "当前账号没有此操作权限")
        return user

    def scope_key(user, kind):
        return user["id"] + ":" + kind

    def identity(user, fields):
        if (
            fields.applicant_name != user["name"]
            or fields.department != user["department"]
            or fields.position != user["position"]
        ):
            raise APIError(403, "IDENTITY_MISMATCH", "申请人信息必须与登录账号一致")
        return Employee(
            employee_id=user["id"],
            name=user["name"],
            department=user["department"],
            legal_entity=user["company_id"],
        )

    def company_claim_ids(user):
        with store.lock:
            return {
                row[0]
                for row in store.db.execute(
                    "SELECT claim_id FROM enterprise_claims WHERE company_id=?",
                    (user["company_id"],),
                )
            }

    def detail(claim_id, user):
        row = store.claim_row(claim_id, user)
        claim = repo.get(claim_id)
        history = store.history(claim_id)
        context = repo.run_context(claim_id, claim.active_run_id) if claim.active_run_id else None
        evidence = invoice_store.evidence(claim_id)
        result = dict(
            **row,
            fields=repo.submission(claim_id),
            claim=claim.model_dump(mode="json"),
            ai_result=(context or {}).get("ai_decision_result"),
            ai_analysis=(context or {}).get("llm_analysis"),
            travel_details=history[-1].get("travel_details") if history else None,
            employee_snapshot=history[-1].get("employee_snapshot") if history else None,
            history=history,
            timeline=store.events(user, claim_id),
            invoice_evidence=evidence,
            erp_sync_status=claim.erp_status.value,
        )
        if any(
            r.get("rule_id") == "TRAVEL_STANDARD" and r.get("outcome") == "UNKNOWN"
            and r.get("reason", "").startswith("缺少适用的已发布标准")
            for r in (result["ai_result"] or {}).get("rule_results", [])
        ):
            result["travel_standard_diagnosis"] = travel.unmatched_diagnosis(
                row["company_id"], history[-1], context or {}
            )
        if hasattr(service.catalog, "report"):
            result["compliance_analysis"] = service.catalog.report(claim_id, claim.active_run_id)
        if hasattr(type(service.catalog), "advisory"):
            result["advisory_analysis"] = service.catalog.advisory(claim_id, claim.active_run_id)
        if (hasattr(service.catalog, "is_hotel_matrix")
                and service.catalog.is_hotel_matrix(claim_id)):
            result["p4a"] = True
        if evidence:
            with store.lock:
                draft = store.db.execute(
                    "SELECT draft_id FROM invoice_evidence WHERE claim_id=?", (claim_id,)
                ).fetchone()[0]
            result["invoice_draft"] = {"draft_id": draft, "token": ""}
        return result

    def stage_after_audit(claim):
        if claim.status == ClaimStatus.PENDING_HUMAN:
            return "FINANCE_REVIEW"
        return "SYSTEM_ERROR"

    async def run_audit(claim_id, version):
        async with work_lock:
            with store.lock:
                row = store.db.execute(
                    "SELECT owner_id,stage,version FROM enterprise_claims WHERE claim_id=?",
                    (claim_id,),
                ).fetchone()
            if not row or row[1] != "AI_AUDITING" or row[2] != version:
                return
            user = store.user(row[0])
            try:
                await service.audit(
                    claim_id,
                    AuditRequest(idempotency_key=f"enterprise:{claim_id}:v{version}:audit"),
                )
                claim = repo.get(claim_id)
                context = repo.run_context(claim_id, claim.active_run_id) or {}
                stage = stage_after_audit(claim)
                reason = "" if stage == "FINANCE_REVIEW" else "审核执行中断，请重试或联系管理员"
                with store.lock, store.db:
                    store.db.execute(
                        "UPDATE enterprise_claims SET stage=?,reason=?,"
                        "revision=revision+1 WHERE claim_id=?",
                        (stage, reason, claim_id),
                    )
                    store.update_version(
                        claim_id,
                        version,
                        ai_result=context.get("ai_decision_result"),
                        run_id=str(claim.active_run_id),
                        stage=stage,
                        invoice_evidence=invoice_store.evidence(claim_id),
                    )
                    store.event(user, "AI_AUDIT_COMPLETED", claim_id, stage=stage, version=version)
            except Exception as exc:
                # No forged decision. A durable explicit failure is visible and retryable.
                with store.lock, store.db:
                    store.db.execute(
                        "UPDATE enterprise_claims SET stage='SYSTEM_ERROR',reason=?,"
                        "revision=revision+1 WHERE claim_id=?",
                        ("审核执行中断，请重试或联系管理员", claim_id),
                    )
                    store.event(
                        user,
                        "AI_AUDIT_INTERRUPTED",
                        claim_id,
                        version=version,
                        error_type=type(exc).__name__,
                    )

    app.state.run_audit = run_audit

    async def initialize():
        """Reconcile durable stage after a process interruption, without inventing a result."""
        with store.lock:
            pending = store.db.execute(
                "SELECT claim_id,owner_id,version FROM enterprise_claims WHERE stage='AI_AUDITING'"
            ).fetchall()
        for cid, owner, version in pending:
            claim = repo.get(cid)
            if claim.status == ClaimStatus.NEW:
                await run_audit(cid, version)
                continue
            context = repo.run_context(cid, claim.active_run_id) or {}
            stage = stage_after_audit(claim)
            with store.lock, store.db:
                store.db.execute(
                    "UPDATE enterprise_claims SET stage=?,reason=?,revision=revision+1 "
                    "WHERE claim_id=?",
                    (stage, "" if stage == "FINANCE_REVIEW" else "审核进程中断，请重试", cid),
                )
                store.update_version(
                    cid, version, stage=stage, ai_result=context.get("ai_decision_result")
                )
                store.event(store.user(owner), "AI_AUDIT_RECONCILED", cid, stage=stage)

    app.state.initialize = initialize

    @app.get("/auth/captcha")
    def captcha(request: Request):
        binding = request.cookies.get(CAPTCHA_COOKIE)
        if not binding or len(binding) > 100:
            binding = secrets.token_urlsafe(32)
        response = JSONResponse(store.issue_captcha(
            request.client.host if request.client else "unknown", binding
        ))
        response.set_cookie(
            CAPTCHA_COOKIE, binding, httponly=True, secure=settings.enterprise_cookie_secure,
            samesite="strict", path="/api/v1.5/auth", max_age=600,
        )
        return response

    @app.post("/auth/login")
    async def login(body: LoginRequest, request: Request):
        token, user = await run_in_threadpool(
            store.login,
            body,
            request.client.host if request.client else "unknown",
            settings.enterprise_session_seconds,
            request.cookies.get(CAPTCHA_COOKIE, ""),
        )
        response = JSONResponse({"user": user})
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            secure=settings.enterprise_cookie_secure,
            samesite="strict",
            path="/api/v1.5",
            max_age=settings.enterprise_session_seconds,
        )
        return response

    @app.get("/auth/me")
    def me(request: Request):
        return {"user": principal(request)}

    @app.post("/auth/logout")
    def logout(request: Request):
        store.logout(request.cookies.get(COOKIE))
        response = JSONResponse({"logged_out": True})
        response.delete_cookie(
            COOKIE,
            path="/api/v1.5",
            httponly=True,
            samesite="strict",
            secure=settings.enterprise_cookie_secure,
        )
        return response

    @app.get("/users")
    def users(request: Request):
        user = principal(request, "ADMIN")
        return {"items": store.users(user["company_id"])}

    @app.post("/users", status_code=201)
    async def create_user(body: CreateUser, request: Request):
        user = principal(request, "ADMIN")
        return await run_in_threadpool(store.create_user, user["company_id"], body, user)

    @app.post("/users/{user_id}/enabled")
    def enabled(user_id: str, body: SetUserEnabled, request: Request):
        return store.set_enabled(principal(request, "ADMIN"), user_id, body.enabled)

    @app.get("/claims")
    def claims(request: Request):
        user = principal(request)
        return {"items": [detail(cid, user) for cid in store.claims(user)]}

    @app.get("/claims/{claim_id}")
    def read_claim(claim_id: str, request: Request):
        return detail(claim_id, principal(request))

    def claim_invoice(claim_id, user, version):
        store.claim_row(claim_id, user)
        if version is None:
            evidence = invoice_store.evidence(claim_id)
        else:
            snapshot = next((v for v in store.history(claim_id) if v["version"] == version), None)
            evidence = snapshot.get("invoice_evidence") if snapshot else None
        if not evidence:
            raise APIError(404, "INVOICE_NOT_FOUND", "此提交版本没有发票")
        invoice_store.verified_path(evidence["attachment"])
        return evidence["attachment"]

    @app.get("/claims/{claim_id}/invoice-preview")
    async def claim_invoice_preview(claim_id: str, request: Request, version: int | None = None):
        attachment = claim_invoice(claim_id, principal(request), version)
        raw = await run_in_threadpool(invoice_store.preview, attachment)
        return Response(raw, media_type="image/png")

    @app.get("/claims/{claim_id}/invoice-original")
    def claim_invoice_original(claim_id: str, request: Request, version: int | None = None):
        attachment = claim_invoice(claim_id, principal(request), version)
        return Response(
            invoice_store.verified_path(attachment).read_bytes(),
            media_type=attachment["content_type"],
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(attachment["file_name"]),
                "Content-Security-Policy": "sandbox; default-src 'none'",
            },
        )

    @app.post("/claims", status_code=202)
    async def create_claim(body: PortalSubmission, request: Request, background: BackgroundTasks):
        user = principal(request, "EMPLOYEE")
        employee = identity(user, body.fields)
        travel_data = travel_snapshot(user, body)
        scope = scope_key(user, "create")
        async with work_lock:
            with store.lock:
                replay = store.replay(scope, body.idempotency_key, body.model_dump_json())
            if replay:
                return detail(replay[1], user)
            invoice_body = None
            if body.invoice:
                token = store.resource("invoice", body.invoice.draft_id, user, personal=True)
                invoice_body = InvoiceClaimRequest(
                    **body.fields.model_dump(),
                    **body.invoice.model_dump(),
                    token=token,
                    idempotency_key=body.idempotency_key,
                )

            def hook(claim, replay):
                if replay:
                    raise APIError(409, "CREATE_INCOMPLETE", "已有申请，请通过原请求查询")
                if invoice_body:
                    invoice_store.bind(
                        invoice_body, claim, duplicate_claim_ids=company_claim_ids(user)
                    )
                store.db.execute(
                    "INSERT INTO enterprise_claims VALUES(?,?,?,'AI_AUDITING',1,0,'',?)",
                    (claim.claim_id, user["company_id"], user["id"], timestamp()),
                )
                version = dict(
                    version=1,
                    submitted_at=timestamp(),
                    **travel_data,
                    fields=body.fields.model_dump(mode="json"),
                    stage="AI_AUDITING",
                    invoice_evidence=invoice_store.evidence(claim.claim_id),
                )
                store.db.execute(
                    "INSERT INTO enterprise_versions VALUES(?,?,?)",
                    (claim.claim_id, 1, json.dumps(version, ensure_ascii=False)),
                )
                store.remember(scope, body.idempotency_key, body.model_dump_json(), claim.claim_id)
                store.event(user, "SUBMITTED", claim.claim_id, version=1)

            claim = repo.create_submission(
                CreateClaimRequest(
                    **body.fields.model_dump(),
                    idempotency_key="portal:" + digest(scope + ":" + body.idempotency_key),
                ),
                employee=employee,
                invoice_hook=hook,
            )
            background.add_task(run_audit, claim.claim_id, 1)
            return detail(claim.claim_id, user)

    @app.post("/claims/{claim_id}/resubmit", status_code=202)
    async def resubmit(
        claim_id: str, body: ResubmitRequest, request: Request, background: BackgroundTasks
    ):
        user = principal(request, "EMPLOYEE")
        identity(user, body.fields)
        if (hasattr(service.catalog, "is_hybrid")
                and service.catalog.is_hybrid(claim_id)):
            raise APIError(409, "P4B_ROUTE_REQUIRED", "混合审核须保留申请原文并由财务补证重审")
        if (hasattr(service.catalog, "is_hotel_matrix")
                and service.catalog.is_hotel_matrix(claim_id)):
            raise APIError(409, "P4A_ROUTE_REQUIRED", "请从住宿职级标准页面重新提交")
        travel_data = travel_snapshot(user, body)
        async with work_lock, service.lock:
            with store.lock, store.db:
                store.db.execute("BEGIN IMMEDIATE")
                row = store.claim_row(claim_id, user)
                scope = scope_key(user, "resubmit:" + claim_id)
                if store.replay(scope, body.idempotency_key, body.model_dump_json()):
                    return detail(claim_id, user)
                if row["stage"] != "RETURNED" or row["revision"] != body.expected_revision:
                    raise APIError(409, "REVISION_CONFLICT", "仅当前版本的退回单可以重新提交")
                claim = repo.get(claim_id)
                if repo.erp_write_count(claim_id):
                    raise APIError(409, "ERP_EFFECT_EXISTS", "已有入账记录，不能修改")
                old_evidence = invoice_store.evidence(claim_id)
                if old_evidence and not body.invoice:
                    raise APIError(422, "INVOICE_CONFIRM_REQUIRED", "请重新核对并确认保留的发票")
                if body.invoice:
                    token = store.resource("invoice", body.invoice.draft_id, user, personal=True)
                    invoice_body = InvoiceClaimRequest(
                        **body.fields.model_dump(),
                        **body.invoice.model_dump(),
                        token=token,
                        idempotency_key=body.idempotency_key,
                    )
                    attachment = invoice_store.attachment(
                        body.invoice.draft_id, body.invoice.attachment_id
                    )
                    invoice_store.verified_path(attachment)
                    if old_evidence and (
                        attachment["attachment_id"] == old_evidence["attachment"]["attachment_id"]
                    ):
                        warnings = invoice_store.checks(
                            attachment,
                            body.fields,
                            body.invoice.confirmed_fields,
                            exclude_claim=claim_id,
                            claim_ids=company_claim_ids(user),
                        )
                        if warnings and not body.invoice.discrepancy_reason.strip():
                            raise APIError(422, "EXPLANATION_REQUIRED", "请说明重新核对的发票提示")
                        evidence = {
                            **old_evidence,
                            "confirmed_fields": body.invoice.confirmed_fields.model_dump(
                                mode="json"
                            ),
                            "warnings": warnings,
                            "discrepancy_reason": body.invoice.discrepancy_reason,
                            "confirmed_at": timestamp(),
                            "confirmation_payload": invoice_body.model_dump_json(
                                exclude={"token", "idempotency_key"}
                            ),
                        }
                        store.db.execute(
                            "UPDATE invoice_evidence SET body=? WHERE claim_id=?",
                            (json.dumps(evidence, ensure_ascii=False), claim_id),
                        )
                    else:
                        # Previous version retains the original immutable evidence and blob.
                        store.db.execute(
                            "DELETE FROM invoice_evidence WHERE claim_id=?", (claim_id,)
                        )
                        invoice_store.bind(
                            invoice_body, claim, duplicate_claim_ids=company_claim_ids(user)
                        )
                fields = body.fields
                claim.expense = Expense(
                    category=fields.category,
                    city=fields.city,
                    amount=fields.amount,
                    incurred_on=fields.incurred_on,
                    nights=fields.nights or 1,
                )
                claim.status, claim.active_run_id = ClaimStatus.NEW, None
                claim.updated_at = datetime.now(UTC)
                claim.decision_result, claim.erp_status = None, ERPStatus.NOT_STARTED
                claim.documents[0].sha256 = sha256(
                    fields.model_dump_json().encode("utf-8")
                ).hexdigest()
                claim.documents[0].parse_status = "UNPARSED"
                store.db.execute(
                    "UPDATE claims SET body=? WHERE id=?", (claim.model_dump_json(), claim_id)
                )
                store.db.execute(
                    "UPDATE submissions SET payload=? WHERE claim_id=?",
                    (fields.model_dump_json(), claim_id),
                )
                version = row["version"] + 1
                store.db.execute(
                    "UPDATE enterprise_claims SET stage='AI_AUDITING',version=?,"
                    "revision=revision+1,reason='' WHERE claim_id=?",
                    (version, claim_id),
                )
                snapshot = dict(
                    version=version,
                    **travel_data,
                    fields=fields.model_dump(mode="json"),
                    submitted_at=timestamp(),
                    stage="AI_AUDITING",
                    invoice_evidence=invoice_store.evidence(claim_id),
                )
                store.db.execute(
                    "INSERT INTO enterprise_versions VALUES(?,?,?)",
                    (claim_id, version, json.dumps(snapshot, ensure_ascii=False)),
                )
                store.remember(scope, body.idempotency_key, body.model_dump_json(), claim_id)
                store.event(user, "RESUBMITTED", claim_id, version=version)
            background.add_task(run_audit, claim_id, version)
            return detail(claim_id, user)

    async def execute_finance(claim_id, body, requester, token, *, recovery_scope=None):
        # All callers hold work_lock. The requester remains distinct from the historical reviewer.
        requester = store.principal(token)
        if requester["role"] != "FINANCE":
            raise APIError(403, "ROLE_FORBIDDEN", "当前账号没有此操作权限")
        scope = recovery_scope or scope_key(requester, "finance:" + claim_id)
        with store.lock, store.db:
            store.db.execute("UPDATE enterprise_claims SET claim_id=claim_id WHERE claim_id=?",
                             (claim_id,))
            row = store.claim_row(claim_id, requester)
            replay = store.replay(scope, body.idempotency_key, body.model_dump_json())
            if replay and json.loads(replay[2]).get("done") and row["stage"] in (
                "APPROVED", "REJECTED", "RETURNED", "ERP_SYNC_PENDING"
            ):
                return detail(claim_id, requester)
            if replay:
                operation = json.loads(replay[2])
            else:
                if recovery_scope or row["stage"] != "FINANCE_REVIEW" or (
                    row["revision"] != body.expected_revision
                ):
                    raise APIError(409, "REVISION_CONFLICT", "申请状态已改变，请刷新后操作")
                claim = repo.get(claim_id)
                context = repo.run_context(claim_id, claim.active_run_id)
                if body.advisory_report_digest is not None:
                    if row["owner_id"] == requester["id"]:
                        raise APIError(403, "SELF_REVIEW_FORBIDDEN", "不能审核本人的申请")
                    advisory = (service.catalog.advisory(claim_id, claim.active_run_id)
                                if hasattr(type(service.catalog), "advisory") else None)
                    if (not advisory or advisory["report_digest"]
                            != body.advisory_report_digest):
                        raise APIError(409, "ADVISORY_STALE", "AI 建议已变化，请刷新后决定")
                if not context or context.get("human_review") or repo.erp_write_count(claim_id):
                    raise APIError(409, "INVALID_TRANSITION", "申请已有决定或副作用")
                operation = dict(run_id=str(claim.active_run_id), run_revision=context["revision"],
                                 version=row["version"], done=False,
                                 authorization=store.finance_authorization(requester, row))
                store.remember(
                    scope, body.idempotency_key, body.model_dump_json(), claim_id, operation
                )
                store.db.execute("UPDATE enterprise_claims SET stage='FINANCE_APPLYING' "
                                 "WHERE claim_id=?", (claim_id,))
        execution = finance_execution.set(dict(claim_id=claim_id, scope=scope,
                                                key=body.idempotency_key, token=token))
        try:
            with store.lock, store.db:
                store.db.execute("UPDATE enterprise_claims SET claim_id=claim_id WHERE claim_id=?",
                                 (claim_id,))
                user = store.authorize_finance(scope, body.idempotency_key, token)
            if body.action == "RETURN":
                async with service.lock:
                    with store.lock, store.db:
                        store.db.execute("UPDATE enterprise_claims SET claim_id=claim_id "
                                         "WHERE claim_id=?", (claim_id,))
                        user = store.authorize_finance(scope, body.idempotency_key, token)
                        context = repo.run_context(claim_id, operation["run_id"])
                        if context.get("human_review") or repo.erp_write_count(claim_id):
                            raise APIError(409, "INVALID_TRANSITION", "已作决定的申请不能退回")
                        context.update(
                            superseded=True, interrupted=False, superseded_reason=body.reason
                        )
                        store.db.execute(
                            "UPDATE runs SET body=? WHERE claim_id=? AND run_id=?",
                            (json.dumps(context), claim_id, operation["run_id"]),
                        )
                stage = "RETURNED"
            else:
                reviewed = await service.human_review(
                    claim_id,
                    HumanReviewRequest(
                        run_id=operation["run_id"],
                        expected_revision=operation["run_revision"],
                        decision=body.action,
                        reviewer_id=user["id"],
                        comment=body.reason,
                        idempotency_key="pf:" + digest(scope + body.idempotency_key),
                    ),
                )
                await service.resume(
                    claim_id,
                    ResumeRequest(
                        run_id=reviewed.run_id,
                        expected_revision=reviewed.revision,
                        idempotency_key="pr:" + digest(scope + body.idempotency_key),
                    ),
                )
                claim = repo.get(claim_id)
                stage = (
                    "ERP_SYNC_PENDING"
                    if claim.status == ClaimStatus.PENDING_ERP
                    else "APPROVED"
                    if claim.status == ClaimStatus.COMPLETED and body.action == "APPROVE"
                    else "REJECTED"
                    if claim.status == ClaimStatus.COMPLETED
                    else "SYSTEM_ERROR"
                )
            with store.lock, store.db:
                store.db.execute("UPDATE enterprise_claims SET claim_id=claim_id WHERE claim_id=?",
                                 (claim_id,))
                user = store.authorize_finance(scope, body.idempotency_key, token)
                store.db.execute(
                    "UPDATE enterprise_claims SET stage=?,reason=?,"
                    "revision=revision+1 WHERE claim_id=?",
                    (stage, body.reason, claim_id),
                )
                store.update_version(
                    claim_id,
                    operation["version"],
                    stage=stage,
                    finance_action=body.action,
                    finance_reason=body.reason,
                    reviewer_id=user["id"],
                    reviewed_at=(repo.run_context(claim_id, operation["run_id"])
                                 .get("human_review") or {}).get("reviewed_at", timestamp()),
                )
                historical_only = (not user.get("enabled") or user["role"] != "FINANCE"
                                   or user["company_id"] != requester["company_id"]
                                   or store.authority(user["id"]) !=
                                   operation["authorization"]["generation"])
                store.event(
                    requester if historical_only else user,
                    "FINANCE_EFFECT_RECONCILED" if historical_only else "FINANCE_" + body.action,
                    claim_id,
                    reason=body.reason,
                    version=operation["version"],
                    stage=stage,
                )
                operation["done"] = True
                store.db.execute(
                    "UPDATE enterprise_requests SET result=? WHERE scope=? AND key=?",
                    (json.dumps(operation), scope, body.idempotency_key),
                )
            return detail(claim_id, requester)
        except APIError as exc:
            if exc.code in ("FINANCE_AUTHORITY_REVOKED", "FINANCE_INTENT_INVALID",
                            "FINANCE_EVIDENCE_INVALID"):
                with store.lock, store.db:
                    operation["invalidated_reason"] = exc.code
                    store.db.execute(
                        "UPDATE enterprise_requests SET result=? WHERE scope=? AND key=?",
                                     (json.dumps(operation), scope, body.idempotency_key))
                    store.db.execute(
                        "UPDATE enterprise_claims SET stage='MANUAL_RECOVERY_REQUIRED',"
                                     "reason=?,revision=revision+1 WHERE claim_id=?",
                                     (exc.message if exc.code == "FINANCE_EVIDENCE_INVALID"
                                      else "财务授权或意图已失效，需要人工恢复", claim_id))
                    store.event(requester, "FINANCE_RECOVERY_DENIED", claim_id,
                                original_actor_id=scope.split(":", 1)[0],
                                intent_key=body.idempotency_key, security_reason=exc.code)
                raise
            if exc.status in (401, 403):
                with store.lock, store.db:
                    store.event(requester, "FINANCE_RECOVERY_DENIED", claim_id,
                                original_actor_id=scope.split(":", 1)[0],
                                intent_key=body.idempotency_key, security_reason=exc.code)
                raise
            # Invalid approval must leave no reserved decision, so rejection/return is possible.
            context = repo.run_context(claim_id, operation["run_id"]) or {}
            if not context.get("human_review") and not repo.erp_write_count(claim_id):
                with store.lock, store.db:
                    store.db.execute(
                        "DELETE FROM enterprise_requests WHERE scope=? AND key=?",
                        (scope, body.idempotency_key),
                    )
                    store.db.execute(
                        "UPDATE enterprise_claims SET stage='FINANCE_REVIEW' WHERE claim_id=?",
                        (claim_id,),
                    )
                    store.event(user, "FINANCE_ACTION_DENIED", claim_id, action=body.action)
            raise

        finally:
            finance_execution.reset(execution)

    @app.post("/claims/{claim_id}/finance-action")
    async def finance_action(claim_id: str, body: FinanceAction, request: Request):
        user = principal(request, "FINANCE")
        async with work_lock:
            return await execute_finance(claim_id, body, user, request.cookies.get(COOKIE))

    @app.post("/claims/{claim_id}/retry")
    async def retry(
        claim_id: str, body: RetryRequest, request: Request, background: BackgroundTasks
    ):
        user = principal(request)
        initial = store.claim_row(claim_id, user)
        if initial["stage"] == "FINANCE_APPLYING":
            principal(request, "FINANCE")
            async with work_lock:
                current = store.claim_row(claim_id, principal(request, "FINANCE"))
                if current["stage"] != "FINANCE_APPLYING" or (
                    current["revision"] != body.expected_revision
                ):
                    raise APIError(409, "REVISION_CONFLICT", "申请已改变，请刷新")
                with store.lock:
                    candidates = store.db.execute(
                        "SELECT scope,payload,result FROM enterprise_requests WHERE claim_id=?",
                        (claim_id,),
                    ).fetchall()
                pending = [(scope, payload) for scope, payload, result in candidates
                           if ":finance:" in scope and result
                           and not json.loads(result).get("done")]
                if len(pending) != 1:
                    raise APIError(409, "MANUAL_RECOVERY_REQUIRED", "没有唯一可恢复的财务意图")
                scope, payload = pending[0]
                resumed = await execute_finance(
                    claim_id, FinanceAction.model_validate_json(payload), user,
                    request.cookies.get(COOKIE), recovery_scope=scope,
                )
                with store.lock, store.db:
                    store.event(user, "FINANCE_ACTION_RECOVERED", claim_id,
                                original_actor_id=scope.split(":", 1)[0])
                return resumed
        async with work_lock:
            user = principal(request)
            row = store.claim_row(claim_id, user)
            scope = scope_key(user, "retry:" + claim_id)
            with store.lock:
                replay = store.replay(scope, body.idempotency_key, body.model_dump_json())
            if replay and (not replay[2] or json.loads(replay[2]).get("done")):
                return detail(claim_id, user)
            if not replay and row["revision"] != body.expected_revision:
                raise APIError(409, "REVISION_CONFLICT", "申请已改变，请刷新")
            if row["stage"] == "ERP_SYNC_PENDING":
                principal(request, "FINANCE")
                claim = repo.get(claim_id)
                context = repo.run_context(claim_id, claim.active_run_id)
                if replay:
                    intent = json.loads(replay[2])
                else:
                    intent = dict(
                        run_id=str(claim.active_run_id),
                        run_revision=context["revision"],
                        done=False,
                    )
                    with store.lock, store.db:
                        store.remember(
                            scope, body.idempotency_key, body.model_dump_json(), claim_id, intent
                        )
                with store.lock:
                    candidates = store.db.execute(
                        "SELECT scope,key,result FROM enterprise_requests WHERE claim_id=?",
                        (claim_id,),
                    ).fetchall()
                matching = [(s, k) for s, k, result in candidates if ":finance:" in s and result
                            and json.loads(result).get("run_id") == str(claim.active_run_id)]
                if len(matching) != 1:
                    raise APIError(409, "MANUAL_RECOVERY_REQUIRED", "没有唯一可恢复的财务意图")
                finance_scope, finance_key = matching[0]
                execution = finance_execution.set(dict(claim_id=claim_id, scope=finance_scope,
                                                       key=finance_key,
                                                       token=request.cookies.get(COOKIE)))
                try:
                    with store.lock, store.db:
                        store.db.execute("UPDATE enterprise_claims SET claim_id=claim_id "
                                         "WHERE claim_id=?", (claim_id,))
                        store.authorize_finance(finance_scope, finance_key,
                                                request.cookies.get(COOKIE))
                    if claim.status != ClaimStatus.COMPLETED:
                        await service.resume(
                            claim_id,
                            ResumeRequest(
                                run_id=intent["run_id"],
                                expected_revision=intent["run_revision"],
                                idempotency_key="re:" + digest(scope + body.idempotency_key),
                            ),
                        )
                except APIError as exc:
                    if exc.code in ("FINANCE_AUTHORITY_REVOKED", "FINANCE_INTENT_INVALID",
                                    "FINANCE_EVIDENCE_INVALID"):
                        with store.lock, store.db:
                            store.db.execute("UPDATE enterprise_claims "
                                             "SET stage='MANUAL_RECOVERY_REQUIRED',"
                                             "reason=?,revision=revision+1 WHERE claim_id=?",
                                             (exc.message if exc.code == "FINANCE_EVIDENCE_INVALID"
                                              else "财务授权或意图已失效，需要人工恢复", claim_id))
                            store.event(user, "FINANCE_RECOVERY_DENIED", claim_id,
                                        original_actor_id=finance_scope.split(":", 1)[0],
                                        intent_key=finance_key, security_reason=exc.code)
                    raise
                finally:
                    finance_execution.reset(execution)
                stage = (
                    "APPROVED"
                    if repo.get(claim_id).status == ClaimStatus.COMPLETED
                    else row["stage"]
                )
            elif row["stage"] == "SYSTEM_ERROR":
                principal(request, "EMPLOYEE")
                claim = repo.get(claim_id)
                context = repo.run_context(claim_id, claim.active_run_id) or {}
                if context.get("human_review") or repo.erp_write_count(claim_id):
                    raise APIError(409, "MANUAL_RECOVERY_REQUIRED", "已有财务决定，请联系财务恢复")
                claim.status, claim.active_run_id = ClaimStatus.NEW, None
                with store.lock, store.db:
                    store.db.execute(
                        "UPDATE claims SET body=? WHERE id=?", (claim.model_dump_json(), claim_id)
                    )
                    # New version gives a new audit key and never replays a failed accepted run.
                    version = row["version"] + 1
                    store.update_version(claim_id, row["version"], stage="SYSTEM_ERROR")
                    store.db.execute(
                        "INSERT INTO enterprise_versions VALUES(?,?,?)",
                        (
                            claim_id,
                            version,
                            json.dumps(
                                dict(
                                    version=version,
                                    fields=repo.submission(claim_id),
                                    submitted_at=timestamp(),
                                    stage="AI_AUDITING",
                                )
                            ),
                        ),
                    )
                    store.db.execute(
                        "UPDATE enterprise_claims SET version=? WHERE claim_id=?",
                        (version, claim_id),
                    )
                stage = "AI_AUDITING"
                background.add_task(run_audit, claim_id, version)
            else:
                raise APIError(409, "INVALID_TRANSITION", "当前申请不需要重试")
            with store.lock, store.db:
                store.db.execute(
                    "UPDATE enterprise_claims SET stage=?,revision=revision+1 WHERE claim_id=?",
                    (stage, claim_id),
                )
                existing_request = store.replay(scope, body.idempotency_key, body.model_dump_json())
                if existing_request:
                    store.db.execute(
                        "UPDATE enterprise_requests SET result=? WHERE scope=? AND key=?",
                        (json.dumps({"done": True}), scope, body.idempotency_key),
                    )
                else:
                    store.remember(
                        scope,
                        body.idempotency_key,
                        body.model_dump_json(),
                        claim_id,
                        {"done": True},
                    )
                store.update_version(
                    claim_id, version if stage == "AI_AUDITING" else row["version"], stage=stage
                )
                store.event(user, "RETRY", claim_id, stage=stage)
            return detail(claim_id, user)

    @app.get("/dashboard")
    def dashboard(request: Request):
        user = principal(request, "FINANCE", "ADMIN")
        rows = [store.claim_row(cid, user) for cid in store.claims(user)]
        return {
            "total": len(rows),
            "by_stage": {
                stage: sum(row["stage"] == stage for row in rows)
                for stage in (
                    "AI_AUDITING",
                    "FINANCE_REVIEW",
                    "FINANCE_APPLYING",
                    "RETURNED",
                    "APPROVED",
                    "REJECTED",
                    "ERP_SYNC_PENDING",
                    "SYSTEM_ERROR",
                )
            },
        }

    @app.get("/audit")
    def audit(request: Request):
        user = principal(request, "FINANCE", "ADMIN")
        return {
            "events": store.events(user),
            "claims": [
                {"claim_id": cid, "events": [e.model_dump(mode="json") for e in repo.events(cid)]}
                for cid in store.claims(user)
            ],
        }

    async def delegate(target, path, *, body=None, file=None):
        # In-process ASGI execution: genuine existing handlers, no network/paid calls or UI stubs.
        transport = httpx.ASGITransport(app=target, client=("127.0.0.1", 0))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8001"
        ) as client:
            if file:
                response = await client.post(path, data=body, files={"file": file})
            else:
                response = await client.post(path, json=body or {})
        if response.status_code >= 400:
            try:
                error = response.json()["error"]
                raise APIError(response.status_code, error["code"], error["message"])
            except (ValueError, KeyError):
                raise APIError(
                    response.status_code, "BUSINESS_REQUEST_FAILED", "业务操作失败"
                ) from None
        return Response(
            response.content,
            status_code=response.status_code,
            headers={
                key: value
                for key, value in response.headers.items()
                if key.lower() in {"content-type", "content-disposition", "content-security-policy"}
            },
        )

    @app.post("/invoices/drafts")
    async def draft(request: Request):
        user = principal(request, "EMPLOYEE")
        body = await request.json()
        try:
            uid = str(UUID(body.get("draft_id", "")))
        except (ValueError, TypeError):
            raise APIError(422, "INVALID_DRAFT_ID", "草稿编号无效") from None
        with store.lock, store.db:
            existing = store.db.execute(
                "SELECT 1 FROM enterprise_resources WHERE kind='invoice' AND id=?", (uid,)
            ).fetchone()
            if existing:
                token = store.resource("invoice", uid, user, personal=True)
            else:
                # Supplied capability is never an authorization signal in enterprise mode.
                token = str(uuid4())
                if store.db.execute("SELECT 1 FROM invoice_drafts WHERE id=?", (uid,)).fetchone():
                    raise APIError(409, "DRAFT_EXISTS", "草稿编号已被使用")
                store.own_resource("invoice", uid, user, token)
            result = invoice_store.reserve(DraftRequest(draft_id=uid, token=token))
        return result

    @app.post("/invoices/drafts/{draft_id}/{action:path}")
    async def invoice_action(draft_id: UUID, action: str, request: Request):
        user = principal(request, "EMPLOYEE")
        token = store.resource("invoice", draft_id, user, personal=True)
        allowed = action in {"read", "checks", "attachments"}
        if action.startswith("attachments/"):
            parts = action.split("/")
            allowed = len(parts) == 3 and parts[2] in {"preview", "ocr", "ocr-retry", "delete"}
            if allowed:
                try:
                    UUID(parts[1])
                except ValueError:
                    allowed = False
        if not allowed:
            raise APIError(404, "RESOURCE_NOT_FOUND", "资料不存在")
        path = f"/drafts/{draft_id}/{action}"
        if action == "attachments":
            form = await request.form()
            file = form.get("file")
            if not file or not hasattr(file, "read"):
                raise APIError(422, "FILE_REQUIRED", "请选择发票文件")
            raw = await file.read(10 * 1024 * 1024 + 1)
            return await delegate(
                invoice_app,
                path,
                body={"token": token},
                file=(file.filename, raw, file.content_type),
            )
        body = await request.json()
        if action == "checks":
            # No spoofed employee identity is accepted even for pre-submission checks.
            from pydantic import ValidationError

            from domain.submission import SubmissionFields

            try:
                fields = SubmissionFields.model_validate(body.get("fields"))
                confirmed = ConfirmedFields.model_validate(body.get("confirmed_fields"))
            except ValidationError:
                raise APIError(
                    422, "INVALID_INVOICE_FIELDS", "请检查报销表单与票据确认字段"
                ) from None
            identity(user, fields)
            with store.lock:
                cid = invoice_store.draft(draft_id, token)
                attachment = invoice_store.attachment(draft_id)
                invoice_store.verified_path(attachment)
                if not attachment["ocr"]:
                    raise APIError(409, "OCR_REQUIRED", "请先识别发票")
                return {
                    "warnings": invoice_store.checks(
                        attachment,
                        fields,
                        confirmed,
                        exclude_claim=cid,
                        claim_ids=company_claim_ids(user),
                    )
                }
        return await delegate(invoice_app, path, body={**body, "token": token})

    def policy_store():
        if policy_app.state.store is None or publication_app.state.store is None:
            raise APIError(503, "POLICY_DISABLED", "制度数据库未启用")
        return policy_app.state.store

    async def enterprise_policy_call(method, uid, model, body):
        try:
            validated = model.model_validate(body)
        except ValidationError:
            raise APIError(
                422, "INVALID_POLICY_FIELDS", "请核对制度信息、适用范围及确认字段"
            ) from None
        try:
            return await run_in_threadpool(method, uid, validated)
        except SQLAlchemyError:
            raise APIError(
                503, "POLICY_DATABASE_FAILED", "制度事务失败；未提交变更，请稍后重试"
            ) from None

    def policy_version(policy_id, user):
        draft_store = policy_store()
        with Session(draft_store.engine) as session:
            value = session.get(PolicyVersion, policy_id)
            if not value or value.legal_entity != user["company_id"]:
                raise APIError(404, "POLICY_NOT_FOUND", "制度版本不存在")

    @app.post("/policies/upload")
    async def upload_policy(request: Request, file: UploadFile = File(...)):
        user = principal(request, "ADMIN")
        target = policy_store()
        raw = await file.read(10 * 1024 * 1024 + 1)
        if len(raw) > 10 * 1024 * 1024:
            raise APIError(413, "FILE_TOO_LARGE", "文件超过 10 MiB")
        async with work_lock:
            before = {item["upload_id"] for item in target.list()["items"]}
            row = await run_in_threadpool(target.upload, raw, file.filename)
            uid = row["upload_id"]
            with store.lock, store.db:
                existing = store.db.execute(
                    "SELECT company_id FROM enterprise_resources WHERE kind='policy' AND id=?",
                    (uid,),
                ).fetchone()
                if existing:
                    store.resource("policy", uid, user)
                elif uid in before:
                    raise APIError(
                        409, "POLICY_CONTENT_RESERVED", "相同文件已有管理记录，请使用本企业独立制度"
                    )
                else:
                    store.own_resource("policy", uid, user)
                store.event(user, "POLICY_UPLOADED", upload_id=uid, file_sha256=row["sha256"])
        await file.close()
        return row

    @app.post("/policies/list")
    def policy_list(request: Request):
        user = principal(request, "ADMIN")
        with store.lock:
            owned = {
                row[0]
                for row in store.db.execute(
                    "SELECT id FROM enterprise_resources WHERE kind='policy' AND company_id=?",
                    (user["company_id"],),
                )
            }
        return {
            "items": [row for row in policy_store().list()["items"] if row["upload_id"] in owned],
            "embedding_mode": settings.policy_draft_embedding,
        }

    def policy_file_scope(user, upload_id=None):
        with store.lock:
            owners = dict(store.db.execute(
                "SELECT id,company_id FROM enterprise_resources WHERE kind='policy'"))
        if (upload_id in owners and owners[upload_id] != user["company_id"]):
            raise APIError(404, "RESOURCE_NOT_FOUND", "资料不存在")
        target = policy_store()
        return target, owned_file_ids(
            target, owners, user["company_id"], publication_app.state.store.clock())

    @app.get("/policies/files")
    def policy_files(request: Request):
        user = principal(request, "ADMIN", "FINANCE")
        try:
            target, owned = policy_file_scope(user)
            return uploaded_files(target, owned, user["company_id"],
                                  publication_app.state.store.clock(), travel)
        except SQLAlchemyError:
            raise APIError(
                503, "POLICY_DATABASE_FAILED", "制度列表暂时不可用，请稍后重试") from None

    @app.get("/policies/files/{upload_id}/original")
    def policy_file_original(upload_id: UUID, request: Request):
        user = principal(request, "ADMIN", "FINANCE")
        try:
            target, owned = policy_file_scope(user, str(upload_id))
            if str(upload_id) not in owned:
                raise APIError(404, "RESOURCE_NOT_FOUND", "资料不存在")
            raw, mime, name = target.original(upload_id)
        except SQLAlchemyError:
            raise APIError(
                503, "POLICY_DATABASE_FAILED", "制度原件暂时不可用，请稍后重试") from None
        return Response(raw, media_type=mime, headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(name),
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        })

    @app.post("/publication/policies/history")
    def publication_history(request: Request):
        user = principal(request, "ADMIN")
        policy_store()
        history = publication_app.state.store.history()
        # Event result contains nested snapshots; filter by authoritative source version.
        with Session(policy_app.state.store.engine) as session:
            owned = set(
                session.scalars(
                    select(PolicyVersion.policy_version_id).where(
                        PolicyVersion.legal_entity == user["company_id"]
                    )
                )
            )
        return {
            **history,
            "versions": [v for v in history["versions"] if v["policy_version_id"] in owned],
            "events": [
                e
                for e in history["events"]
                if (
                    e.get("policy_version_id") in owned
                    or (e.get("published") or {}).get("policy_version_id") in owned
                    or (e.get("policy") or {}).get("policy_version_id") in owned
                    or e.get("predecessor_id") in owned
                )
            ],
        }

    @app.post("/policies/{upload_id}/{action}")
    async def policy_action(upload_id: UUID, action: str, request: Request):
        user = principal(request, "ADMIN")
        store.resource("policy", upload_id, user)
        if action == "delete" and travel.publications(user["company_id"], upload_id):
            raise APIError(
                409, "TRAVEL_POLICY_PUBLISHED",
                "已发布差旅标准的原文件须保留，请记录退休日期",
            )
        if action not in {"read", "original", "confirm", "ingest", "preview", "delete"}:
            raise APIError(404, "ACTION_NOT_FOUND", "操作不存在")
        body = await request.json()
        if action in {"confirm", "preview"}:
            entity = (
                (body.get("metadata") or {}).get("legal_entity")
                if action == "confirm"
                else body.get("legal_entity")
            )
            if entity != user["company_id"]:
                raise APIError(403, "COMPANY_MISMATCH", "制度必须属于当前企业")
        if body.get("remote_authorized"):
            raise APIError(403, "PAID_PROVIDER_DISABLED", "当前演示不允许调用收费向量服务")
        secret = settings.policy_demo_token.get_secret_value()
        if action in {"confirm", "preview"}:
            model = EnterpriseConfirmPolicy if action == "confirm" else EnterprisePreviewPolicy
            return await enterprise_policy_call(
                getattr(policy_store(), action), upload_id, model, {**body, "token": secret}
            )
        return await delegate(
            policy_app, f"/policies/{upload_id}/{action}", body={**body, "token": secret}
        )

    @app.post("/publication/policies/{resource_id}/{action}")
    async def publication_action(resource_id: str, action: str, request: Request):
        user = principal(request, "ADMIN")
        if settings.p4a_enabled and action in {"confirm-rule", "publish", "lifecycle"}:
            raise APIError(409, "P4A_ROUTE_REQUIRED", "住宿规则的确认、发布和回滚请使用 P4A")
        body = await request.json()
        if action in {"lifecycle-preview", "lifecycle"}:
            policy_version(resource_id, user)
        elif action in {"rules", "confirm-rule", "publication-preview", "publish"}:
            store.resource("policy", resource_id, user)
        else:
            raise APIError(404, "ACTION_NOT_FOUND", "操作不存在")
        for key in ("predecessor_id", "restore_id"):
            if body.get(key):
                policy_version(body[key], user)
        if action in {"confirm-rule", "publish", "lifecycle"}:
            body["actor"] = user["id"]
        if "idempotency_key" in body:
            body["idempotency_key"] = "ep:" + digest(user["id"] + ":" + body["idempotency_key"])
        secret = settings.policy_demo_token.get_secret_value()
        if action in {
            "rules",
            "confirm-rule",
            "publication-preview",
            "publish",
            "lifecycle-preview",
            "lifecycle",
        }:
            policy_store()
            original = publication_app.state.store
            publisher = EnterprisePolicyPublicationStore(
                original.draft, original.embedding, app_env=original.app_env, clock=original.clock
            )
            method, model = {
                "rules": (lambda uid, body: publisher.read(uid), LocalCapability),
                "confirm-rule": (publisher.confirm, EnterpriseRuleConfirmation),
                "publication-preview": (publisher.preview, PublicationPreview),
                "publish": (publisher.publish, PublishPolicy),
                "lifecycle-preview": (publisher.lifecycle_preview, LifecyclePreview),
                "lifecycle": (publisher.change_lifecycle, ChangeLifecycle),
            }[action]
            return await enterprise_policy_call(
                method, resource_id, model, {**body, "token": secret}
            )
        return await delegate(
            publication_app, f"/policies/{resource_id}/{action}", body={**body, "token": secret}
        )

    return app
