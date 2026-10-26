"""Amended source dates and policy-derived cities; all providers are offline."""

from datetime import date
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from advisory_support import BASE as P4B
from p4b_fix2_support import login
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_employee_hotel import BASE, V1, declaration
from test_employee_hotel import employee_portal as employee_portal
from test_employee_hotel_advisory import employee_advisory as employee_advisory
from test_p4a_postgres import hotel_portal as hotel_portal
from test_policies_postgres import policy_pg as policy_pg

from apps.api.policy_store import PolicyDraftStore
from database.models import PolicyVersion
from domain.hotel_matrix import MatrixConfirm, MatrixPreview, MatrixPublish
from domain.policies import ConfirmPolicy
from integrations.policies.hotel_cities import named_city
from integrations.policies.hotel_matrix import extract_matrix
from integrations.policies.parser import PolicyParser
from rag.embeddings.providers import DeterministicTestEmbedding
from rag.retrieval.compliance import (
    HOTEL_2020_2030_SHA256,
    SOURCE_SHA256,
    ComplianceRetriever,
    original_chunks,
)
from scripts.hotel_policy_amendment import DemoAmendmentPublication
from tests.test_enterprise import action

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'docs/policies/222-hotel-2020-2030.docx'
PARSER = PolicyParser(SimpleNamespace(binary='', languages='eng', tessdata=None))


def test_original_source_preserved_and_amendment_dates_and_cells():
    raw = SOURCE.read_bytes()
    assert sha256(raw).hexdigest() == HOTEL_2020_2030_SHA256
    old = (ROOT / 'tests/fixtures/policies/p4a-222.docx').read_bytes()
    assert sha256(old).hexdigest() == SOURCE_SHA256
    parsed = PARSER.parse(raw, SOURCE.name)
    metadata = parsed['extracted_metadata']
    assert metadata['effective_from']['value'] == '2020-01-01'
    assert metadata['effective_to']['value'] == '2031-01-01'
    assert metadata['version']['value'] == 'HOTEL-2020-2030-V1'
    matrix = extract_matrix(raw, parsed)
    original = extract_matrix(old, PARSER.parse(old, '222.docx'))
    assert matrix['blockers'] == []
    assert matrix['rules'] == original['rules']
    assert matrix['city_classification'] == original['city_classification']
    with pytest.raises(ValueError, match='P4B_SOURCE_SHA_MISMATCH'):
        original_chunks(raw + b'tampered', document_id='x', policy_version_id='x',
                        effective_from=date(2020, 1, 1), effective_to=date(2031, 1, 1))


@pytest.mark.parametrize('name,tier', [('北京', 'I'), ('北京市', 'I'), ('上海', 'I'),
                                     ('广州', 'I'), ('深圳市', 'I'), ('杭州', 'II'),
                                     ('重庆市', 'II'), ('天津', 'II'), ('厦门', 'II')])
def test_exact_named_city_and_supported_city_suffix(name, tier):
    matrix = extract_matrix(SOURCE.read_bytes(), PARSER.parse(SOURCE.read_bytes(), SOURCE.name))
    result = named_city(matrix, name)
    assert result['tier'] == tier
    assert result['source_row']['original_cells']


@pytest.mark.parametrize('name', ['北京周边', '北京朝阳', '未维护县城', '香港', '台北', '北亰'])
def test_unlisted_or_ambiguous_places_are_not_inferred(name):
    matrix = extract_matrix(SOURCE.read_bytes(), PARSER.parse(SOURCE.read_bytes(), SOURCE.name))
    assert named_city(matrix, name) is None


