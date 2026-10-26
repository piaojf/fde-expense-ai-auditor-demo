"""Separate append-only shadow results with durable run identity and process exclusion."""

import fcntl
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from evals.shadow_mode.schema import ShadowResult, timestamp


class ShadowStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Refuse an existing business database before executing any schema/write statement.
        if self.path.exists():
            with sqlite3.connect(f"file:{self.path}?mode=ro", uri=True) as check:
                tables = {r[0] for r in check.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                if tables and tables != {"shadow_runs", "shadow_results"}:
                    raise ValueError("Not a Shadow Store; business database is protected")
        self.db = sqlite3.connect(self.path)
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS shadow_runs "
                "(run_id TEXT PRIMARY KEY, manifest TEXT NOT NULL, summary TEXT)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS shadow_results "
                "(run_id TEXT, case_id TEXT, body TEXT NOT NULL, PRIMARY KEY(run_id,case_id), "
                "FOREIGN KEY(run_id) REFERENCES shadow_runs(run_id))"
            )
        self.db.execute("PRAGMA foreign_keys=ON")

    @contextmanager
    def exclusive(self):
        # OS releases this lock on process exit, including abrupt termination.
        with Path(str(self.path) + ".lock").open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def begin(self, run_id, manifest, *, resume=False):
        encoded = json.dumps(manifest, sort_keys=True, allow_nan=False)
        previous = self.db.execute(
            "SELECT manifest, summary FROM shadow_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if previous:
            if not resume:
                raise ValueError("Duplicate Shadow Run; use explicit resume or a new Run ID")
            if previous[0] != encoded:
                raise ValueError("Run manifest conflict; dataset/policy/code/selection changed")
            return json.loads(previous[1])["started_at"]
        else:
            if resume:
                raise ValueError("Cannot resume an unknown Shadow Run")
            started = timestamp().isoformat()
            with self.db:
                self.db.execute("INSERT INTO shadow_runs VALUES (?, ?, ?)", (
                    run_id, encoded, json.dumps({"started_at": started, "status": "RUNNING"}),
                ))
            return started

    def get(self, run_id, case_id):
        row = self.db.execute(
            "SELECT body FROM shadow_results WHERE run_id=? AND case_id=?", (run_id, case_id)
        ).fetchone()
        return ShadowResult.model_validate_json(row[0]) if row else None

    def save(self, result):
        # No upsert/update: completed case records are immutable across resume/repeat.
        with self.db:
            self.db.execute("INSERT INTO shadow_results VALUES (?, ?, ?)", (
                str(result.shadow_run_id), result.case_id, result.model_dump_json(),
            ))

    def finish(self, run_id, summary):
        with self.db:
            self.db.execute("UPDATE shadow_runs SET summary=? WHERE run_id=?", (
                json.dumps(summary, ensure_ascii=False, allow_nan=False), run_id,
            ))

    def close(self):
        self.db.close()
