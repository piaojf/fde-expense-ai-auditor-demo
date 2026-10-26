"""Versioned upload/OCR API; no changes to root OpenAPI or v1.1 DTOs."""

import os
from collections import deque
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from apps.api.errors import APIError, install_error_handlers
from apps.api.invoice_store import MAX_FILE
from domain.invoices import (
    Capability,
    CheckRequest,
    DraftRequest,
    InvoiceClaimRequest,
    InvoiceResponse,
)
from domain.schemas import AuditRequest
from domain.submission import CreateClaimRequest, SubmissionFields


class UploadLimit:
    """Limit the entire multipart request, including chunked transfer bodies."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        maximum = MAX_FILE + 64 * 1024
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = maximum + 1
        if declared > maximum:
            return await Response("Request too large", status_code=413)(scope, receive, send)
        # Starlette translates receive exceptions during multipart parsing into 400.
        # Bound before parsing so chunked oversized requests reliably return 413.
        frames = deque()
        total = 0
        while True:
            message = await receive()
            total += len(message.get("body", b""))
            if total > maximum:
                return await Response("Request too large", status_code=413)(scope, receive, send)
            frames.append(message)
            if message["type"] == "http.disconnect" or not message.get("more_body", False):
                break

        async def bounded_receive():
            return frames.popleft() if frames else await receive()

        await self.app(scope, bounded_receive, send)


def create_invoice_app(repo, service, submission_app, store, provider):
    app = FastAPI(title="Local Invoice Evidence", version="1.2.0")
    install_error_handlers(app)
    app.add_middleware(UploadLimit)
    lock = submission_app.state.creation_lock

    @app.post("/drafts")
    def reserve(body: DraftRequest):
        return store.reserve(body)

    @app.post("/drafts/{draft_id}/read")
    def read(draft_id: UUID, body: Capability):
        return store.read(draft_id, body.token)

    @app.post("/drafts/{draft_id}/attachments")
    async def upload(draft_id: UUID, token: UUID = Form(...), file: UploadFile = File(...)):
        with store.lock:
            store.draft(draft_id, token, mutable=True)
        part = store.path(uuid4(), ".part")
        size = 0
        try:
            with part.open("xb") as target:
                os.chmod(part, 0o600)
                while chunk := await file.read(64 * 1024):
                    size += len(chunk)
                    if size > MAX_FILE:
                        raise APIError(413, "FILE_TOO_LARGE", "File exceeds 10 MiB")
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            if not size:
                raise APIError(422, "INVALID_FILE", "Empty file")
            decoded = await run_in_threadpool(store.processor.validate, part)
            return await run_in_threadpool(
                store.upload, draft_id, token, part, file.filename, decoded
            )
        finally:
            # A registered STAGING part belongs to recovery, not compensation.
            with store.lock:
                registered = store.db.execute(
                    "SELECT 1 FROM invoice_attachments WHERE id=?", (part.stem,)
                ).fetchone()
            if not registered:
                part.unlink(missing_ok=True)
            await file.close()

    @app.post("/drafts/{draft_id}/attachments/{attachment_id}/delete")
    def delete(draft_id: UUID, attachment_id: UUID, body: Capability):
        return store.delete(draft_id, attachment_id, body.token)

    @app.post("/drafts/{draft_id}/attachments/{attachment_id}/ocr")
    def ocr(draft_id: UUID, attachment_id: UUID, body: Capability):
        return store.recognize(draft_id, attachment_id, body.token, provider)

    @app.post("/drafts/{draft_id}/attachments/{attachment_id}/ocr-retry")
    def ocr_retry(draft_id: UUID, attachment_id: UUID, body: Capability):
        return store.recognize(draft_id, attachment_id, body.token, provider, refresh=True)

    @app.post("/drafts/{draft_id}/attachments/{attachment_id}/preview")
    def preview(draft_id: UUID, attachment_id: UUID, body: Capability):
        with store.lock:
            store.draft(draft_id, body.token)
            attachment = store.attachment(draft_id, attachment_id)
        data = store.preview(attachment)
        return Response(
            data,
            media_type="image/png",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.post("/drafts/{draft_id}/checks")
    def checks(draft_id: UUID, body: CheckRequest):
        with store.lock:
            cid = store.draft(draft_id, body.token)
            attachment = store.attachment(draft_id)
            store.verified_path(attachment)
            if attachment["ocr"] is None:
                raise APIError(409, "OCR_REQUIRED", "OCR required")
            return {
                "warnings": store.checks(
                    attachment, body.fields, body.confirmed_fields, exclude_claim=cid
                )
            }

    @app.post("/claims", status_code=202, response_model=InvoiceResponse)
    async def create(body: InvoiceClaimRequest):
        async with lock:
            standard = CreateClaimRequest(
                **SubmissionFields.model_validate(
                    {key: getattr(body, key) for key in SubmissionFields.model_fields}
                ).model_dump(),
                idempotency_key=body.idempotency_key,
            )
            claim = repo.create_submission(
                standard, invoice_hook=lambda c, replay: store.bind(body, c, replay=replay)
            )
            if claim.status == "NEW":
                await service.audit(
                    claim.claim_id, AuditRequest(idempotency_key=f"submission:{claim.claim_id}")
                )
            return detail(claim.claim_id)

    def detail(claim_id):
        return InvoiceResponse(
            **submission_app.state.detail(claim_id).model_dump(exclude={"schema_version"}),
            invoice_evidence=store.evidence(claim_id),
        )

    @app.get("/claims/{claim_id}", response_model=InvoiceResponse)
    def get(claim_id: str):
        return detail(claim_id)

    @app.get("/claims/{claim_id}/invoice-preview")
    def claim_preview(claim_id: str):
        evidence = store.evidence(claim_id)
        if not evidence:
            raise APIError(404, "INVOICE_NOT_FOUND", "Invoice not found")
        return Response(
            store.preview(evidence["attachment"]),
            media_type="image/png",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    return app
