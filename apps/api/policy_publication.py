"""v1.4 local publication; authority never belongs to an LLM or actor label."""

import hmac
from uuid import UUID

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from apps.api.errors import APIError, install_error_handlers
from apps.api.invoices import UploadLimit
from apps.api.policies import LocalPolicyGate
from apps.api.policy_publication_store import PolicyPublicationStore
from domain.policies import LocalCapability
from domain.policy_publication import (
    ChangeLifecycle,
    LifecyclePreview,
    PublicationPreview,
    PublishPolicy,
    RuleConfirmation,
)


def create_publication_app(settings, draft, embedding):
    app = FastAPI(
        title="Controlled Local Policy Publication",
        version="1.4.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_error_handlers(app)
    app.add_middleware(UploadLimit)
    app.add_middleware(LocalPolicyGate, settings=settings)
    store = PolicyPublicationStore(draft, embedding, app_env=settings.app_env) if draft else None
    app.state.store = store

    def authorize(body):
        expected = (
            settings.policy_demo_token.get_secret_value() if settings.policy_demo_token else ""
        )
        if not expected or not hmac.compare_digest(
            body.token.get_secret_value().encode(), expected.encode()
        ):
            raise APIError(403, "POLICY_CAPABILITY", "本机制度管理凭证无效")
        if store is None:
            raise APIError(503, "POLICY_DEMO_DISABLED", "制度发布 Demo 未开启")

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        return JSONResponse(
            status_code=503,
            content=dict(
                error=dict(
                    code="PUBLICATION_DATABASE_FAILED",
                    message="事务失败；未提交半发布状态",
                    details={},
                ),
                request_id=str(request.state.request_id),
            ),
        )

    @app.post("/policies/history")
    def history(body: LocalCapability):
        authorize(body)
        return store.history()

    @app.post("/policies/{upload_id}/rules")
    def rules(upload_id: UUID, body: LocalCapability):
        authorize(body)
        return store.read(upload_id)

    @app.post("/policies/{upload_id}/confirm-rule")
    def confirm_rule(upload_id: UUID, body: RuleConfirmation):
        authorize(body)
        return store.confirm(upload_id, body)

    @app.post("/policies/{upload_id}/publication-preview")
    def preview(upload_id: UUID, body: PublicationPreview):
        authorize(body)
        return store.preview(upload_id, body)

    @app.post("/policies/{upload_id}/publish")
    def publish(upload_id: UUID, body: PublishPolicy):
        authorize(body)
        return store.publish(upload_id, body)

    @app.post("/policies/{policy_id}/lifecycle-preview")
    def lifecycle_preview(policy_id: str, body: LifecyclePreview):
        authorize(body)
        return store.lifecycle_preview(policy_id, body)

    @app.post("/policies/{policy_id}/lifecycle")
    def lifecycle(policy_id: str, body: ChangeLifecycle):
        authorize(body)
        return store.change_lifecycle(policy_id, body)

    return app
