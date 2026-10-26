"""SQLite-linked local invoice evidence with recoverable file lifecycle.

Controlled single-worker demo. Capabilities scope drafts, not production users.
"""

import hashlib
import hmac
import json
import os
import re
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from os import open as open_directory
from pathlib import Path
from uuid import UUID, uuid4

from apps.api.errors import APIError

MAX_FILE = 10 * 1024 * 1024
REQUIRED = ("invoice_number", "issue_date", "seller_name", "buyer_name", "total_amount")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def norm(value):
    return re.sub(r"\s+", "", str(value or "")).casefold()


class InvoiceStore:
    def __init__(self, repository, processor):
        self.repo, self.processor = repository, processor
        self.db, self.lock = repository._db, repository._lock
        checkpoint = Path(repository.checkpoint_path)
        self.root = checkpoint.parent / (checkpoint.name + ".invoices")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        with self.lock, self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS invoice_drafts "
                "(id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, claim_id TEXT)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS invoice_attachments "
                "(id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, sha256 TEXT NOT NULL, "
                "state TEXT NOT NULL, metadata TEXT NOT NULL, ocr TEXT)"
            )
            self.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS invoice_active_draft "
                "ON invoice_attachments(draft_id) WHERE state IN ('STAGING','READY','MISSING')"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS invoice_evidence "
                "(claim_id TEXT PRIMARY KEY, draft_id TEXT UNIQUE NOT NULL, body TEXT NOT NULL)"
            )
        self.recover()

    def path(self, attachment_id, suffix=".blob"):
        # Generated UUID identity, never metadata filename or caller filesystem path.
        return self.root / (str(UUID(str(attachment_id))) + suffix)

    def sync_dir(self):
        fd = open_directory(self.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def recover(self):
        with self.lock, self.db:
            rows = self.db.execute("SELECT id,state,sha256 FROM invoice_attachments").fetchall()
            registered = {row[0] for row in rows}
            for aid, state, expected in rows:
                final, part = self.path(aid), self.path(aid, ".part")
                if state == "STAGING":
                    if not final.exists() and part.exists():
                        os.replace(part, final)
                        self.sync_dir()
                    state = "READY" if final.exists() and digest(final) == expected else "MISSING"
                    self.db.execute(
                        "UPDATE invoice_attachments SET state=? WHERE id=?", (state, aid)
                    )
                if state == "READY" and (not final.exists() or digest(final) != expected):
                    self.db.execute(
                        "UPDATE invoice_attachments SET state='MISSING' WHERE id=?", (aid,)
                    )
                if state == "DELETED":
                    final.unlink(missing_ok=True)
                    part.unlink(missing_ok=True)
            # Only app-generated temporary files in this store-owned directory.
            for part in self.root.glob("*.part"):
                if part.stem not in registered:
                    part.unlink(missing_ok=True)

    def draft(self, draft_id, token, *, mutable=False):
        did = str(draft_id)
        row = self.db.execute(
            "SELECT token_hash,claim_id FROM invoice_drafts WHERE id=?", (did,)
        ).fetchone()
        token_hash = hashlib.sha256(str(token).encode()).hexdigest()
        if row is None or not hmac.compare_digest(row[0], token_hash):
            raise APIError(404, "DRAFT_NOT_FOUND", "Draft not found")
        if mutable and row[1]:
            raise APIError(409, "DRAFT_SUBMITTED", "Submitted invoice is immutable")
        return row[1]

    def reserve(self, request):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO invoice_drafts VALUES (?, ?, NULL)",
                (str(request.draft_id), hashlib.sha256(str(request.token).encode()).hexdigest()),
            )
            return self.read(request.draft_id, request.token)

    def attachment(self, draft_id, attachment_id=None):
        where, args = ("draft_id=? AND state!='DELETED'", (str(draft_id),))
        if attachment_id:
            where += " AND id=?"
            args += (str(attachment_id),)
        row = self.db.execute(
            "SELECT id,state,metadata,ocr FROM invoice_attachments WHERE " + where, args
        ).fetchone()
        if not row:
            raise APIError(404, "ATTACHMENT_NOT_FOUND", "Attachment not found")
        return {
            **json.loads(row[2]),
            "state": row[1],
            "ocr": json.loads(row[3]) if row[3] else None,
        }

    def verified_path(self, attachment):
        path = self.path(attachment["attachment_id"])
        if (
            attachment["state"] != "READY"
            or not path.is_file()
            or path.is_symlink()
            or digest(path) != attachment["sha256"]
        ):
            raise APIError(409, "ATTACHMENT_INTEGRITY", "Attachment unavailable or changed")
        return path

    def read(self, draft_id, token):
        with self.lock:
            cid = self.draft(draft_id, token)
            try:
                attachment = self.attachment(draft_id)
            except APIError:
                attachment = None
            return {"draft_id": str(draft_id), "claim_id": cid, "attachment": attachment}

    def upload(self, draft_id, token, part, filename, decoded):
        expected, size = digest(part), part.stat().st_size
        # The temp identity is generated by this server during bounded streaming.
        aid = part.stem
        name = re.split(r"[/\\]", filename or "invoice")[-1]
        name = re.sub(r"[\x00-\x1f\x7f]", "", name)[:150] or "invoice"
        metadata = dict(
            attachment_id=aid,
            draft_id=str(draft_id),
            file_name=name,
            content_type=decoded["content_type"],
            pages=decoded["pages"],
            size=size,
            sha256=expected,
            created_at=datetime.now(UTC).isoformat(),
        )
        with self.lock:
            self.draft(draft_id, token, mutable=True)
            try:
                prior = self.attachment(draft_id)
            except APIError:
                prior = None
            if prior:
                if prior["sha256"] != expected or prior["state"] != "READY":
                    raise APIError(409, "ATTACHMENT_EXISTS", "Delete existing attachment first")
                self.verified_path(prior)
                part.unlink(missing_ok=True)
                return prior
            with self.db:
                self.db.execute(
                    "INSERT INTO invoice_attachments VALUES (?,?,?,'STAGING',?,NULL)",
                    (aid, str(draft_id), expected, json.dumps(metadata)),
                )
            # STAGING is durable before rename. Recovery handles any interruption below.
            os.replace(part, self.path(aid))
            self.sync_dir()
            with self.db:
                self.db.execute("UPDATE invoice_attachments SET state='READY' WHERE id=?", (aid,))
            return self.attachment(draft_id, aid)

    def delete(self, draft_id, aid, token):
        with self.lock:
            self.draft(draft_id, token, mutable=True)
            row = self.db.execute(
                "SELECT state FROM invoice_attachments WHERE id=? AND draft_id=?",
                (str(aid), str(draft_id)),
            ).fetchone()
            if not row:
                raise APIError(404, "ATTACHMENT_NOT_FOUND", "Attachment not found")
            with self.db:
                self.db.execute(
                    "UPDATE invoice_attachments SET state='DELETED' WHERE id=?", (str(aid),)
                )
            self.path(aid).unlink(missing_ok=True)
            self.path(aid, ".part").unlink(missing_ok=True)
            self.sync_dir()
            return {"deleted": True}

    def recognize(self, draft_id, aid, token, provider, *, refresh=False):
        with self.lock:
            self.draft(draft_id, token, mutable=True)
            attachment = self.attachment(draft_id, aid)
            path = self.verified_path(attachment)
            if attachment["ocr"] is not None and not refresh:
                return attachment
        # Never hold the business DB lock during a potentially slow OCR engine call.
        result = provider.recognize(str(path))
        with self.lock:
            self.draft(draft_id, token, mutable=True)
            current = self.attachment(draft_id, aid)
            self.verified_path(current)
            # Preserve a newer concurrent recognition, and retain the old result
            # if the provider failed. Submitted drafts are checked again above.
            if current["ocr"] == attachment["ocr"]:
                with self.db:
                    self.db.execute(
                        "UPDATE invoice_attachments SET ocr=? WHERE id=?",
                        (result.model_dump_json(), str(aid)),
                    )
            return self.attachment(draft_id, aid)

    def preview(self, attachment):
        with self.lock:
            path = self.verified_path(attachment)
        data = self.processor.preview(path)
        with self.lock:
            self.verified_path(attachment)
        return data

    def checks(self, attachment, fields, confirmed, *, exclude_claim=None, claim_ids=None):
        recognized = {key: value["value"] for key, value in attachment["ocr"]["fields"].items()}
        corrected = confirmed.model_dump(mode="json")
        warnings = []

        def warn(code, message):
            if code not in {w["code"] for w in warnings}:
                warnings.append({"code": code, "message": message})

        for key in REQUIRED:
            if not corrected[key] or not recognized.get(key):
                warn("MISSING_" + key.upper(), f"发票字段 {key} 缺失或识别不确定，请人工核实")
        for source, values in (("识别", recognized), ("确认", corrected)):
            amount = values.get("total_amount")
            if amount and Decimal(amount) != fields.amount:
                warn(
                    "AMOUNT_MISMATCH",
                    f"{source}发票金额 {amount} 与申报金额 {fields.amount} 不一致",
                )
            if values.get("seller_name") and norm(values["seller_name"]) != norm(fields.seller):
                warn("SELLER_MISMATCH", f"{source}销售方与申报开票单位不一致")
            issue = values.get("issue_date")
            if issue:
                today = datetime.now(UTC).astimezone(timezone(timedelta(hours=8))).date()
                parsed = date.fromisoformat(issue)
                if parsed > today or abs((parsed - fields.incurred_on).days) > 90:
                    warn(
                        "ISSUE_DATE_UNUSUAL", f"{source}开票日期需核实（未来或距费用日期超过90天）"
                    )
        for key, original in recognized.items():
            if original and norm(original) != norm(corrected.get(key)):
                warn("OCR_CORRECTED", "已人工修正识别结果，请核实修正原因")
        pairs = {
            (norm(v.get("invoice_number")), norm(v.get("seller_name")))
            for v in (recognized, corrected)
            if v.get("invoice_number") and v.get("seller_name")
        }
        for cid, body in self.db.execute("SELECT claim_id,body FROM invoice_evidence").fetchall():
            if cid == exclude_claim or (claim_ids is not None and cid not in claim_ids):
                continue
            evidence = json.loads(body)
            old = evidence["confirmed_fields"]
            old_raw = {k: v["value"] for k, v in evidence["attachment"]["ocr"]["fields"].items()}
            old_pairs = {
                (norm(v.get("invoice_number")), norm(v.get("seller_name")))
                for v in (old, old_raw)
                if v.get("invoice_number") and v.get("seller_name")
            }
            if pairs & old_pairs or attachment["sha256"] == evidence["attachment"]["sha256"]:
                warn(
                    "DUPLICATE_INVOICE_WARNING",
                    "发票号码及销售方或相同文件已有使用记录；仅为风险提示，需核实",
                )
        return warnings

    def bind(self, request, claim, *, replay=False, duplicate_claim_ids=None):
        """Called inside create_submission's BEGIN IMMEDIATE. No inner commit."""
        cid = self.draft(request.draft_id, request.token)
        canonical = request.model_dump_json(exclude={"token", "idempotency_key"})
        prior = self.evidence(claim.claim_id)
        if prior:
            if prior["confirmation_payload"] != canonical:
                raise APIError(409, "IDEMPOTENCY_CONFLICT", "Different invoice confirmation")
            self.verified_path(prior["attachment"])
            return
        if replay:
            raise APIError(409, "IDEMPOTENCY_CONFLICT", "Key belongs to a non-invoice claim")
        if cid:
            raise APIError(409, "DRAFT_SUBMITTED", "Draft already bound to a claim")
        attachment = self.attachment(request.draft_id, request.attachment_id)
        self.verified_path(attachment)
        if attachment["ocr"] is None:
            raise APIError(409, "OCR_REQUIRED", "Recognize and confirm the invoice first")
        warnings = self.checks(attachment, request, request.confirmed_fields,
                               claim_ids=duplicate_claim_ids)
        if warnings and not request.discrepancy_reason.strip():
            raise APIError(
                422, "EXPLANATION_REQUIRED", "Explain invoice warnings before submission"
            )
        evidence = dict(
            attachment=attachment,
            confirmed_fields=request.confirmed_fields.model_dump(mode="json"),
            discrepancy_reason=request.discrepancy_reason.strip(),
            warnings=warnings,
            confirmed_at=datetime.now(UTC).isoformat(),
            confirmation_payload=canonical,
            invoice_verified=False,
        )
        self.db.execute(
            "INSERT INTO invoice_evidence VALUES (?,?,?)",
            (claim.claim_id, str(request.draft_id), json.dumps(evidence)),
        )
        self.db.execute(
            "UPDATE invoice_drafts SET claim_id=? WHERE id=?",
            (claim.claim_id, str(request.draft_id)),
        )
        from domain.schemas import AuditEvent

        event = AuditEvent(
            event_id=uuid4(),
            claim_id=claim.claim_id,
            run_id=uuid4(),
            event_type="INVOICE_CONFIRMED",
            actor="api:invoice",
            occurred_at=datetime.now(UTC),
            payload={
                "attachment_id": str(request.attachment_id),
                "sha256": attachment["sha256"],
                "warning_codes": [w["code"] for w in warnings],
                "invoice_verified": False,
            },
        )
        self.db.execute(
            "INSERT INTO events (claim_id,body) VALUES (?,?)",
            (claim.claim_id, event.model_dump_json()),
        )

    def evidence(self, cid):
        with self.lock:
            row = self.db.execute(
                "SELECT body FROM invoice_evidence WHERE claim_id=?", (cid,)
            ).fetchone()
            return json.loads(row[0]) if row else None

    def workflow_hints(self, cid):
        evidence = self.evidence(cid)
        if evidence is None:
            return None
        try:
            self.verified_path(evidence["attachment"])
            integrity = True
        except APIError:
            integrity = False
        return {"integrity": integrity, "warnings": [w["code"] for w in evidence["warnings"]]}
