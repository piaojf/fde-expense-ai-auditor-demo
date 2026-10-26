"""Reuse unchanged authority adversaries against the v2 protocol path."""

import os

import pytest
from advisory_v2_support import AdvisoryWireV2
from test_advisory_execution_postgres import (
    test_committed_response_loss_only_reconciles_existing_effect as reconcile,
)
from test_advisory_execution_postgres import (
    test_uncommitted_recovery_revalidates_execution_evidence as revalidate,
)
from test_advisory_postgres import advisory as advisory
from test_advisory_postgres import test_old_manual_review_cannot_approve_new_state as stale
from test_p4a_postgres import hotel_portal as hotel_portal
from test_policies_postgres import policy_pg as policy_pg

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="Disposable PostgreSQL required")]


def v2(advisory):
    env, _ = advisory
    provider = AdvisoryWireV2()
    env[1].state.audit_service.catalog.provider = provider
    return env, provider


@pytest.mark.parametrize("change", ["facts", "version", "run", "role_restore"])
def test_v2_stale_approval_and_restored_role_are_still_rejected(advisory, change):
    stale(v2(advisory), change)


@pytest.mark.parametrize("model_failure", [False, True])
@pytest.mark.parametrize("boundary", ["entry", "adapter"])
@pytest.mark.parametrize("mutation", ["invoice_missing", "facts"])
def test_v2_first_effect_rechecks_evidence(
    advisory, monkeypatch, model_failure, boundary, mutation,
):
    revalidate(v2(advisory), monkeypatch, model_failure, boundary, mutation)


@pytest.mark.parametrize("mutation", ["invoice_missing", "facts"])
def test_v2_existing_committed_effect_can_reconcile_only(advisory, monkeypatch, mutation):
    reconcile(v2(advisory), monkeypatch, mutation)
