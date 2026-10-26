"""Durable HTTP attempt reservations. No URLs, headers, bodies or credentials stored."""

import json
import sqlite3
import threading
from pathlib import Path


class PilotStopped(Exception):
    pass


class HTTPBudget:
    def __init__(self, path, identity, *, limit=30):
        if type(limit) is not int or not 1 <= limit <= 30:
            raise ValueError("Invalid HTTP cap")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA synchronous=FULL")
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS metadata (identity TEXT, cap INTEGER)")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS attempts ("
                "id INTEGER PRIMARY KEY, provider TEXT, case_id TEXT, "
                "http_status INTEGER, error_type TEXT, evidence TEXT)"
            )
            self.db.execute("CREATE TABLE IF NOT EXISTS stop (reason TEXT)")
            stored = self.db.execute("SELECT identity,cap FROM metadata").fetchone()
            if stored is None:
                self.db.execute("INSERT INTO metadata VALUES (?,?)", (identity, limit))
            elif stored != (identity, limit):
                raise ValueError("Budget identity/cap conflict")
        self.limit = limit

    def counts(self):
        return {
            p: self.db.execute("SELECT count(*) FROM attempts WHERE provider=?", (p,)).fetchone()[0]
            for p in ("deepseek", "bge-m3")
        }

    def stop(self, reason):
        with self.lock, self.db:
            self.db.execute("INSERT INTO stop VALUES (?)", (reason,))

    def reserve(self, provider, case_id):
        if provider not in ("deepseek", "bge-m3"):
            raise ValueError("Unbudgeted provider")
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if self.db.execute("SELECT reason FROM stop LIMIT 1").fetchone():
                    raise PilotStopped("PILOT_ALREADY_STOPPED")
                if any(n >= self.limit for n in self.counts().values()):
                    raise PilotStopped("HTTP_BUDGET_EXHAUSTED")
                cursor = self.db.execute(
                    "INSERT INTO attempts(provider,case_id) VALUES (?,?)", (provider, case_id)
                )
                self.db.commit()  # Commit before HTTP; uncertain/crashed calls still consume cap.
                return cursor.lastrowid
            except BaseException:
                self.db.rollback()
                raise

    def record(self, attempt, *, status=None, error=None, evidence=None):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE attempts SET http_status=COALESCE(?,http_status),"
                "error_type=COALESCE(?,error_type),evidence=COALESCE(?,evidence) "
                "WHERE id=?",
                (status, error, json.dumps(evidence) if evidence else None, attempt),
            )

    def records(self, case_id=None):
        sql = "SELECT id,provider,case_id,http_status,error_type,evidence FROM attempts"
        args = ()
        if case_id:
            sql += " WHERE case_id=?"
            args = (case_id,)
        return [
            dict(
                zip(
                    ("id", "provider", "case_id", "http_status", "error_type", "evidence"),
                    row,
                    strict=True,
                )
            )
            for row in self.db.execute(sql, args)
        ]

    def close(self):
        self.db.close()


class HTTPHooks:
    """Per-case hooks preserve httpx default transports and existing proxy inheritance."""

    def __init__(self, budget, provider, case_id):
        self.budget, self.provider, self.case_id = budget, provider, case_id
        self.last_id = None

    def request(self, request):
        self.last_id = self.budget.reserve(self.provider, self.case_id)
        request.extensions["pilot_attempt_id"] = self.last_id

    def response(self, response):
        self.budget.record(
            response.request.extensions["pilot_attempt_id"], status=response.status_code
        )
        if response.status_code in (401, 403):
            self.budget.stop("AUTH_REJECTED")

    async def async_request(self, request):
        self.request(request)

    async def async_response(self, response):
        self.response(response)

    def hooks(self, *, asynchronous=False):
        return {
            "request": [self.async_request if asynchronous else self.request],
            "response": [self.async_response if asynchronous else self.response],
        }
