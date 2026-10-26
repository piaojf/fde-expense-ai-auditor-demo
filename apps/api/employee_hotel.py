"""Additive P4A employee declaration adapter, preserving the v1 trusted-booking API."""

import json
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

from fastapi import BackgroundTasks, Request

from apps.api.enterprise import COOKIE
from apps.api.enterprise_store import timestamp
from apps.api.errors import APIError
from apps.api.hotel_facts import HotelFacts
from domain.employee_hotel import (
    DeclarationResubmission,
    DeclarationSubmission,
    EmployeeHotel,
    EmployeeHotelResubmission,
    VerifyEmployeeStay,
)
from domain.hotel_matrix import HotelFields

PREFIX = "/p4a/v2"


class EmployeeHotelFacts(HotelFacts):
    def resolve(self, company, kind, key, on):
        selected = super().resolve(company, kind, key, on)
        if kind != "EMPLOYEE" or selected is None:
            return selected
        # Adjacent records of the same grade describe continuous eligibility.
        # Preserve immutable source rows and expose their IDs in the derived snapshot.
        rows = sorted(
            (r for r in self.list(company) if r["kind"] == kind and r["key"] == key),
            key=lambda r: r["effective_from"],
        )
        index = next(i for i, row in enumerate(rows) if row["id"] == selected["id"])
        left = right = index
        while (left > 0 and rows[left - 1]["effective_to"] == rows[left]["effective_from"]
               and rows[left - 1]["value"] == selected["value"]):
            left -= 1
        while (right + 1 < len(rows)
               and rows[right]["effective_to"] == rows[right + 1]["effective_from"]
               and rows[right + 1]["value"] == selected["value"]):
            right += 1
        if left == right:
            return selected
        parts = rows[left:right + 1]
        identifiers = [row["id"] for row in parts]
        digest = sha256(json.dumps(identifiers).encode()).hexdigest()
        return {
            **selected,
            "id": "GRADE-COVERAGE:" + digest,
            "effective_from": parts[0]["effective_from"],
            "effective_to": parts[-1]["effective_to"],
            "source": "P4A:CONTIGUOUS_EMPLOYEE_GRADE:" + digest,
            "actor": "server:p4a-grade-coverage",
            "recorded_at": max(row["recorded_at"] for row in parts),
            "version": "coverage:" + digest[:24],
            "component_fact_ids": identifiers,
        }

    def snapshot(self, user, body):
        if not isinstance(body, (DeclarationSubmission, DeclarationResubmission)):
            return super().snapshot(user, body)
        if body._authority_check is not None:
            body._authority_check()
        f = body.fields
        company = user["company_id"]
        grade = self.resolve(company, "EMPLOYEE", user["id"], f.incurred_on)
        cities = [
            r
            for r in self.list(company)
            if r["kind"] == "CITY"
            and r["effective_from"] <= str(f.incurred_on) < r["effective_to"]
            and f.city.strip() in [r["value"]["name"], *r["value"].get("aliases", [])]
        ]
        city = cities[0] if len(cities) == 1 else None
        policy_city = (self.policy_city_resolver(company, f)
                       if hasattr(self, "policy_city_resolver") else None)
        city_conflict = bool(city and policy_city
                             and city["value"]["tier"] != policy_city["value"]["tier"])
        if city_conflict:
            city = None
        elif policy_city:
            # Keep the published row's provenance even when matching legacy city
            # master data exists. Conflicting classifications remain blocked above.
            city = policy_city
        blockers = ["STAY_UNVERIFIED"]
        if city_conflict:
            blockers.append("CITY_CLASSIFICATION_CONFLICT")
        if not grade:
            blockers.append("EMPLOYEE_GRADE_UNKNOWN")
        elif str(f.trip_end) > grade["effective_to"]:
            blockers.append("GRADE_CHANGES_DURING_STAY")
        if not city:
            blockers.append("CITY_TIER_UNKNOWN")
        elif str(f.trip_end) > city["effective_to"]:
            blockers.append("CITY_CHANGES_DURING_STAY")
        return dict(
            employee=grade,
            city=city,
            stay=None,
            blockers=blockers,
            company=company,
            declaration=body.model_dump(mode="json"),
            employee_stay=dict(
                status="PENDING",
                employee_id=user["id"],
                city=f.city,
                address=body.address,
                check_in=str(f.trip_start),
                check_out=str(f.trip_end),
                nights=f.nights,
            ),
        )

    def refresh_pending(self, cid, version):
        """Explicit re-audit refreshes pending facts; old submission snapshots remain intact."""
        stored = self.read(cid)
        if not stored or stored[0].get("employee_stay", {}).get("status") != "PENDING":
            return
        old = stored[0]
        declaration_type = (DeclarationResubmission if "expected_revision" in old["declaration"]
                            else DeclarationSubmission)
        new = self.snapshot(
            dict(id=old["employee_stay"]["employee_id"], company_id=old["company"]),
            declaration_type.model_validate(old["declaration"]),
        )
        self.db.execute("UPDATE p4a_claims SET body=? WHERE claim_id=?",
                        (json.dumps(new, ensure_ascii=False), cid))
        self.db.execute("INSERT INTO p4a_claim_versions VALUES(?,?,?)",
                        (cid, version, json.dumps(new, ensure_ascii=False)))


