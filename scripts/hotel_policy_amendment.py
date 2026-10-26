"""Explicit retrospective hotel amendment for the isolated local Mock demo only.

Normal administrative publication still requires a future effective date.
This offline demo operation appends a new version/events; it never edits an old
source, policy, report, employee grade or finance decision.
"""

import argparse
import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from apps.api.hotel_publication import HotelPublication
from apps.api.policy_publication_store import fail
from apps.api.policy_store import PolicyDraftStore
from domain.hotel_matrix import MatrixConfirm, MatrixPreview, MatrixPublish
from domain.policies import ConfirmPolicy
from integrations.ocr.local import LocalOCRProvider
from integrations.policies.parser import PolicyParser
from rag.embeddings.providers import DeterministicTestEmbedding
from rag.retrieval.compliance import HOTEL_2020_2030_SHA256
from scripts.finance_workbench_demo import DB_PREFIX, ROOT, active, save

REASON = "用户明确要求本机住宿演示适用2020至2030年；旧审核及原文保留，不扩展其他费用"


class DemoAmendmentPublication(HotelPublication):
    def __init__(self, draft, embedding):
        url = make_url(str(draft.engine.url.render_as_string(hide_password=False)))
        if not (url.database or "").startswith(DB_PREFIX):
            raise ValueError("Isolated finance demo database required")
        super().__init__(draft, embedding, app_env="test",
                         clock=lambda: datetime.now(UTC))

    def future(self, value):
        if value.isoformat() != "2020-01-01":
            fail("DEMO_AMENDMENT_SCOPE", "仅接受已授权的2020至2030住宿修订")

    def check_replacement(self, session, policy, predecessor, *, exclude=()):
        # Only this exact allowlisted artifact/interval may retrospectively replace
        # the demo hotel scope. Standard HotelPublication retains all its guards.
        source = self.draft.get(session, policy.authoritative_rules["upload_id"]) \
            if policy.authoritative_rules.get("upload_id") else None
        # Indexed drafts have no executable rules yet; locate their upload directly.
        if source is None:
            from sqlalchemy import select

            from database.models import PolicyUpload

            source = session.scalar(select(PolicyUpload).where(
                PolicyUpload.policy_version_id == policy.policy_version_id))
        if (not source or source.sha256 != HOTEL_2020_2030_SHA256
                or policy.legal_entity != "DEMO-CN" or policy.category != "HOTEL"
                or policy.version != "HOTEL-2020-2030-V1"
                or policy.effective_from.isoformat() != "2020-01-01"
                or policy.effective_to.isoformat() != "2031-01-01"):
            fail("DEMO_AMENDMENT_SCOPE", "修订原文或生效范围不匹配")
        rows = [p for p in self.scope_rows(session, policy)
                if p.authority_level >= 100 and p.policy_version_id not in exclude
                and p.effective_from < policy.effective_to
                and (p.effective_to is None or p.effective_to > policy.effective_from)]
        if set(predecessor or []) != {p.policy_version_id for p in rows}:
            fail("POLICY_CONFLICT", "必须明确覆盖本机演示的全部重叠住宿版本")
        return rows


def apply():
    gen = active()
    marker = gen / "hotel-policy-2020-2030.json"
    if marker.exists():
        return json.loads(marker.read_text(encoding="utf-8"))
    source = ROOT / "docs/policies/222-hotel-2020-2030.docx"
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != HOTEL_2020_2030_SHA256:
        raise ValueError("Amended source SHA mismatch")
    url = make_url((gen / "database-url.txt").read_text(encoding="utf-8").strip())
    if not (url.database or "").startswith(DB_PREFIX):
        raise ValueError("Refuse non-demo database")
    engine = create_engine(url)
    try:
        embedding = DeterministicTestEmbedding()
        draft = PolicyDraftStore(engine, PolicyParser(LocalOCRProvider()), lambda _: embedding)
        publication = DemoAmendmentPublication(draft, embedding)
        row = draft.upload(raw, source.name)
        uid = row["upload_id"]
        metadata = {k: v["value"] for k, v in row["parsed"]["extracted_metadata"].items()}
        draft.confirm(uid, ConfirmPolicy(token="synthetic-local-policy-amendment-20201017",
                                         confirmed=True, metadata=metadata))
        draft.ingest(uid)
        current = publication.read(uid)
        publication.confirm_matrix(uid, MatrixConfirm(
            expected_revision=0, extraction_digest=current["extraction_digest"],
            confirmed=True, reason=REASON), "local-demo:user-authorized")
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from database.models import PolicyVersion

        with Session(engine) as session:
            predecessors = list(session.scalars(select(PolicyVersion.policy_version_id).where(
                PolicyVersion.legal_entity == "DEMO-CN", PolicyVersion.category == "HOTEL",
                PolicyVersion.status == "ACTIVE", PolicyVersion.authority_level >= 100)))
        view = publication.preview(uid, MatrixPreview(predecessor_ids=predecessors))
        if view["blockers"]:
            raise ValueError(view["blockers"])
        result = publication.publish_matrix(uid, MatrixPublish(
            predecessor_ids=predecessors, preview_digest=view["preview_digest"],
            confirmed=True, idempotency_key="local-hotel-amendment-2020-2030-v1", reason=REASON),
            "local-demo:user-authorized")
        save(marker, result)
        return result
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply-local-demo", action="store_true", required=True)
    parser.parse_args()
    print(json.dumps(apply(), ensure_ascii=False, indent=2))