def publish_amendment(env):
    c, app, _, _, _, _ = env
    engine = app.state.audit_service.catalog.engine
    emb = DeterministicTestEmbedding()
    draft = PolicyDraftStore(engine, PARSER, lambda _: emb)
    pub = DemoAmendmentPublication(draft, emb)
    row = draft.upload(SOURCE.read_bytes(), SOURCE.name)
    uid = row['upload_id']
    metadata = {k: v['value'] for k, v in row['parsed']['extracted_metadata'].items()}
    draft.confirm(uid, ConfirmPolicy(token='synthetic-local-amendment-confirmation-0000',
                                    confirmed=True, metadata=metadata))
    draft.ingest(uid)
    view = pub.read(uid)
    pub.confirm_matrix(uid, MatrixConfirm(expected_revision=0, confirmed=True,
                       extraction_digest=view['extraction_digest'], reason='明确授权本机住宿修订'),
                       'synthetic-admin')
    with Session(engine) as s:
        predecessors = list(s.scalars(select(PolicyVersion.policy_version_id).where(
            PolicyVersion.status == 'ACTIVE', PolicyVersion.authority_level >= 100,
            PolicyVersion.category == 'HOTEL', PolicyVersion.legal_entity == 'DEMO-CN')))
    preview = pub.preview(uid, MatrixPreview(predecessor_ids=predecessors))
    assert not preview['blockers'], preview
    result = pub.publish_matrix(uid, MatrixPublish(predecessor_ids=predecessors,
                               confirmed=True, preview_digest=preview['preview_digest'],
                               reason='明确授权本机住宿修订', idempotency_key=str(uuid4())),
                               'synthetic-admin')
    pid = result['policy']['policy_version_id']
    login(c, 'admin')
    source = c.get(P4B + '/policies/' + pid)
    assert source.status_code == 200, source.text
    response = c.post(P4B + '/policies/' + pid + '/confirm-scope', json=dict(
        extraction_digest=source.json()['rules']['extraction_digest'], confirmed=True,
        reason='原文城市与日期核对', idempotency_key=str(uuid4())))
    assert response.status_code == 200, response.text
    return pid


@pytest.fixture
def amended(employee_advisory):
    return employee_advisory, publish_amendment(employee_advisory)


@pytest.mark.parametrize('on,inside', [('2019-12-31', True), ('2020-01-01', True),
    ('2024-05-12', True), ('2027-01-05', True), ('2030-12-31', True),
    ('2031-01-01', False), ('2031-01-02', False)])
def test_published_amendment_applies_until_cutoff_and_old_retirements(amended, on, inside):
    """Only the half-open cutoff rejects a claim; the start date never does."""
    env, pid = amended
    from datetime import UTC, datetime
    retriever = ComplianceRetriever(env[1].state.audit_service.catalog.engine)
    if inside:
        result = retriever.read(company='DEMO-CN', city='北京', incurred_on=date.fromisoformat(on),
                                started=datetime.now(UTC))
        assert result['policy_version_id'] == pid
        assert all(e.document_sha256 == HOTEL_2020_2030_SHA256 for e in result['chunks'])
    else:
        with pytest.raises(ValueError, match='NO_ACTIVE_P4B_POLICY'):
            retriever.read(company='DEMO-CN', city='北京', incurred_on=date.fromisoformat(on),
                           started=datetime.now(UTC))


def test_2024_beijing_advice_runs_without_inventing_hr_stay_or_approval(amended):
    env, pid = amended
    c, _, repo, _, _, _ = env
    login(c)
    body = declaration()
    body.update(city='北京市', check_in='2024-05-12', check_out='2024-05-13', amount='342.00')
    response = c.post(BASE + '/claims', json=body)
    assert response.status_code == 202, response.text
    cid = response.json()['claim_id']
    hotel = c.get(V1 + '/claims/' + cid).json()
    assert hotel['facts']['city']['value']['tier'] == 'I'
    assert hotel['facts']['city']['policy_version_id'] == pid
    assert hotel['facts']['employee'] is None
    assert 'EMPLOYEE_GRADE_UNKNOWN' in hotel['facts']['blockers']
    assert 'STAY_UNVERIFIED' in hotel['facts']['blockers']
    assert 'CITY_TIER_UNKNOWN' not in hotel['facts']['blockers']
    detail = c.get('/api/v1.5/claims/' + cid).json()
    report = detail['advisory_analysis']
    assert report['ai_advisory_status'] == 'SUCCEEDED', report['validation_errors']
    assert report['deterministic_status'] == 'UNKNOWN'
    assert report['recommendation'] == 'REQUEST_EVIDENCE'
    assert 'grade' in report['missing_evidence'] and 'stay' in report['missing_evidence']
    login(c, 'finance')
    assert action(c, detail, 'APPROVE').status_code == 409
    assert repo.erp_write_count(cid) == 0


