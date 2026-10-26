"""One final finance decision; authored AI, isolated stores and Mock ERP only."""

import os
from uuid import uuid4

import pytest
from advisory_support import prepare
from p4b_fix2_support import invoice, login
from test_employee_hotel import declaration
from test_hotel_demo_fixes import booking
from test_hotel_demo_fixes import demo as demo
from test_p4a_postgres import hotel_portal as hotel_portal
from test_p4b_fix2_postgres import COMMON
from test_policies_postgres import policy_pg as policy_pg

from domain.enterprise import FinanceAction
from integrations.erp.mock import MockERP
from integrations.faults import FailureMode
from integrations.retry import RetryPolicy

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv('TEST_DATABASE_URL'), reason='Disposable PostgreSQL required')]


def decide(client, detail, action='APPROVE', **changes):
    body = dict(action=action, reason='根据本次材料作出财务决定',
                expected_revision=detail['revision'], idempotency_key=str(uuid4()),
                advisory_report_digest=detail['advisory_analysis']['report_digest'])
    body.update(changes)
    return client.post('/api/v1.5/claims/' + detail['claim_id'] + '/finance-action', json=body)


def ready(demo, **changes):
    env, provider = demo
    booking(env)
    return env, provider, prepare(env, **changes)


def pending_employee_hotel(env):
    client = env[0]
    inv = invoice(client, dict(amount='680.00'))
    body = {**declaration(), 'amount': '680.00', 'check_out': '2026-11-03',
            'seller': 'Synthetic Shenzhen Hotel', 'invoice': inv}
    created = client.post('/api/v1.5/p4a/v2/claims', json=body)
    assert created.status_code == 202, created.text
    login(client, 'finance')
    return client.get('/api/v1.5/claims/' + created.json()['claim_id']).json()


@pytest.mark.parametrize('action,stage,writes', [
    ('APPROVE', 'APPROVED', 1), ('REJECT', 'REJECTED', 0), ('RETURN', 'RETURNED', 0),
])
def test_single_decision_without_separate_attestation(demo, action, stage, writes):
    env, provider, detail = ready(demo)
    client, app, repo = env[:3]
    calls = len(provider.requests)
    report = detail['advisory_analysis']
    assert report['manual_review'] is None
    assert report['input_facts']['candidate']['amount'] == '680.00'
    key = str(uuid4())
    first = decide(client, detail, action, idempotency_key=key)
    assert first.status_code == 200, first.text
    assert first.json()['stage'] == stage
    assert first.json()['advisory_analysis']['manual_review'] is None
    assert first.json()['advisory_analysis']['original_output'] == report['original_output']
    assert first.json()['history'][-1]['finance_reason'] == '根据本次材料作出财务决定'
    assert decide(client, detail, action, idempotency_key=key).status_code == 200
    assert repo.erp_write_count(detail['claim_id']) == writes
    assert len(provider.requests) == calls


def test_final_decision_cannot_override_failed_rule(demo):
    env, _, detail = ready(demo, amount='1200.00')
    assert decide(env[0], detail).status_code == 409
    assert env[2].erp_write_count(detail['claim_id']) == 0
    assert decide(env[0], detail, 'RETURN').status_code == 200


@pytest.mark.parametrize('case', ['invoice_unverified', 'stay_grade_unknown', 'ai_failed_unknown'])
def test_finance_can_approve_unknown_without_rewriting_evidence(demo, case):
    env, provider = demo
    if case == 'invoice_unverified':
        booking(env)
    if case == 'ai_failed_unknown':
        provider.mode = 'invalid_json'
    detail = (prepare(env, facts={k: v for k, v in COMMON.items() if k != 'invoice_verified'})
              if case == 'invoice_unverified' else pending_employee_hotel(env))
    report = detail['advisory_analysis']
    assert report['deterministic_status'] == 'UNKNOWN'
    if case == 'ai_failed_unknown':
        assert report['ai_advisory_status'] == 'FAILED'
    calls = len(provider.requests)
    reason = '人工核对原始票据、实际入住、有效职级及业务审批依据，确认可以报销。'
    key = str(uuid4())
    response = decide(env[0], detail, reason=reason, idempotency_key=key)
    assert response.status_code == 200, response.text
    final = response.json()
    assert final['stage'] == 'APPROVED'
    for field in ['deterministic_status', 'ai_advisory_status', 'original_output', 'rule_results']:
        assert final['advisory_analysis'][field] == report[field]
    assert final['history'][-1]['finance_reason'] == reason
    assert final['history'][-1]['reviewer_id'] == env[4]['finance']['id']
    approval = final['claim']['decision_result']['manual_approval']
    assert approval['reviewer_id'] == env[4]['finance']['id']
    assert decide(env[0], detail, reason=reason, idempotency_key=key).status_code == 200
    assert env[2].erp_write_count(detail['claim_id']) == 1
    assert len(provider.requests) == calls


def test_unknown_legacy_request_still_requires_resolved_evidence(demo):
    env, _ = demo
    detail = pending_employee_hotel(env)
    assert detail['advisory_analysis']['deterministic_status'] == 'UNKNOWN'
    assert decide(env[0], detail, advisory_report_digest=None).status_code == 409
    assert env[2].erp_write_count(detail['claim_id']) == 0


