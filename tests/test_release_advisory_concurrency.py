"""Advisory display must serialize access to the shared SQLite connection."""

from types import SimpleNamespace

from apps.api.advisory import AdvisoryCatalog
from apps.api.compliance import ComplianceStore
from apps.api.repository import MockClaimRepository


class LockedConnection:
    def __init__(self, connection, lock):
        self.connection, self.lock = connection, lock

    def execute(self, *args):
        assert self.lock._is_owned(), "shared SQLite read requires repository lock"
        return self.connection.execute(*args)


def test_advisory_stage_read_uses_shared_connection_lock(tmp_path):
    repo = MockClaimRepository(store_path=str(tmp_path / "advisory.sqlite3"))
    enterprise = SimpleNamespace(db=repo._db, lock=repo._lock)
    store = ComplianceStore(enterprise)
    catalog = AdvisoryCatalog.__new__(AdvisoryCatalog)
    catalog.store, catalog.repo = store, repo
    try:
        with repo._lock, repo._db:
            repo._db.execute(
                "CREATE TABLE enterprise_claims (claim_id TEXT PRIMARY KEY, stage TEXT)"
            )
            repo._db.execute("INSERT INTO enterprise_claims VALUES ('claim', 'FINANCE_REVIEW')")
            store.append("ADVISORY", "claim:run", {"binding": {}})
        enterprise.db = LockedConnection(repo._db, repo._lock)
        report = catalog.advisory("claim", "run")
        assert report["finance_decision_status"] == "PENDING"
        assert report["manual_review"] is None
    finally:
        repo.close()
