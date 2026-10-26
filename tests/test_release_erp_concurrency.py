"""Identical Mock ERP requests across independent SQLite connections must replay."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from apps.api.repository import MockClaimRepository


class CoordinatedConnection:
    """Expose the real pre-write race without replacing SQLite or its transactions."""

    def __init__(self, connection, barrier):
        self.connection, self.barrier = connection, barrier

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, *args):
        cursor = self.connection.execute(sql, *args)
        if "WHERE idempotency_key=?" in sql and not self.connection.in_transaction:
            # Reads without a writer transaction can observe the same absent key.
            self.barrier.wait(timeout=5)
        return cursor


def test_same_erp_key_across_repository_connections_replays(tmp_path):
    path = str(tmp_path / "erp.sqlite3")
    repos = [MockClaimRepository(store_path=path) for _ in range(2)]
    barrier = Barrier(2)
    for repo in repos:
        repo._db = CoordinatedConnection(repo._db, barrier)
    cid = repos[0].list()[0].claim_id
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(lambda repo: repo.write_erp(cid, "APPROVED", key="same"), repos)
            )
        assert sum(result["idempotent_replay"] for result in results) == 1
        assert len(repos[0].erp_effects(cid)) == repos[0].erp_write_count(cid) == 1
    finally:
        for repo in repos:
            repo.close()


def test_committed_erp_effect_survives_killed_process_and_replays(tmp_path):
    import subprocess
    import sys

    path = str(tmp_path / "crash.sqlite3")
    script = """
import sys, time
from apps.api.repository import MockClaimRepository
repo = MockClaimRepository(store_path=sys.argv[1])
cid = repo.list()[0].claim_id
repo.write_erp(cid, 'APPROVED', key='durable-effect')
print(cid, flush=True)
time.sleep(30)
"""
    worker = subprocess.Popen(
        [sys.executable, "-c", script, path], stdout=subprocess.PIPE, text=True, encoding="utf-8"
    )
    try:
        cid = worker.stdout.readline().strip()
        assert cid
        worker.kill()
        worker.wait(timeout=5)
        repo = MockClaimRepository(store_path=path)
        try:
            result = repo.write_erp(cid, "APPROVED", key="durable-effect")
            assert result["idempotent_replay"]
            assert len(repo.erp_effects(cid)) == repo.erp_write_count(cid) == 1
            assert repo._db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            repo.close()
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        worker.stdout.close()


def test_failed_effect_insert_rolls_back_then_retry_commits_once(tmp_path):
    import sqlite3

    import pytest

    repo = MockClaimRepository(store_path=str(tmp_path / "failure.sqlite3"))
    cid = repo.list()[0].claim_id
    try:
        with repo._db:
            repo._db.execute("""CREATE TRIGGER inject_failure BEFORE INSERT ON erp_writes
                                BEGIN SELECT RAISE(ABORT, 'synthetic DB failure'); END""")
        with pytest.raises(sqlite3.IntegrityError, match="synthetic DB failure"):
            repo.write_erp(cid, "APPROVED", key="db-failure")
        assert repo.erp_effects(cid) == []
        assert repo.erp_write_count(cid) == 0
        with repo._db:
            repo._db.execute("DROP TRIGGER inject_failure")
        repo.write_erp(cid, "APPROVED", key="db-failure")
        assert len(repo.erp_effects(cid)) == repo.erp_write_count(cid) == 1
    finally:
        repo.close()


def test_two_worker_processes_consume_same_erp_key_once(tmp_path):
    import json
    import subprocess
    import sys

    path = str(tmp_path / "workers.sqlite3")
    repo = MockClaimRepository(store_path=path)
    cid = repo.list()[0].claim_id
    repo.close()
    script = """
import json, sys
from apps.api.repository import MockClaimRepository
repo = MockClaimRepository(store_path=sys.argv[1])
print('READY', flush=True)
sys.stdin.readline()
result = repo.write_erp(sys.argv[2], 'APPROVED', key='shared-worker-key')
print(json.dumps(result), flush=True)
repo.close()
"""
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", script, path, cid],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        for _ in range(2)
    ]
    try:
        for worker in workers:
            assert worker.stdout.readline().strip() == "READY"
        for worker in workers:
            worker.stdin.write("GO\n")
            worker.stdin.flush()
        results = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=10)
            assert worker.returncode == 0, stderr
            results.append(json.loads(stdout))
        assert sum(r["idempotent_replay"] for r in results) == 1
        repo = MockClaimRepository(store_path=path)
        try:
            assert len(repo.erp_effects(cid)) == repo.erp_write_count(cid) == 1
        finally:
            repo.close()
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=5)