def test_manual_decision_cannot_override_unavailable_policy_source(demo, monkeypatch):
    env, _ = demo
    booking(env)
    def fail(*args, **kwargs):
        raise ValueError('synthetic policy retrieval failure')
    monkeypatch.setattr(env[1].state.audit_service.catalog.retriever, 'read', fail)
    detail = prepare(env)
    assert detail['advisory_analysis']['source_status'] == 'FAILED'
    assert decide(env[0], detail).status_code == 409
    assert env[2].erp_write_count(detail['claim_id']) == 0


def test_partial_ai_can_be_decided_by_finance_without_fabricating_success(demo):
    env, provider, detail = ready(demo, notes='SYNTHETIC_AI_FAILURE 本次建议校验失败')
    assert detail['advisory_analysis']['ai_advisory_status'] == 'PARTIAL'
    calls = len(provider.requests)
    response = decide(env[0], detail)
    assert response.status_code == 200, response.text
    assert response.json()['advisory_analysis']['ai_advisory_status'] == 'PARTIAL'
    assert env[2].erp_write_count(detail['claim_id']) == 1
    assert len(provider.requests) == calls


@pytest.mark.parametrize('action', ['APPROVE', 'REJECT', 'RETURN'])
def test_stale_report_cannot_receive_a_new_decision(demo, action):
    env, _, detail = ready(demo)
    response = decide(env[0], detail, action, advisory_report_digest='a' * 64)
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'ADVISORY_STALE'
    assert env[2].erp_write_count(detail['claim_id']) == 0
    assert env[0].get('/api/v1.5/claims/' + detail['claim_id']).json()['stage'] == 'FINANCE_REVIEW'


@pytest.mark.parametrize('role', ['employee', 'admin'])
def test_final_decision_still_requires_live_finance_role(demo, role):
    env, _, detail = ready(demo)
    login(env[0], role)
    assert decide(env[0], detail).status_code == 403
    assert env[2].erp_write_count(detail['claim_id']) == 0


def test_self_review_remains_forbidden(demo):
    env, _, detail = ready(demo)
    client, _, repo, store, users, _ = env
    with store.lock, store.db:
        store.db.execute('UPDATE enterprise_claims SET owner_id=? WHERE claim_id=?',
                         (users['finance']['id'], detail['claim_id']))
    assert decide(client, detail).status_code == 403
    assert repo.erp_write_count(detail['claim_id']) == 0


def test_blank_reason_and_stale_revision_remain_rejected(demo):
    env, _, detail = ready(demo)
    assert decide(env[0], detail, reason='   ').status_code == 422
    assert decide(env[0], detail, expected_revision=detail['revision'] + 1).status_code == 409
    assert env[2].erp_write_count(detail['claim_id']) == 0


@pytest.mark.parametrize('mutation', ['facts', 'revoked'])
@pytest.mark.parametrize('unknown', [False, True])
def test_first_erp_effect_rechecks_authority_and_evidence(demo, monkeypatch, mutation, unknown):
    if unknown:
        env, _ = demo
        detail = pending_employee_hotel(env)
    else:
        env, _, detail = ready(demo)
    client, app, repo, store, users, _ = env
    erp = MockERP(repo)
    app.state.audit_service.graph_factory.keywords['erp'] = erp
    original = erp.write_decision

    async def interleaved(*args, **kwargs):
        with store.lock, store.db:
            if mutation == 'revoked':
                store.db.execute('UPDATE enterprise_users SET enabled=0 WHERE id=?',
                                 (users['finance']['id'],))
            else:
                catalog = app.state.audit_service.catalog
                record = catalog.store.read('FACTS', detail['claim_id'])[1] or {}
                changed = {**record, 'facts': {**record.get('facts', {}), 'duplicate_clear': False}}
                catalog.store.append('FACTS', detail['claim_id'], changed)
        return await original(*args, **kwargs)

    monkeypatch.setattr(erp, 'write_decision', interleaved)
    result = decide(client, detail)
    assert result.status_code in (401, 403, 409), {
        'stage': result.json().get('stage'), 'error': result.json().get('error')}
    assert repo.erp_write_count(detail['claim_id']) == 0


def test_direct_decision_recovers_pending_erp_without_duplicate(demo):
    env, _, detail = ready(demo)
    client, app, repo = env[:3]
    erp = MockERP(repo, failure_mode=FailureMode.TIMEOUT_ALWAYS)
    app.state.audit_service.graph_factory.keywords.update(
        erp=erp, retry_policies={'erp': RetryPolicy(max_attempts=1)})
    first = decide(client, detail)
    assert first.status_code == 200, first.text
    assert first.json()['stage'] == 'ERP_SYNC_PENDING'
    assert repo.erp_write_count(detail['claim_id']) == 0
    erp.faults.mode = FailureMode.NONE
    body = dict(expected_revision=first.json()['revision'], idempotency_key=str(uuid4()))
    endpoint = '/api/v1.5/claims/' + detail['claim_id'] + '/retry'
    response = client.post(endpoint, json=body)
    assert response.status_code == 200, response.text
    assert response.json()['stage'] == 'APPROVED'
    assert client.post(endpoint, json=body).status_code == 200
    assert repo.erp_write_count(detail['claim_id']) == 1


def test_legacy_intent_serialization_stays_byte_compatible():
    body = dict(action='APPROVE', reason='人工核对后决定', expected_revision=2,
                idempotency_key='synthetic-key')
    parsed = FinanceAction.model_validate(body)
    assert parsed.model_dump() == body
    assert FinanceAction.model_validate_json(parsed.model_dump_json()).model_dump() == body
