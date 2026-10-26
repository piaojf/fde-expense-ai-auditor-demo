from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock

from domain.schemas import AuditEvent, ClaimDetail, ClaimStatus, OperationResponse


class MockClaimRepository:
    """Isolated mock storage; optional SQLite durability, never a real ERP/database claim."""

    def __init__(self, fixture_path: Path | None = None, store_path: str | None = None):
        path = fixture_path or Path(__file__).parent / "mock_data" / "claims.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        claims = {item["claim_id"]: ClaimDetail.model_validate(item) for item in raw}
        if len(claims) != len(raw):
            raise ValueError("Duplicate fixture claim IDs")
        if store_path:
            Path(store_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._checkpoint_temp = None if store_path else TemporaryDirectory(prefix="fde-checkpoint-")
        self.checkpoint_path = (
            str(Path(store_path).resolve()) + ".checkpoints.sqlite3"
            if store_path
            else str(Path(self._checkpoint_temp.name) / "checkpoints.sqlite3")
        )
        self._db = sqlite3.connect(store_path or ":memory:", check_same_thread=False)
        with self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS claims (id TEXT PRIMARY KEY, body TEXT)")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS submissions "
                "(claim_id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS events "
                "(seq INTEGER PRIMARY KEY, claim_id TEXT, body TEXT)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS operations "
                "(claim_id TEXT, key TEXT, payload TEXT, response TEXT, PRIMARY KEY(claim_id, key))"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS mock_erp (claim_id TEXT PRIMARY KEY, status TEXT)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS operation_v2 "
                "(claim_id TEXT, kind TEXT, key TEXT, payload TEXT, response TEXT, "
                "PRIMARY KEY(claim_id, kind, key))"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS runs "
                "(claim_id TEXT, run_id TEXT, body TEXT, PRIMARY KEY(claim_id, run_id))"
            )
            self._db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS event_identity "
                "ON events(json_extract(body, '$.event_id'))"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS erp_writes "
                "(claim_id TEXT, key TEXT, status TEXT, PRIMARY KEY(claim_id,key))"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS erp_effects "
                "(idempotency_key TEXT PRIMARY KEY, claim_id TEXT, operation TEXT, "
                "status TEXT, result TEXT, timestamp TEXT)"
            )
            for claim in claims.values():
                self._db.execute(
                    "INSERT OR IGNORE INTO claims VALUES (?, ?)",
                    (claim.claim_id, claim.model_dump_json()),
                )

    def get(self, claim_id: str) -> ClaimDetail | None:
        with self._lock:
            row = self._db.execute("SELECT body FROM claims WHERE id=?", (claim_id,)).fetchone()
        return ClaimDetail.model_validate_json(row[0]) if row else None

    def list(self, status: ClaimStatus | None = None) -> list[ClaimDetail]:
        with self._lock:
            rows = self._db.execute("SELECT body FROM claims ORDER BY id").fetchall()
        claims = [ClaimDetail.model_validate_json(row[0]) for row in rows]
        return [c for c in claims if status is None or c.status == status]

    def operation(self, claim_id: str, key: str, kind: str = "audit"):
        with self._lock:
            if kind in ("human-review", "resume") and hasattr(self, "finance_guard"):
                self.finance_guard(claim_id, kind)
            row = self._db.execute(
                "SELECT payload, response FROM operation_v2 WHERE claim_id=? AND kind=? AND key=?",
                (claim_id, kind, key),
            ).fetchone()
            if row is None and kind == "audit":
                row = self._db.execute(
                    "SELECT payload, response FROM operations WHERE claim_id=? AND key=?",
                    (claim_id, key),
                ).fetchone()
        return (row[0], OperationResponse.model_validate_json(row[1])) if row else None

    def save_operation(
        self,
        claim: ClaimDetail,
        key: str,
        payload: str,
        response: OperationResponse,
        events: list[AuditEvent] | None = None,
        *,
        kind: str = "audit",
        run_context: dict | None = None,
    ):
        # Claim, accepted/completed operation and append-only events share one commit.
        with self._lock, self._db:
            self._db.execute(
                "UPDATE claims SET body=? WHERE id=?", (claim.model_dump_json(), claim.claim_id)
            )
            if kind in ("human-review", "resume") and hasattr(self, "finance_guard"):
                # UPDATE above acquires SQLite's writer lock; check and commit cannot
                # race with revocation even from another repository/process connection.
                self.finance_guard(claim.claim_id, kind, run_id=response.run_id,
                                   review=(run_context or {}).get("human_review"))
            self._db.execute(
                "INSERT INTO operation_v2 VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(claim_id,kind,key) DO UPDATE SET response=excluded.response",
                (claim.claim_id, kind, key, payload, response.model_dump_json()),
            )
            if run_context is not None:
                self._db.execute(
                    "INSERT INTO runs VALUES (?, ?, ?) "
                    "ON CONFLICT(claim_id,run_id) DO UPDATE SET body=excluded.body",
                    (claim.claim_id, str(response.run_id), json.dumps(run_context)),
                )
            for event in events or []:
                self._db.execute(
                    "INSERT OR IGNORE INTO events (claim_id, body) VALUES (?, ?)",
                    (claim.claim_id, event.model_dump_json()),
                )

    def events(self, claim_id: str) -> list[AuditEvent]:
        with self._lock:
            rows = self._db.execute(
                "SELECT body FROM events WHERE claim_id=? ORDER BY seq", (claim_id,)
            ).fetchall()
        return [AuditEvent.model_validate_json(row[0]) for row in rows]

    def append_events(self, events):
        """Persist sanitized dependency diagnostics even before a node checkpoint completes."""
        with self._lock, self._db:
            for event in events:
                if event.event_type in ("HUMAN_APPROVED", "HUMAN_REJECTED", "WORKFLOW_RESUMED"):
                    if hasattr(self, "finance_guard"):
                        self._db.execute("UPDATE claims SET id=id WHERE id=?", (event.claim_id,))
                        self.finance_guard(event.claim_id, "event", run_id=event.run_id)
                self._db.execute(
                    "INSERT OR IGNORE INTO events (claim_id, body) VALUES (?, ?)",
                    (event.claim_id, event.model_dump_json()),
                )

    def run_context(self, claim_id, run_id):
        with self._lock:
            row = self._db.execute(
                "SELECT body FROM runs WHERE claim_id=? AND run_id=?", (claim_id, str(run_id))
            ).fetchone()
        return json.loads(row[0]) if row else None

    def write_erp(self, claim_id: str, status: str, key: str = "legacy",
                  operation: str = "write_decision"):
        with self._lock, self._db:
            if hasattr(self, "finance_guard"):
                self._db.execute("UPDATE claims SET id=id WHERE id=?", (claim_id,))
                self.finance_guard(claim_id, "erp", effect_key=key, effect_status=status,
                                   effect_operation=operation)
            prior_effect = self._db.execute(
                "SELECT claim_id, operation, status, result FROM erp_effects "
                "WHERE idempotency_key=?", (key,)
            ).fetchone()
            if prior_effect:
                if prior_effect[:3] != (claim_id, operation, status):
                    raise ValueError("ERP idempotency conflict")
                return {**json.loads(prior_effect[3]), "idempotent_replay": True}
            prior = self._db.execute(
                "SELECT status FROM erp_writes WHERE claim_id=? AND key=?", (claim_id, key)
            ).fetchone()
            if prior:
                if prior[0] != status:
                    raise ValueError("ERP idempotency conflict")
            result = {
                "source": "mock", "status": status, "claim_id": claim_id,
                "idempotency_key": key, "operation": operation,
                "timestamp": datetime.now(UTC).isoformat(),
            }
            self._db.execute(
                "INSERT INTO erp_effects VALUES (?, ?, ?, ?, ?, ?)",
                (key, claim_id, operation, status, json.dumps(result), result["timestamp"]),
            )
            if not prior:
                self._db.execute("INSERT INTO erp_writes VALUES (?, ?, ?)", (claim_id, key, status))
                self._db.execute(
                    "INSERT INTO mock_erp VALUES (?, ?) "
                    "ON CONFLICT(claim_id) DO UPDATE SET status=excluded.status",
                    (claim_id, status),
                )
            return {**result, "idempotent_replay": bool(prior)}

    def erp_effects(self, claim_id):
        with self._lock:
            rows = self._db.execute(
                "SELECT result FROM erp_effects WHERE claim_id=?", (claim_id,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def erp_status(self, claim_id: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT status FROM mock_erp WHERE claim_id=?", (claim_id,)
            ).fetchone()
        return row[0] if row else None

    def erp_write_count(self, claim_id: str) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT count(*) FROM erp_writes WHERE claim_id=?", (claim_id,)
            ).fetchone()[0]

    def close(self):
        self._db.close()
        if self._checkpoint_temp:
            self._checkpoint_temp.cleanup()

    def submission(self, claim_id: str):
        with self._lock:
            row = self._db.execute(
                "SELECT payload FROM submissions WHERE claim_id=?", (claim_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def create_submission(self, request, *, invoice_hook=None, employee=None):
        """Reserve the global key and declaration atomically, including its creation event."""
        from hashlib import sha256
        from uuid import uuid4

        from apps.api.errors import APIError
        from domain.schemas import Document, Employee, Expense
        from domain.submission import SubmissionFields

        fields = SubmissionFields.model_validate(request.model_dump(exclude={"idempotency_key"}))
        payload = fields.model_dump_json()
        with self._lock, self._db:
            # A SQLite write lock also serializes distinct repository/process connections.
            self._db.execute("BEGIN IMMEDIATE")
            prior = self._db.execute(
                "SELECT claim_id, payload FROM submissions WHERE key=?", (request.idempotency_key,)
            ).fetchone()
            if prior:
                if prior[1] != payload:
                    raise APIError(409, "IDEMPOTENCY_CONFLICT", "Key used with another submission")
                existing = self.get(prior[0])
                if invoice_hook:
                    invoice_hook(existing, replay=True)
                return existing
            now = datetime.now(UTC)
            claim_id = f"CLM-{now.year}-{uuid4().hex}"
            claim = ClaimDetail(
                claim_id=claim_id,
                employee=employee or Employee(
                    employee_id=f"EMP-{uuid4().hex}", name=fields.applicant_name,
                    department=fields.department, legal_entity="DEMO-CN",
                ),
                expense=Expense(
                    category=fields.category, city=fields.city, amount=fields.amount,
                    incurred_on=fields.incurred_on, nights=fields.nights or 1,
                ),
                status=ClaimStatus.NEW, created_at=now, updated_at=now,
                documents=[Document(
                    document_id=f"DECL-{uuid4().hex}", file_name="structured-submission.json",
                    content_type="application/json", sha256=sha256(payload.encode()).hexdigest(),
                )],
            )
            event = AuditEvent(
                event_id=uuid4(), claim_id=claim_id, run_id=uuid4(), event_type="CLAIM_CREATED",
                actor="api:submission", occurred_at=now,
                payload={"source": "structured-declaration", "invoice_verified": False,
                         "schema_version": "1.1.0"},
            )
            self._db.execute(
                "INSERT INTO claims VALUES (?, ?)", (claim_id, claim.model_dump_json())
            )
            self._db.execute(
                "INSERT INTO submissions VALUES (?, ?, ?)",
                (claim_id, request.idempotency_key, payload),
            )
            self._db.execute(
                "INSERT INTO events (claim_id, body) VALUES (?, ?)",
                (claim_id, event.model_dump_json()),
            )
            if invoice_hook:
                invoice_hook(claim, replay=False)
            return claim
