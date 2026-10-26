"""Versioned, opt-in, loopback-only local policy management. No publication API."""

import hmac
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse, Response
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from apps.api.errors import APIError, install_error_handlers
from apps.api.invoices import UploadLimit
from apps.api.policy_store import PolicyDraftStore
from domain.policies import ConfirmPolicy, IngestPolicy, LocalCapability, PreviewPolicy
from evals.shadow_mode.http_budget import HTTPBudget, HTTPHooks
from integrations.policies.parser import PolicyParser
from rag.embeddings.providers import DeterministicTestEmbedding, RemoteEmbeddingProvider


class LocalPolicyGate:
    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        host = headers.get(b"host", b"").decode("latin-1")
        try:
            local_host = urlsplit("//" + host).hostname in {"127.0.0.1", "localhost", "::1"}
        except ValueError:
            local_host = False
        peer = (scope.get("client") or (None,))[0]
        origin = headers.get(b"origin", b"").decode("latin-1")
        origin_ok = (
            not origin
            or origin in self.settings.cors_origins
            or origin
            in {
                "http://" + host,
                "https://" + host,
            }
        )
        proxied = any(k == b"forwarded" or k.startswith(b"x-forwarded-") for k in headers)
        # Configured non-local CORS entries never grant policy-management access.
        if origin:
            try:
                origin_ok = origin_ok and urlsplit(origin).hostname in {
                    "127.0.0.1",
                    "localhost",
                    "::1",
                }
            except ValueError:
                origin_ok = False
        if not self.settings.policy_demo_enabled:
            status, code, message = 503, "POLICY_DEMO_DISABLED", "企业制度管理仅可显式开启本机演示"
        elif peer not in {"127.0.0.1", "::1"} or not local_host or not origin_ok or proxied:
            status, code, message = 403, "LOCAL_ONLY", "制度管理仅限受控本机访问"
        else:
            return await self.app(scope, receive, send)
        await JSONResponse(
            status_code=status,
            content={
                "error": {"code": code, "message": message, "details": {}},
                "request_id": str(uuid4()),
            },
        )(scope, receive, send)


def create_policy_app(settings, processor, *, store=None):
    app = FastAPI(
        title="Controlled Local Policy Drafts",
        version="1.3.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_error_handlers(app)
    app.add_middleware(UploadLimit)
    app.add_middleware(LocalPolicyGate, settings=settings)
    app.state.engine = None
    app.state.budget = None
    if settings.policy_demo_enabled:
        secret = settings.policy_demo_token
        if secret is None or len(secret.get_secret_value()) < 32:
            raise ValueError("Enabled local policy demo requires a >=32-character capability")
        if store is None:
            engine = create_engine(
                settings.database_url, pool_pre_ping=True, connect_args={"connect_timeout": 3}
            )
            app.state.engine = engine

            def embedding_factory(authorized):
                if (
                    settings.policy_draft_embedding == "deterministic"
                    and settings.app_env == "test"
                ):
                    return DeterministicTestEmbedding()
                if settings.policy_draft_embedding == "remote":
                    if (
                        not authorized
                        or not settings.policy_remote_enabled
                        or settings.policy_remote_max_requests == 0
                    ):
                        raise APIError(
                            403, "REMOTE_CONSENT_REQUIRED", "远程向量调用需要明确授权和固定请求预算"
                        )
                    if app.state.budget is None:
                        app.state.budget = HTTPBudget(
                            Path(settings.policy_remote_budget_path).resolve(),
                            "ui-p3a-policy-embedding-v1",
                            limit=settings.policy_remote_max_requests,
                        )
                    hooks = HTTPHooks(app.state.budget, "bge-m3", "policy-draft")
                    return RemoteEmbeddingProvider(
                        base_url=settings.embedding_base_url,
                        api_key=settings.embedding_api_key,
                        model_name=settings.embedding_model,
                        dimension=settings.embedding_dimension,
                        timeout=settings.embedding_timeout,
                        max_attempts=settings.embedding_max_attempts,
                        batch_size=settings.embedding_batch_size,
                        event_hooks=hooks.hooks(),
                    )
                raise APIError(
                    503,
                    "DRAFT_EMBEDDING_DISABLED",
                    "草稿向量服务未开启；原文和确认信息已保存在本机",
                )

            store = PolicyDraftStore(engine, PolicyParser(processor), embedding_factory)
    app.state.store = store

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "POLICY_DATABASE_UNAVAILABLE",
                    "message": "制度数据库不可用或未迁移",
                    "details": {},
                },
                "request_id": str(request.state.request_id),
            },
        )

    def authorize(token):
        actual = token.get_secret_value() if hasattr(token, "get_secret_value") else token
        expected = (
            settings.policy_demo_token.get_secret_value() if settings.policy_demo_token else ""
        )
        if not expected or not hmac.compare_digest(actual.encode(), expected.encode()):
            raise APIError(403, "POLICY_CAPABILITY", "本机制度管理凭证无效")

    @app.post("/policies/list")
    def list_policies(body: LocalCapability):
        authorize(body.token)
        return {**store.list(), "embedding_mode": settings.policy_draft_embedding}

    @app.post("/policies/upload")
    async def upload(token: str = Form(...), file: UploadFile = File(...)):
        authorize(token)
        try:
            if Path(file.filename or "").suffix.lower() not in {".pdf", ".docx"}:
                raise APIError(422, "INVALID_FILE", "仅支持 PDF / DOCX")
            raw = await file.read(10 * 1024 * 1024 + 1)
            if len(raw) > 10 * 1024 * 1024:
                raise APIError(413, "FILE_TOO_LARGE", "文件超过 10 MiB")
            return await run_in_threadpool(store.upload, raw, file.filename)
        finally:
            await file.close()

    @app.post("/policies/{upload_id}/read")
    def read(upload_id: UUID, body: LocalCapability):
        authorize(body.token)
        return store.read(upload_id)

    @app.post("/policies/{upload_id}/original")
    def original(upload_id: UUID, body: LocalCapability):
        authorize(body.token)
        raw, mime, name = store.original(upload_id)
        return Response(
            raw,
            media_type=mime,
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(name),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'",
            },
        )

    @app.post("/policies/{upload_id}/confirm")
    def confirm(upload_id: UUID, body: ConfirmPolicy):
        authorize(body.token)
        return store.confirm(upload_id, body)

    @app.post("/policies/{upload_id}/ingest")
    def ingest(upload_id: UUID, body: IngestPolicy):
        authorize(body.token)
        return store.ingest(upload_id, remote_authorized=body.remote_authorized)

    @app.post("/policies/{upload_id}/preview")
    def preview(upload_id: UUID, body: PreviewPolicy):
        authorize(body.token)
        return store.preview(upload_id, body)

    @app.post("/policies/{upload_id}/delete")
    def delete(upload_id: UUID, body: LocalCapability):
        authorize(body.token)
        return store.delete(upload_id)

    return app