def test_policy_city_classification_is_valid_before_policy_start_date():
    """A selected final hotel matrix can classify an earlier claim's declared city."""
    from apps.api.compliance import HybridCatalog

    employee_fact = {
        "id": "grade-d",
        "key": "employee-1",
        "value": {"grade": "D"},
        "effective_from": "1900-01-01",
        "effective_to": "9999-12-31",
    }
    city_fact = {
        "id": "published-city-beijing",
        "key": "policy-city:北京",
        "value": {"name": "北京", "tier": "I"},
        "effective_from": "2026-11-01",
        "effective_to": "2027-01-01",
        "policy_version_id": "published-final-matrix",
    }
    snapshot = {
        "company": "DEMO-CN",
        "blockers": ["STAY_UNVERIFIED"],
        "city": city_fact,
        "stay": None,
    }

    class Facts:
        def resolve(self, company, kind, key, on):
            return employee_fact if kind == "EMPLOYEE" else None

        def list(self, company):
            return []

        def read(self, claim_id):
            return snapshot, None

    catalog = HybridCatalog.__new__(HybridCatalog)
    catalog.legacy = SimpleNamespace(facts=Facts())
    result = catalog.trusted(
        SimpleNamespace(
            employee=SimpleNamespace(
                legal_entity="DEMO-CN", employee_id="employee-1", department="研发部"
            ),
            claim_id="claim-1",
            expense=SimpleNamespace(incurred_on="2024-05-12"),
        ),
        {
            "city": "北京",
            "incurred_on": "2024-05-12",
            "trip_start": "2024-05-12",
            "trip_end": "2024-05-13",
        },
    )
    assert result["city_tier"] == "I"
    assert result["grade"] == "D"


def test_explicit_reaudit_refreshes_city_and_preserves_failed_history(employee_advisory):
    env = employee_advisory
    c, _, repo, _, _, _ = env
    login(c)
    # Past the current cutoff, so no policy applies until the amendment is published.
    body = declaration()
    body.update(city="北京", check_in="2027-01-02", check_out="2027-01-03", amount="342.00")
    response = c.post(BASE + "/claims", json=body)
    assert response.status_code == 202, response.text
    cid = response.json()["claim_id"]
    detail = c.get("/api/v1.5/claims/" + cid).json()
    assert detail["advisory_analysis"]["ai_advisory_status"] == "FAILED"
    assert detail["advisory_analysis"]["validation_errors"][0]["code"] == "NO_ACTIVE_P4B_POLICY"
    original = repo._db.execute("SELECT key,body FROM p4b_records WHERE kind='ADVISORY' "
                                "AND key LIKE ?", (cid + ":%",)).fetchall()
    original_snapshot = repo._db.execute("SELECT body FROM p4a_claim_versions "
                                        "WHERE claim_id=? AND version=1", (cid,)).fetchone()[0]
    pid = publish_amendment(env)
    login(c, "finance")
    result = c.post(P4B + f"/claims/{cid}/reaudit", json=dict(
        expected_revision=detail["revision"], idempotency_key=str(uuid4())))
    assert result.status_code == 202, result.text
    current = c.get("/api/v1.5/claims/" + cid).json()
    assert current["advisory_analysis"]["ai_advisory_status"] == "SUCCEEDED", current
    assert current["advisory_analysis"]["policy_version_id"] == pid
    assert current["advisory_analysis"]["deterministic_status"] == "UNKNOWN"
    hotel = c.get(V1 + "/claims/" + cid).json()
    assert hotel["facts"]["city"]["value"]["tier"] == "I"
    assert hotel["facts"]["employee"]["value"]["grade"] == "D"
    for key, body in original:
        assert repo._db.execute("SELECT body FROM p4b_records WHERE kind='ADVISORY' "
                                "AND key=?", (key,)).fetchone()[0] == body
    assert repo._db.execute("SELECT body FROM p4a_claim_versions WHERE claim_id=? "
                            "AND version=1", (cid,)).fetchone()[0] == original_snapshot
    assert repo.erp_write_count(cid) == 0


def test_manual_city_conflicting_with_published_table_remains_unknown(amended):
    env, _ = amended
    c = env[0]
    login(c, 'admin')
    r = c.post(V1 + '/facts', json=dict(
        kind='CITY', key='deliberately-conflicting-beijing',
        effective_from='2020-01-01', effective_to='2031-01-01',
        value=dict(name='北京', tier='II', level='MUNICIPALITY'),
        source='SYNTHETIC conflicting master', version='test-conflict', reason='隔离负例'))
    assert r.status_code == 200, r.text
    login(c)
    body = declaration()
    body.update(city='北京', check_in='2024-05-12', check_out='2024-05-13', amount='342.00')
    r = c.post(BASE + '/claims', json=body)
    assert r.status_code == 202, r.text
    facts = c.get(V1 + '/claims/' + r.json()['claim_id']).json()['facts']
    assert facts['city'] is None
    assert 'CITY_CLASSIFICATION_CONFLICT' in facts['blockers']
