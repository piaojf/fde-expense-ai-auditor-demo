"""Opt-in: disposable DB named fde_m0_test_* with pgvector available."""

import os
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from database.models import Base

URL = os.getenv("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set; real pgvector DB not verified"),
]


def test_real_postgres_migration_schema_vector_and_constraints():
    url = make_url(URL)
    assert (url.database or "").startswith("fde_m0_test_"), "Use a disposable fde_m0_test_* DB"
    config = Config("alembic.ini")
    config.attributes["database_url"] = URL
    engine = create_engine(URL)
    # Never downgrade or migrate a nonempty DB supplied by accident.
    with engine.connect() as conn:
        assert inspect(conn).get_table_names() == [], "Expected an empty test database"
    try:
        command.upgrade(config, "head")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
            assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
            assert conn.scalar(text("SELECT '[1,2,3]'::vector <-> '[1,2,3]'::vector")) == 0
        with engine.begin() as conn:
            conn.execute(
                Base.metadata.tables["claims"]
                .insert()
                .values(
                    claim_id="integration",
                    legal_entity="DEMO-CN",
                    employee_id="employee",
                    employee_snapshot={},
                    expense_snapshot={},
                    amount=Decimal("800.00"),
                    incurred_on=date(2026, 10, 1),
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
            assert conn.scalar(text("SELECT amount FROM claims")) == Decimal("800.00")
        command.downgrade(config, "base")
        with engine.connect() as conn:
            assert set(inspect(conn).get_table_names()) == {"alembic_version"}
    finally:
        engine.dispose()