def install_employee_hotel_api(app, repo, service):
    enterprise = app.state.store
    facts = service.catalog.facts
    old_create = next(
        r.endpoint for r in app.routes if r.path == "/p4a/v1/claims" and "POST" in r.methods
    )
    old_resubmit = next(
        r.endpoint
        for r in app.routes
        if r.path == "/p4a/v1/claims/{cid}/resubmit" and "POST" in r.methods
    )

    def bridge(body, p, resubmit=False):
        fields = HotelFields(
            applicant_name=p["name"],
            department=p["department"],
            position=p["position"],
            city=body.city.strip(),
            incurred_on=body.check_in,
            trip_start=body.check_in,
            trip_end=body.check_out,
            nights=(body.check_out - body.check_in).days,
            amount=body.amount,
            purpose=body.purpose.strip(),
            seller=body.seller.strip(),
            notes=body.notes,
        )
        values = dict(
            fields=fields,
            address=body.address.strip(),
            invoice=body.invoice,
            idempotency_key=body.idempotency_key,
        )
        if resubmit:
            return DeclarationResubmission(**values, expected_revision=body.expected_revision)
        return DeclarationSubmission(**values)

    @app.post(PREFIX + "/claims", status_code=202)
    async def create(body: EmployeeHotel, request: Request, background: BackgroundTasks):
        p = enterprise.live_actor(request.cookies.get(COOKIE), "EMPLOYEE")
        internal = bridge(body, p)
        generation = enterprise.authority(p["id"])
        internal._authority_check = lambda: enterprise.live_actor(
            request.cookies.get(COOKIE),
            "EMPLOYEE",
            expected_generation=generation,
            company_id=p["company_id"],
        )
        return await old_create(internal, request, background)

    @app.post(PREFIX + "/claims/{cid}/resubmit", status_code=202)
    async def resubmit(
        cid: str, body: EmployeeHotelResubmission, request: Request, background: BackgroundTasks
    ):
        p = enterprise.live_actor(request.cookies.get(COOKIE), "EMPLOYEE")
        internal = bridge(body, p, True)
        generation = enterprise.authority(p["id"])
        internal._authority_check = lambda: enterprise.live_actor(
            request.cookies.get(COOKIE),
            "EMPLOYEE",
            expected_generation=generation,
            company_id=p["company_id"],
        )
        return await old_resubmit(cid, internal, request, background)

    @app.post(PREFIX + "/claims/{cid}/verify-stay")
    async def verify(cid: str, body: VerifyEmployeeStay, request: Request):
        p = enterprise.live_actor(request.cookies.get(COOKIE), "FINANCE", "ADMIN")
        generation = enterprise.authority(p["id"])
        scope = p["id"] + ":employee-stay:" + cid
        payload = body.model_dump_json()
        async with app.state.work_lock, service.lock:
            with facts.lock, facts.db:
                facts.db.execute("BEGIN IMMEDIATE")
                p = enterprise.live_actor(
                    request.cookies.get(COOKIE),
                    "FINANCE",
                    "ADMIN",
                    expected_generation=generation,
                    company_id=p["company_id"],
                )
                row = enterprise.claim_row(cid, p)
                if row["owner_id"] == p["id"]:
                    raise APIError(403, "SELF_VERIFICATION_FORBIDDEN", "不能核实本人的申请")
                replay = enterprise.replay(scope, body.idempotency_key, payload)
                if replay:
                    return dict(claim_id=cid, status="VERIFIED", revision=body.expected_revision)
                if row["revision"] != body.expected_revision or row["stage"] != "FINANCE_REVIEW":
                    raise APIError(409, "REVISION_CONFLICT", "仅可核实当前待财务审核版本")
                if repo.erp_write_count(cid):
                    raise APIError(409, "ERP_EFFECT_EXISTS", "已有入账记录不能修改")
                stored = facts.read(cid)
                snapshot = deepcopy(stored[0]) if stored else {}
                declaration = snapshot.get("employee_stay")
                if not declaration or declaration["status"] != "PENDING":
                    raise APIError(409, "STAY_NOT_PENDING", "此版本没有待核实的员工住宿申报")
                if declaration["employee_id"] != row["owner_id"]:
                    raise APIError(409, "IDENTITY_MISMATCH", "住宿申报身份不一致")
                if not snapshot.get("city") or not snapshot.get("employee"):
                    raise APIError(
                        409, "MASTER_DATA_UNKNOWN", "请先维护有效职级和城市标准，再退回员工重新提交"
                    )
                snapshot["stay"] = dict(
                    id="STAY-" + uuid4().hex,
                    kind="STAY",
                    key="claim:" + cid,
                    company=p["company_id"],
                    actor=p["id"],
                    effective_from=declaration["check_in"],
                    effective_to=declaration["check_out"],
                    source=body.source_reference.strip(),
                    version=str(row["revision"]),
                    recorded_at=timestamp(),
                    claim_id=cid,
                    value=dict(
                        employee_id=declaration["employee_id"],
                        city_key=snapshot["city"]["key"],
                        address=declaration["address"],
                        check_in=declaration["check_in"],
                        check_out=declaration["check_out"],
                        evidence_reference=body.source_reference.strip(),
                    ),
                )
                declaration.update(status="VERIFIED", actor=p["id"], basis=body.basis.strip())
                snapshot["blockers"] = [b for b in snapshot["blockers"] if b != "STAY_UNVERIFIED"]
                facts.db.execute(
                    "UPDATE p4a_claims SET body=? WHERE claim_id=?",
                    (json.dumps(snapshot, ensure_ascii=False), cid),
                )
                enterprise.remember(scope, body.idempotency_key, payload, cid)
                enterprise.event(
                    p,
                    "EMPLOYEE_STAY_VERIFIED",
                    cid,
                    revision=row["revision"],
                    facts=snapshot,
                    source_reference=body.source_reference,
                    basis=body.basis,
                )
        return dict(claim_id=cid, status="VERIFIED", revision=body.expected_revision)
