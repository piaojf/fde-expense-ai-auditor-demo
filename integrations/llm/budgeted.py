"""Reused durable call cap from the backed-up enterprise extension; no credentials stored."""

import sqlite3
import threading
from pathlib import Path

from integrations.llm.deepseek import DeepSeekError


class BudgetedLLM:
    def __init__(self, provider, path, maximum):
        if type(maximum) is not int or not 1 <= maximum <= 30:
            raise ValueError("Invalid LLM call cap")
        self.provider, self.maximum = provider, maximum
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        try:
            with self.db:
                self.db.execute("CREATE TABLE IF NOT EXISTS llm_cap (maximum INTEGER NOT NULL)")
                self.db.execute("CREATE TABLE IF NOT EXISTS llm_attempts (id INTEGER PRIMARY KEY)")
                current = self.db.execute("SELECT maximum FROM llm_cap").fetchone()
                if current and current[0] != maximum:
                    raise ValueError("Existing LLM cap cannot be changed")
                if not current:
                    self.db.execute("INSERT INTO llm_cap VALUES (?)", (maximum,))
        except BaseException:
            self.db.close()
            raise

    @property
    def model(self):
        return self.provider.model

    @property
    def protocol_version(self):
        return getattr(self.provider, "protocol_version", None)

    def usage(self):
        with self.lock:
            used = self.db.execute("SELECT count(*) FROM llm_attempts").fetchone()[0]
        return dict(maximum=self.maximum, used=used, remaining=max(0, self.maximum - used))

    async def analyze(self, request):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                used = self.db.execute("SELECT count(*) FROM llm_attempts").fetchone()[0]
                if used >= self.maximum:
                    raise DeepSeekError("LOCAL_AI_BUDGET_EXHAUSTED")
                self.db.execute("INSERT INTO llm_attempts DEFAULT VALUES")
                self.db.commit()  # Reserve before the call; failures and retries still count.
            except BaseException:
                self.db.rollback()
                raise
        return await self.provider.analyze(request)
