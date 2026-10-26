"""Append-only trusted master data and claim snapshots inside the existing SQLite transaction."""

import json
from uuid import uuid4

from apps.api.enterprise_store import timestamp
from apps.api.policy_publication_store import fail


class HotelFacts:
    def __init__(self, repo):
        self.repo, self.db, self.lock = repo, repo._db, repo._lock
        with self.lock, self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS p4a_facts "
                "(id TEXT PRIMARY KEY,company TEXT,kind TEXT,key TEXT,"
                "start TEXT,end TEXT,body TEXT)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS p4a_claims "
                "(claim_id TEXT PRIMARY KEY,body TEXT NOT NULL,report TEXT)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS p4a_reports "
                "(claim_id TEXT,run_id TEXT,body TEXT NOT NULL,"
                "PRIMARY KEY(claim_id,run_id))"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS p4a_claim_versions "
                "(claim_id TEXT,version INTEGER,body TEXT NOT NULL,"
                "PRIMARY KEY(claim_id,version))"
            )
            for table in ("p4a_facts", "p4a_reports", "p4a_claim_versions"):
                for action in ("UPDATE", "DELETE"):
                    self.db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_{action}_immutable "
                        f"BEFORE {action} ON {table} BEGIN "
                        "SELECT RAISE(ABORT,'P4A_FACTS_IMMUTABLE'); END"
                    )

    def add(self, company, actor, body):
        record = {
            **body.model_dump(mode="json"),
            "id": str(uuid4()),
            "actor": actor,
            "recorded_at": timestamp(),
            "company": company,
        }
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            overlap = self.db.execute(
                "SELECT 1 FROM p4a_facts WHERE company=? AND kind=? "
                "AND key=? AND start<? AND end>?",
                (company, body.kind, body.key, str(body.effective_to), str(body.effective_from)),
            ).fetchone()
            if overlap:
                fail("FACT_INTERVAL_CONFLICT", "主数据有效期重叠，禁止随机选择")
            self.db.execute(
                "INSERT INTO p4a_facts VALUES(?,?,?,?,?,?,?)",
                (
                    record["id"],
                    company,
                    body.kind,
                    body.key,
                    str(body.effective_from),
                    str(body.effective_to),
                    json.dumps(record, ensure_ascii=False),
                ),
            )
        return record

    def list(self, company):
        with self.lock:
            return [
                json.loads(r[0])
                for r in self.db.execute(
                    "SELECT body FROM p4a_facts WHERE company=? ORDER BY kind,key,start", (company,)
                )
            ]

    def resolve(self, company, kind, key, on):
        with self.lock:
            rows = self.db.execute(
                "SELECT body FROM p4a_facts WHERE company=? AND kind=? "
                "AND key=? AND start<=? AND end>?",
                (company, kind, key, str(on), str(on)),
            ).fetchall()
        return json.loads(rows[0][0]) if len(rows) == 1 else None

    def snapshot(self, user, body):
        f = body.fields
        grade = self.resolve(user["company_id"], "EMPLOYEE", user["id"], f.incurred_on)
        stay = self.resolve(user["company_id"], "STAY", body.stay_id, f.incurred_on)
        city = (
            self.resolve(user["company_id"], "CITY", stay["value"]["city_key"], f.incurred_on)
            if stay
            else None
        )
        blockers = []
        if not grade:
            blockers.append("EMPLOYEE_GRADE_UNKNOWN")
        if not city:
            blockers.append("CITY_TIER_UNKNOWN")
        if (
            not stay
            or stay["value"]["employee_id"] != user["id"]
            or (
                stay["value"]["check_in"] != str(f.trip_start)
                or stay["value"]["check_out"] != str(f.trip_end)
            )
        ):
            blockers.append("STAY_UNVERIFIED")
        if city and f.city not in [city["value"]["name"], *city["value"].get("aliases", [])]:
            blockers.append("STAY_CITY_MISMATCH")
        if grade and not (str(f.trip_end) <= grade["effective_to"]):
            blockers.append("GRADE_CHANGES_DURING_STAY")
        if stay and str(f.trip_end) > stay["effective_to"]:
            blockers.append("STAY_EVIDENCE_EXPIRED")
        if city and not (str(f.trip_end) <= city["effective_to"]):
            blockers.append("CITY_CHANGES_DURING_STAY")
        return dict(
            employee=grade,
            city=city,
            stay=stay,
            blockers=blockers,
            declaration=body.model_dump(mode="json"),
            company=user["company_id"],
        )

    def read(self, cid):
        with self.lock:
            row = self.db.execute(
                "SELECT body,report FROM p4a_claims WHERE claim_id=?", (cid,)
            ).fetchone()
        return (json.loads(row[0]), json.loads(row[1]) if row[1] else None) if row else None

    def save_report(self, cid, report, run_id):
        with self.lock, self.db:
            body = json.dumps(report, ensure_ascii=False, sort_keys=True)
            prior = self.db.execute(
                "SELECT body FROM p4a_reports WHERE claim_id=? AND run_id=?", (cid, str(run_id))
            ).fetchone()
            if prior and prior[0] != body:
                raise ValueError("Pinned P4A report changed; original retained")
            if not prior:
                self.db.execute("INSERT INTO p4a_reports VALUES(?,?,?)", (cid, str(run_id), body))
            self.db.execute("UPDATE p4a_claims SET report=? WHERE claim_id=?", (body, cid))

    def reports(self, cid):
        with self.lock:
            return [
                json.loads(r[0])
                for r in self.db.execute("SELECT body FROM p4a_reports WHERE claim_id=?", (cid,))
            ]
