"""Versioned P4A endpoints behind the existing authenticated enterprise session."""

import json
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

from fastapi import BackgroundTasks, Request
from sqlalchemy.orm import Session

from apps.api.enterprise_store import digest, timestamp
from apps.api.errors import APIError
from apps.api.policy_publication_store import fail
from database.models import PolicyVersion
from domain.enterprise import PortalInvoice
from domain.hotel_matrix import (
    HotelFields,
    HotelSubmission,
    MatrixConfirm,
    MatrixLifecycle,
    MatrixLifecyclePreview,
    MatrixPreview,
    MatrixPublish,
    TrustedFact,
)
from domain.invoices import ConfirmedFields
from domain.schemas import DTO, AuditEvent, ClaimDetail, ClaimStatus, Document, Employee, Expense

PREFIX = "/p4a/v1"


class HotelInvoice(HotelFields, PortalInvoice):
    token: str
    idempotency_key: str


def install_hotel_api(app, settings, repo, service, enterprise, draft, publication, clock=None):
    facts = service.catalog.facts

    def user(request, *roles):
        p = request.state.principal
        if roles and p["role"] not in roles:
            raise APIError(403, "ROLE_FORBIDDEN", "当前账号无此权限")
        return p

    def company_ids(p):
        with facts.lock:
            return {
                r[0]
                for r in facts.db.execute(
                    "SELECT claim_id FROM enterprise_claims WHERE company_id=?", (p["company_id"],)
                )
            }

    def own(uid, p):
        enterprise.resource("policy", uid, p)
        if draft is None:
            raise APIError(503, "POLICY_DISABLED", "制度数据库不可用")

    @app.get(PREFIX + "/people")
    def people(request: Request):
        p = user(request, "ADMIN", "FINANCE")
        return dict(
            items=[
                {k: u[k] for k in ("id", "name", "department", "position")}
                for u in enterprise.users(p["company_id"])
                if u["role"] == "EMPLOYEE"
            ]
        )

    @app.get(PREFIX + "/versions")
    def versions(request: Request):
        from sqlalchemy import select

        from apps.api.policy_publication_store import summary

        p = user(request, "ADMIN")
        with Session(draft.engine) as s:
            rows = s.scalars(
                select(PolicyVersion).where(
                    PolicyVersion.legal_entity == p["company_id"],
                    PolicyVersion.category == "HOTEL",
                    PolicyVersion.status == "ACTIVE",
                    PolicyVersion.authority_level >= 100,
                )
            ).all()
            return dict(items=[summary(v) for v in rows])

    @app.get(PREFIX + "/claims")
    def claim_reports(request: Request):
        p = user(request)
        return dict(
            items=[
                dict(claim_id=cid, rule_result=facts.read(cid)[1])
                for cid in enterprise.claims(p)
                if facts.read(cid)
            ]
        )

    @app.get(PREFIX + "/facts")
    def list_facts(request: Request):
        p = user(request)
        rows = facts.list(p["company_id"])
        if p["role"] == "EMPLOYEE":
            rows = [
                r
                for r in rows
                if r["kind"] == "CITY"
                or (r["kind"] == "EMPLOYEE" and r["key"] == p["id"])
                or (r["kind"] == "STAY" and r["value"]["employee_id"] == p["id"])
            ]
        return dict(items=rows, schema_version="p4a.1")

    @app.post(PREFIX + "/facts")
    def add_fact(body: TrustedFact, request: Request):
        p = user(request, "ADMIN", "FINANCE")
        if body.kind != "STAY" and p["role"] != "ADMIN":
            raise APIError(403, "ROLE_FORBIDDEN", "职级及城市主数据仅限管理员维护")
        if body.kind == "EMPLOYEE" or body.kind == "STAY":
            employee_id = body.key if body.kind == "EMPLOYEE" else body.value["employee_id"]
            employee = enterprise.user(employee_id)
            if not employee or employee["company_id"] != p["company_id"]:
                raise APIError(404, "USER_NOT_FOUND", "员工不存在")
        return facts.add(p["company_id"], p["id"], body)

    @app.post(PREFIX + "/policies/{uid}/read")
    def matrix_read(uid: str, request: Request):
        p = user(request, "ADMIN")
        own(uid, p)
        return publication.read(uid)

    @app.post(PREFIX + "/policies/{uid}/confirm")
    def matrix_confirm(uid: str, body: MatrixConfirm, request: Request):
        p = user(request, "ADMIN")
        own(uid, p)
        return publication.confirm_matrix(uid, body, p["id"])

    @app.post(PREFIX + "/policies/{uid}/preview")
    def matrix_preview(uid: str, body: MatrixPreview, request: Request):
        p = user(request, "ADMIN")
        own(uid, p)
        return publication.preview(uid, body)

    @app.post(PREFIX + "/policies/{uid}/publish")
    def matrix_publish(uid: str, body: MatrixPublish, request: Request):
        p = user(request, "ADMIN")
        own(uid, p)
        body = body.model_copy(
            update={"idempotency_key": "p4a:" + digest(p["id"] + body.idempotency_key)}
        )
        return publication.publish_matrix(uid, body, p["id"])

    def own_version(pid, p):
        if draft is None:
            raise APIError(503, "POLICY_DISABLED", "制度数据库不可用")
        with Session(draft.engine) as s:
            value = s.get(PolicyVersion, pid)
            if value is None or value.legal_entity != p["company_id"]:
                raise APIError(404, "POLICY_NOT_FOUND", "制度版本不存在")

    @app.post(PREFIX + "/versions/{pid}/lifecycle-preview")
    def lifecycle_preview(pid: str, body: MatrixLifecyclePreview, request: Request):
        p = user(request, "ADMIN")
        own_version(pid, p)
        if body.restore_id:
            own_version(body.restore_id, p)
        return publication.matrix_lifecycle_preview(pid, body)

    @app.post(PREFIX + "/versions/{pid}/lifecycle")
    def lifecycle(pid: str, body: MatrixLifecycle, request: Request):
        p = user(request, "ADMIN")
        own_version(pid, p)
        if body.restore_id:
            own_version(body.restore_id, p)
        body = body.model_copy(
            update={"idempotency_key": "p4a:" + digest(p["id"] + body.idempotency_key)}
        )
        return publication.matrix_lifecycle(pid, body, p["id"])

    class InvoiceCheck(DTO):
        fields: HotelFields
        confirmed_fields: ConfirmedFields

    @app.post(PREFIX + "/invoices/drafts/{did}/checks")
    def invoice_checks(did: str, body: InvoiceCheck, request: Request):
        p = user(request, "EMPLOYEE")
        if (body.fields.applicant_name, body.fields.department, body.fields.position) != (
            p["name"],
            p["department"],
            p["position"],
        ):
            raise APIError(403, "IDENTITY_MISMATCH", "身份信息不匹配")
        token = enterprise.resource("invoice", did, p, personal=True)
        with facts.lock:
            bound_claim = repo.invoice_store.draft(did, token)
            attachment = repo.invoice_store.attachment(did)
            repo.invoice_store.verified_path(attachment)
            if not attachment["ocr"]:
                fail("OCR_REQUIRED", "请先识别并确认发票")
            return dict(
                warnings=repo.invoice_store.checks(
                    attachment,
                    body.fields,
                    body.confirmed_fields,
                    claim_ids=company_ids(p),
                    exclude_claim=bound_claim,
                )
            )

    @app.get(PREFIX + "/claims/{cid}")
    def read_report(cid: str, request: Request):
        enterprise.claim_row(cid, user(request))
        row = facts.read(cid)
        if row is None:
            raise APIError(404, "CLAIM_NOT_FOUND", "不是 P4A 住宿单据")
        return dict(
            claim_id=cid,
            facts=row[0],
            rule_result=row[1],
            reports=facts.reports(cid),
            schema_version="p4a.1",
        )

    @app.post(PREFIX + "/claims", status_code=202)
    async def submit(body: HotelSubmission, request: Request, background: BackgroundTasks):
        p = user(request, "EMPLOYEE")
        f = body.fields
        today = (
            clock() if clock else datetime.now(UTC).astimezone(timezone(timedelta(hours=8))).date()
        )
        if f.trip_end > today or f.incurred_on > today:
            raise APIError(422, "FUTURE_EXPENSE", "住宿日期不得晚于当前业务日期")
        if (f.applicant_name, f.department, f.position) != (
            p["name"],
            p["department"],
            p["position"],
        ):
            raise APIError(403, "IDENTITY_MISMATCH", "身份须与登录用户一致")
        scope = p["id"] + ":p4a-create"
        fingerprint = body.model_dump_json()
        async with app.state.work_lock:
            with facts.lock:
                replay = enterprise.replay(scope, body.idempotency_key, fingerprint)
            if replay:
                return read_report(replay[1], request)
            snapshot = facts.snapshot(p, body)
            if snapshot["city"] and "STAY_CITY_MISMATCH" not in snapshot["blockers"]:
                canonical = snapshot["city"]["value"]["name"]
                f = f.model_copy(update={"city": canonical})
                snapshot["user_declaration"] = snapshot["declaration"]
                snapshot["declaration"] = {
                    **snapshot["declaration"],
                    "fields": f.model_dump(mode="json"),
                }
            cid = f"CLM-{today.year}-{uuid4().hex}"
            now = datetime.now(UTC)
            payload = f.model_dump_json()
            claim = ClaimDetail(
                claim_id=cid,
                employee=Employee(
                    employee_id=p["id"],
                    name=p["name"],
                    department=p["department"],
                    legal_entity=p["company_id"],
                ),
                expense=Expense(
                    category="HOTEL",
                    city=f.city,
                    amount=f.amount,
                    incurred_on=f.incurred_on,
                    nights=f.nights,
                ),
                status=ClaimStatus.NEW,
                created_at=now,
                updated_at=now,
                documents=[
                    Document(
                        document_id="DECL-" + uuid4().hex,
                        file_name="p4a-hotel-declaration.json",
                        content_type="application/json",
                        sha256=sha256(payload.encode()).hexdigest(),
                    )
                ],
            )
            with facts.lock, facts.db:
                facts.db.execute("BEGIN IMMEDIATE")
                if enterprise.replay(scope, body.idempotency_key, fingerprint):
                    fail("SUBMIT_RETRY", "提交已完成，请重试原请求")
                facts.db.execute("INSERT INTO claims VALUES(?,?)", (cid, claim.model_dump_json()))
                facts.db.execute(
                    "INSERT INTO submissions VALUES(?,?,?)",
                    (cid, "p4a:" + digest(scope + body.idempotency_key), payload),
                )
                facts.db.execute(
                    "INSERT INTO p4a_claims VALUES(?,?,NULL)",
                    (cid, json.dumps(snapshot, ensure_ascii=False)),
                )
                facts.db.execute(
                    "INSERT INTO p4a_claim_versions VALUES(?,?,?)",
                    (cid, 1, json.dumps(snapshot, ensure_ascii=False)),
                )
                if body.invoice:
                    token = enterprise.resource("invoice", body.invoice.draft_id, p, personal=True)
                    invoice = HotelInvoice(
                        **f.model_dump(),
                        **body.invoice.model_dump(),
                        token=token,
                        idempotency_key=body.idempotency_key,
                    )
                    repo.invoice_store.bind(invoice, claim, duplicate_claim_ids=company_ids(p))
                facts.db.execute(
                    "INSERT INTO enterprise_claims VALUES(?,?,?,'AI_AUDITING',1,0,'',?)",
                    (cid, p["company_id"], p["id"], timestamp()),
                )
                facts.db.execute(
                    "INSERT INTO enterprise_versions VALUES(?,?,?)",
                    (
                        cid,
                        1,
                        json.dumps(
                            dict(
                                version=1,
                                submitted_at=timestamp(),
                                fields=f.model_dump(mode="json"),
                                stage="AI_AUDITING",
                                invoice_evidence=repo.invoice_store.evidence(cid),
                            )
                        ),
                    ),
                )
                enterprise.remember(scope, body.idempotency_key, fingerprint, cid)
                enterprise.event(p, "P4A_SUBMITTED", cid, facts=snapshot)
                event = AuditEvent(
                    event_id=uuid4(),
                    claim_id=cid,
                    run_id=uuid4(),
                    event_type="CLAIM_CREATED",
                    actor="api:p4a",
                    occurred_at=now,
                    payload={"schema_version": "p4a.1", "trusted_facts_snapshot": snapshot},
                )
                facts.db.execute(
                    "INSERT INTO events(claim_id,body) VALUES(?,?)", (cid, event.model_dump_json())
                )
            background.add_task(app.state.run_audit, cid, 1)
            return dict(claim_id=cid, facts=snapshot, rule_result=None, schema_version="p4a.1")

    from domain.hotel_matrix import HotelResubmission
    from domain.schemas import ERPStatus

    @app.post(PREFIX + "/claims/{cid}/resubmit", status_code=202)
    async def resubmit(
        cid: str, body: HotelResubmission, request: Request, background: BackgroundTasks
    ):
        p = user(request, "EMPLOYEE")
        f = body.fields
        today = (
            clock() if clock else datetime.now(UTC).astimezone(timezone(timedelta(hours=8))).date()
        )
        if f.trip_end > today:
            raise APIError(422, "FUTURE_EXPENSE", "住宿日期不得晚于业务日期")
        if (f.applicant_name, f.department, f.position) != (
            p["name"],
            p["department"],
            p["position"],
        ):
            raise APIError(403, "IDENTITY_MISMATCH", "身份须与登录用户一致")
        if facts.read(cid) is None:
            raise APIError(404, "CLAIM_NOT_FOUND", "不是 P4A 住宿单据")
        scope = p["id"] + ":p4a-resubmit:" + cid
        fingerprint = body.model_dump_json()
        async with app.state.work_lock, service.lock:
            with facts.lock, facts.db:
                facts.db.execute("BEGIN IMMEDIATE")
                row = enterprise.claim_row(cid, p)
                if enterprise.replay(scope, body.idempotency_key, fingerprint):
                    return read_report(cid, request)
                if row["stage"] != "RETURNED" or row["revision"] != body.expected_revision:
                    fail("REVISION_CONFLICT", "仅当前版本的退回单可重新提交")
                if repo.erp_write_count(cid):
                    fail("ERP_EFFECT_EXISTS", "已有入账记录不能修改")
                snapshot = facts.snapshot(p, body)
                if snapshot["city"] and "STAY_CITY_MISMATCH" not in snapshot["blockers"]:
                    f = f.model_copy(update={"city": snapshot["city"]["value"]["name"]})
                    snapshot["user_declaration"] = snapshot["declaration"]
                    snapshot["declaration"] = {
                        **snapshot["declaration"],
                        "fields": f.model_dump(mode="json"),
                    }
                claim = repo.get(cid)
                old = repo.invoice_store.evidence(cid)
                if old and not body.invoice:
                    fail("INVOICE_CONFIRM_REQUIRED", "请重新确认保留发票")
                if body.invoice:
                    token = enterprise.resource("invoice", body.invoice.draft_id, p, personal=True)
                    invoice = HotelInvoice(
                        **f.model_dump(),
                        **body.invoice.model_dump(),
                        token=token,
                        idempotency_key=body.idempotency_key,
                    )
                    attachment = repo.invoice_store.attachment(
                        body.invoice.draft_id, body.invoice.attachment_id
                    )
                    repo.invoice_store.verified_path(attachment)
                    if old and attachment["attachment_id"] == old["attachment"]["attachment_id"]:
                        warnings = repo.invoice_store.checks(
                            attachment,
                            f,
                            body.invoice.confirmed_fields,
                            exclude_claim=cid,
                            claim_ids=company_ids(p),
                        )
                        if warnings and not body.invoice.discrepancy_reason.strip():
                            fail("EXPLANATION_REQUIRED", "请说明票据差异")
                        evidence = {
                            **old,
                            "confirmed_fields": body.invoice.confirmed_fields.model_dump(
                                mode="json"
                            ),
                            "warnings": warnings,
                            "discrepancy_reason": body.invoice.discrepancy_reason,
                            "confirmed_at": timestamp(),
                            "confirmation_payload": invoice.model_dump_json(
                                exclude={"token", "idempotency_key"}
                            ),
                        }
                        facts.db.execute(
                            "UPDATE invoice_evidence SET body=? WHERE claim_id=?",
                            (json.dumps(evidence, ensure_ascii=False), cid),
                        )
                    else:
                        facts.db.execute("DELETE FROM invoice_evidence WHERE claim_id=?", (cid,))
                        repo.invoice_store.bind(invoice, claim, duplicate_claim_ids=company_ids(p))
                claim.expense = Expense(
                    category="HOTEL",
                    city=f.city,
                    amount=f.amount,
                    incurred_on=f.incurred_on,
                    nights=f.nights,
                )
                claim.status, claim.active_run_id, claim.decision_result = (
                    ClaimStatus.NEW,
                    None,
                    None,
                )
                claim.erp_status = ERPStatus.NOT_STARTED
                claim.updated_at = datetime.now(UTC)
                claim.documents[0].sha256 = sha256(f.model_dump_json().encode()).hexdigest()
                claim.documents[0].parse_status = "UNPARSED"
                facts.db.execute(
                    "UPDATE claims SET body=? WHERE id=?", (claim.model_dump_json(), cid)
                )
                facts.db.execute(
                    "UPDATE submissions SET payload=? WHERE claim_id=?", (f.model_dump_json(), cid)
                )
                version = row["version"] + 1
                facts.db.execute(
                    "UPDATE enterprise_claims SET stage='AI_AUDITING',version=?,"
                    "revision=revision+1,reason='' WHERE claim_id=?",
                    (version, cid),
                )
                facts.db.execute(
                    "UPDATE p4a_claims SET body=?,report=NULL WHERE claim_id=?",
                    (json.dumps(snapshot, ensure_ascii=False), cid),
                )
                facts.db.execute(
                    "INSERT INTO p4a_claim_versions VALUES(?,?,?)",
                    (cid, version, json.dumps(snapshot, ensure_ascii=False)),
                )
                history = dict(
                    version=version,
                    fields=f.model_dump(mode="json"),
                    submitted_at=timestamp(),
                    stage="AI_AUDITING",
                    invoice_evidence=repo.invoice_store.evidence(cid),
                    trusted_facts=snapshot,
                )
                facts.db.execute(
                    "INSERT INTO enterprise_versions VALUES(?,?,?)",
                    (cid, version, json.dumps(history, ensure_ascii=False)),
                )
                enterprise.remember(scope, body.idempotency_key, fingerprint, cid)
                enterprise.event(p, "P4A_RESUBMITTED", cid, version=version, trusted_facts=snapshot)
            background.add_task(app.state.run_audit, cid, version)
            return dict(claim_id=cid, schema_version="p4a.1")
