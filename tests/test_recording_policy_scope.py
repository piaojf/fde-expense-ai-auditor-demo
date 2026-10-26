from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session
from test_policies_postgres import policy_pg as policy_pg

from rag.retrieval.pgvector import scope_rows

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("category", ["HOTEL", "MEAL", "TRANSPORT"])
def test_future_ordinary_policy_cannot_apply_retroactively(policy_pg, category):
    with Session(policy_pg[0]) as session:
        rows = scope_rows(
            session,
            legal_entity="DEMO-CN",
            category=category,
            city="深圳",
            incurred_on=date(2023, 1, 1),
            audit_started_at=datetime.now(UTC),
        )
    assert rows == []
