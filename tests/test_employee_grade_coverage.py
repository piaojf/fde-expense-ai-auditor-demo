"""Continuous same-grade coverage without editing immutable source facts."""

import pytest

from apps.api.employee_hotel import EmployeeHotelFacts
from apps.api.repository import MockClaimRepository
from domain.employee_hotel import DeclarationSubmission
from domain.hotel_matrix import TrustedFact


@pytest.fixture
def facts(tmp_path):
    repo = MockClaimRepository(store_path=str(tmp_path / "grade.sqlite3"))
    yield EmployeeHotelFacts(repo)
    repo.close()


def add(facts, start, end, grade="D", company="DEMO-CN", employee="employee"):
    return facts.add(company, "admin", TrustedFact(
        kind="EMPLOYEE", key=employee, effective_from=start, effective_to=end,
        value={"grade": grade}, source="Synthetic authorized grade record",
        version="1", reason="Synthetic coverage verification",
    ))


def test_same_grade_extension_is_continuous_and_original_rows_unchanged(facts):
    original = add(facts, "2026-01-01", "2027-02-01")
    add(facts, "2022-01-01", "2026-01-01")
    add(facts, "2027-02-01", "2031-01-01")
    originals = facts.list("DEMO-CN")
    for day in ("2022-01-01", "2024-05-12", "2025-12-31", "2026-01-01",
                "2027-01-31", "2027-02-01", "2030-12-31"):
        record = facts.resolve("DEMO-CN", "EMPLOYEE", "employee", day)
        assert record["value"]["grade"] == "D"
        assert record["effective_from"] == "2022-01-01"
        assert record["effective_to"] == "2031-01-01"
        assert len(record["component_fact_ids"]) == 3
    assert facts.resolve("DEMO-CN", "EMPLOYEE", "employee", "2021-12-31") is None
    assert facts.resolve("DEMO-CN", "EMPLOYEE", "employee", "2031-01-01") is None
    assert facts.list("DEMO-CN") == originals
    assert original in originals


def test_grade_changes_and_gaps_do_not_merge(facts):
    add(facts, "2022-01-01", "2026-01-01", "C")
    middle = add(facts, "2026-01-01", "2027-02-01")
    add(facts, "2027-02-02", "2031-01-01")
    assert facts.resolve("DEMO-CN", "EMPLOYEE", "employee", "2026-05-01") == middle
    assert facts.resolve("DEMO-CN", "EMPLOYEE", "employee", "2027-02-01") is None


def test_company_and_account_boundaries_remain_separate(facts):
    expected = add(facts, "2026-01-01", "2027-02-01")
    add(facts, "2022-01-01", "2026-01-01", company="OTHER")
    add(facts, "2027-02-01", "2031-01-01", employee="someone-else")
    assert facts.resolve("DEMO-CN", "EMPLOYEE", "employee", "2026-05-01") == expected


@pytest.mark.parametrize("start,end", [
    ("2025-12-31", "2026-01-02"), ("2027-01-31", "2027-02-02"),
])
def test_stay_across_same_grade_record_boundary_has_no_grade_change_blocker(facts, start, end):
    add(facts, "2022-01-01", "2026-01-01")
    add(facts, "2026-01-01", "2027-02-01")
    add(facts, "2027-02-01", "2031-01-01")
    body = DeclarationSubmission.model_validate(dict(
        fields=dict(applicant_name="Synthetic Employee", department="Demo", position="Engineer",
                    category="HOTEL", amount="680.00", incurred_on=start, purpose="Demo meeting",
                    city="深圳", trip_start=start, trip_end=end, nights=2,
                    seller="Synthetic Hotel", notes=""),
        address="Synthetic address", idempotency_key="same-grade-boundary-coverage",
    ))
    snapshot = facts.snapshot(dict(company_id="DEMO-CN", id="employee"), body)
    assert snapshot["employee"]["value"]["grade"] == "D"
    assert "EMPLOYEE_GRADE_UNKNOWN" not in snapshot["blockers"]
    assert "GRADE_CHANGES_DURING_STAY" not in snapshot["blockers"]
