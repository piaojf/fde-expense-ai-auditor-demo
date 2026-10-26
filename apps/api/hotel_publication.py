"""Atomic matrix governance using UI-P3B tables, indexes and the same advisory lock."""

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.policy_publication_store import (
    PolicyPublicationStore,
    digest,
    fail,
    review_json,
    summary,
)
from database.models import PolicyPublicationEvent, PolicyRuleReview, PolicyVersion
from integrations.policies.hotel_matrix import extract_matrix


class HotelPublication(PolicyPublicationStore):
    def source(self, row):
        import hashlib

        if row.original is None or hashlib.sha256(row.original).hexdigest() != row.sha256:
            fail("FILE_INTEGRITY", "制度原文件 SHA-256 不匹配")
        parsed = self.draft.parser.parse(row.original, row.filename)
        if parsed != row.parsed:
            fail("PARSE_INTEGRITY", "解析文本与原始文件不一致")
        x = extract_matrix(row.original, parsed)
        if x["blockers"]:
            fail("MATRIX_EXTRACTION_BLOCKED", "矩阵缺失、重复、歧义或来源不完整")
        return x

    def read(self, uid):
        with Session(self.engine) as s:
            row = self.draft.get(s, uid)
            x = self.source(row)
            reviews = s.scalars(
                select(PolicyRuleReview)
                .where(PolicyRuleReview.upload_id == str(uid))
                .order_by(PolicyRuleReview.revision)
            ).all()
            return dict(
                upload_id=str(uid),
                source_state=row.state,
                metadata_confirmed=bool(row.confirmed_metadata),
                extraction=x,
                extraction_digest=digest(x),
                reviews=[review_json(r) for r in reviews],
            )

    def confirm_matrix(self, uid, body, actor):
        with Session(self.engine) as s, s.begin():
            self.lock(s)
            row = self.draft.get(s, uid)
            x = self.source(row)
            if not row.confirmed_metadata or row.confirmed_metadata["expense_type"] not in {
                "HOTEL", "ALL",
            }:
                fail("HOTEL_METADATA_REQUIRED", "请先确认包含住宿费用的制度信息")
            if s.scalar(
                select(PolicyPublicationEvent.event_id).where(
                    PolicyPublicationEvent.upload_id == str(uid)
                )
            ):
                fail("ALREADY_PUBLISHED", "已发布来源不得修改")
            latest = self.latest_review(s, uid)
            if body.expected_revision != (latest.revision if latest else 0):
                fail("REVIEW_STALE", "确认记录已变化")
            if body.extraction_digest != digest(x):
                fail("SOURCE_STALE", "必须确认当前完整原文矩阵")
            r = PolicyRuleReview(
                review_id=str(uuid4()),
                upload_id=str(uid),
                revision=body.expected_revision + 1,
                file_sha256=row.sha256,
                extraction=x,
                confirmed_rule=dict(
                    rule_type="HOTEL_LIMIT_MATRIX", matrix=x, metadata=row.confirmed_metadata
                ),
                actor=actor,
                reason=body.reason,
                recorded_at=self.clock(),
            )
            s.add(r)
            s.flush()
            return review_json(r)

    def scope_rows(self, s, p):
        return s.scalars(
            select(PolicyVersion)
            .where(
                PolicyVersion.status == "ACTIVE",
                PolicyVersion.legal_entity == p.legal_entity,
                PolicyVersion.category == "HOTEL",
            )
            .order_by(PolicyVersion.policy_version_id)
        ).all()

    def check_replacement(self, s, p, predecessor, *, exclude=()):
        rows = self.scope_rows(s, p)
        events = s.scalars(select(PolicyPublicationEvent)).all()
        overlaps = []
        if any(x.city == "*" and x.version == p.version for x in rows):
            fail("VERSION_CONFLICT", "矩阵版本名称已存在")
        for x in rows:
            if x.authority_level < 100 or x.policy_version_id in exclude:
                continue
            ends = [e.effective_on for e in events if e.predecessor_id == x.policy_version_id]
            end = min([*ends, *([x.effective_to] if x.effective_to else [])], default=date.max)
            if x.effective_from < (p.effective_to or date.max) and p.effective_from < end:
                overlaps.append(x)
        predecessors = predecessor or []
        if not predecessors and overlaps:
            fail("PREDECESSOR_REQUIRED", "须明确确认所有冲突旧版本的整体切换")
        if set(predecessors) != {x.policy_version_id for x in overlaps} or any(
            x.effective_from >= p.effective_from for x in overlaps
        ):
            fail("POLICY_CONFLICT", "所选前驱必须精确覆盖全部重叠版本")
        if any(e.predecessor_id in predecessors for e in events):
            fail("SUCCESSOR_CONFLICT", "前驱已有后继或退休安排")
        return overlaps

    def preview_in_session(self, s, uid, predecessor):
        row = self.draft.get(s, uid)
        x = self.source(row)
        if row.state != "INDEXED":
            fail("INDEX_REQUIRED", "请先建立原文草稿索引")
        r = self.latest_review(s, uid)
        if not r or r.confirmed_rule.get("matrix") != x:
            fail("MATRIX_CONFIRM_REQUIRED", "管理员必须确认完整 16 格矩阵")
        m = row.confirmed_metadata
        if r.confirmed_rule["metadata"] != m:
            fail("REVIEW_INTEGRITY", "元数据与已确认矩阵不一致")
        for key in (
            "legal_entity",
            "region",
            "expense_type",
            "version",
            "effective_from",
            "effective_to",
        ):
            original = row.parsed["extracted_metadata"][key]["value"]
            # Document-wide classification does not expand the executable matrix:
            # its city tiers and all 16 hotel cells were verified from the original.
            if key == "region" and m[key] == "*" and original not in {None, "UNKNOWN"}:
                continue
            if key == "expense_type" and m[key] in {"HOTEL", "ALL"} and original in {
                "HOTEL", "ALL",
            }:
                continue
            if original != m[key]:
                labels = {"legal_entity": "公司", "region": "地区", "expense_type": "费用类型",
                          "version": "版本", "effective_from": "生效日期",
                          "effective_to": "截止日期"}
                fail("METADATA_SOURCE_MISMATCH",
                     f"{labels[key]}与原文不一致（已确认：{m[key]}；原文：{original}），请核对原件")
        p = self.draft.indexed_version(s, row)
        self.future(p.effective_from)
        chunks = super().chunks(s, p, row=row)
        old = self.check_replacement(s, p, predecessor)
        view = dict(
            upload_id=str(uid),
            review_id=r.review_id,
            revision=r.revision,
            extraction=x,
            old=[summary(v) for v in old],
            new={**summary(p), "region": "*", "expense_type": "HOTEL", "rules": x},
            impact=dict(
                city_scope=x["city_classification"],
                original_index_region=m["region"],
                matrix_scope="controlled city tiers",
                historical_and_inflight="Preserved at audit-start",
            ),
            scope_snapshot=digest([summary(v) for v in self.scope_rows(s, p)]),
            governance_snapshot=digest(
                s.scalars(
                    select(PolicyPublicationEvent.event_id).order_by(
                        PolicyPublicationEvent.event_id
                    )
                ).all()
            ),
            chunk_snapshot=digest(
                [
                    dict(
                        id=c.chunk_id,
                        hash=c.chunk_hash,
                        vector=list(c.embedding),
                        meta=c.source_metadata,
                    )
                    for c in chunks
                ]
            ),
            blockers=[],
        )
        view["preview_digest"] = digest(view)
        return view, row, p, r, chunks

    def publish_matrix(self, uid, body, actor):
        fp = digest(dict(upload_id=str(uid), actor=actor, **body.model_dump(mode="json")))
        with Session(self.engine) as s, s.begin():
            self.lock(s)
            replay = self.replay(s, body.idempotency_key, fp)
            if replay:
                return replay
            if s.scalar(
                select(PolicyPublicationEvent.event_id).where(
                    PolicyPublicationEvent.upload_id == str(uid)
                )
            ):
                fail("ALREADY_PUBLISHED", "请使用原幂等键查询已有发布")
            predecessors = self.predecessors(body)
            view, row, p, r, chunks = self.preview_in_session(s, uid, predecessors)
            if body.preview_digest != view["preview_digest"]:
                fail("PREVIEW_STALE", "发布预览已变化")
            pid = "P4A-" + str(uuid4())
            stamp = self.clock()
            eid = str(uuid4())
            rules = dict(
                rule_type="HOTEL_LIMIT_MATRIX",
                matrix=r.extraction,
                rule_review_id=r.review_id,
                upload_id=str(uid),
                file_sha256=row.sha256,
            )
            source = SimpleNamespace(
                **{
                    k: getattr(p, k)
                    for k in (
                        "legal_entity",
                        "title",
                        "version",
                        "source_uri",
                        "content_sha256",
                        "policy_version_id",
                    )
                },
                city="*",
                category="HOTEL",
            )
            published = self.copy_version(
                s,
                source,
                chunks,
                pid=pid,
                version=p.version,
                start=p.effective_from,
                end=p.effective_to,
                rules=rules,
                timestamp=stamp,
            )
            result = dict(
                kind="PUBLISH",
                event_id=eid,
                policy=summary(published),
                actor=actor,
                predecessor_id=predecessors[0] if predecessors else None,
                predecessor_ids=predecessors,
                recorded_at=stamp.isoformat(),
            )
            s.add(
                PolicyPublicationEvent(
                    event_id=eid,
                    idempotency_key=body.idempotency_key,
                    request_sha256=fp,
                    kind="PUBLISH",
                    policy_version_id=pid,
                    predecessor_id=predecessors[0] if predecessors else None,
                    restore_id=None,
                    upload_id=str(uid),
                    review_id=r.review_id,
                    effective_on=p.effective_from,
                    recorded_at=stamp,
                    actor=actor,
                    summary=view,
                    result=result,
                )
            )
            for old_id in predecessors[1:]:
                # Additional cutovers share this transaction; no partial city activation.
                cutover_id = str(uuid4())
                s.add(
                    PolicyPublicationEvent(
                        event_id=cutover_id,
                        idempotency_key=body.idempotency_key + ":" + digest(old_id)[:16],
                        request_sha256=fp,
                        kind="RETIRE",
                        policy_version_id=None,
                        predecessor_id=old_id,
                        restore_id=None,
                        upload_id=None,
                        review_id=None,
                        effective_on=p.effective_from,
                        recorded_at=stamp,
                        actor=actor,
                        summary=view,
                        result=dict(
                            kind="RETIRE", matrix_publication_id=eid, predecessor_id=old_id
                        ),
                    )
                )
            s.flush()
            return result

    @staticmethod
    def predecessors(body):
        items = [*body.predecessor_ids, *([body.predecessor_id] if body.predecessor_id else [])]
        if len(items) != len(set(items)):
            fail("POLICY_CONFLICT", "前驱列表重复")
        return sorted(items)

    def preview(self, uid, body):
        from apps.api.errors import APIError

        with Session(self.engine) as s, s.begin():
            self.lock(s)
            try:
                return self.preview_in_session(s, uid, self.predecessors(body))[0]
            except APIError as e:
                return dict(blockers=[e.code], message=e.message)

    def copy_version(self, s, source, chunks, **options):
        rules = options["rules"]
        if rules.get("rule_type") == "HOTEL_LIMIT_MATRIX":
            rules = {
                **rules,
                "cells": [
                    dict(
                        **cell,
                        legal_entity=source.legal_entity,
                        policy_version_id=options["pid"],
                        effective_from=options["start"].isoformat(),
                        effective_to=options["end"].isoformat() if options["end"] else None,
                        source_document_id=rules["upload_id"],
                    )
                    for cell in rules["matrix"]["rules"]
                ],
            }
        return super().copy_version(s, source, chunks, **{**options, "rules": rules})

    def chunks(self, s, p, *, row=None):
        if row is None and p.authoritative_rules.get("rule_type") == "HOTEL_LIMIT_MATRIX":
            row = self.draft.get(s, p.authoritative_rules["upload_id"])
            if self.source(row) != p.authoritative_rules["matrix"]:
                fail("MATRIX_SOURCE_CHANGED", "已发布矩阵与原文不一致")
        return super().chunks(s, p, row=row)

    def lifecycle_preview_in_session(self, s, pid, body):
        from rag.retrieval.pgvector import scope_predicates

        self.future(body.effective_on)
        p = s.get(PolicyVersion, pid)
        if not p or p.authoritative_rules.get("rule_type") != "HOTEL_LIMIT_MATRIX":
            fail("MATRIX_REQUIRED", "请选择矩阵发布版本")
        if body.effective_on <= p.effective_from or (
            p.effective_to and body.effective_on >= p.effective_to
        ):
            fail("CUTOVER_DATE_INVALID", "切换日期须在版本有效期间内")
        active = s.scalars(
            select(PolicyVersion).where(
                *scope_predicates(
                    legal_entity=p.legal_entity,
                    category="HOTEL",
                    city="*",
                    incurred_on=body.effective_on,
                    audit_started_at=self.clock(),
                ),
                PolicyVersion.authority_level >= 100,
            )
        ).all()
        if len(active) != 1 or active[0].policy_version_id != pid:
            fail("POLICY_CONFLICT", "目标日期并非唯一矩阵版本")
        if s.scalar(
            select(PolicyPublicationEvent.event_id).where(
                PolicyPublicationEvent.predecessor_id == pid
            )
        ):
            fail("SUCCESSOR_CONFLICT", "已有后继或退休安排")
        chunks = self.chunks(s, p)
        restore = None
        restore_chunks = []
        if body.kind == "ROLLBACK":
            restore = s.get(PolicyVersion, body.restore_id or "")
            if (
                not restore
                or restore.legal_entity != p.legal_entity
                or restore.category != "HOTEL"
                or (restore.effective_from >= p.effective_from or restore.authority_level != 100)
            ):
                fail("RESTORE_SCOPE_INVALID", "回退需同主体较早的权威 HOTEL 原版")
            restore_chunks = self.chunks(s, restore)
        elif body.restore_id is not None:
            fail("RESTORE_SCOPE_INVALID", "退休不可指定回退来源")
        view = dict(
            target=summary(p),
            restore=summary(restore),
            kind=body.kind,
            effective_on=body.effective_on.isoformat(),
            source_hash=digest([c.chunk_hash for c in [*chunks, *restore_chunks]]),
            governance_snapshot=digest(
                s.scalars(
                    select(PolicyPublicationEvent.event_id).order_by(
                        PolicyPublicationEvent.event_id
                    )
                ).all()
            ),
        )
        view["preview_digest"] = digest(view)
        return view, p, restore, restore_chunks

    def matrix_lifecycle_preview(self, pid, body):
        with Session(self.engine) as s, s.begin():
            self.lock(s)
            return self.lifecycle_preview_in_session(s, pid, body)[0]

    def matrix_lifecycle(self, pid, body, actor):
        fp = digest(dict(pid=pid, actor=actor, **body.model_dump(mode="json")))
        with Session(self.engine) as s, s.begin():
            self.lock(s)
            old = self.replay(s, body.idempotency_key, fp)
            if old:
                return old
            view, p, restore, chunks = self.lifecycle_preview_in_session(s, pid, body)
            if body.preview_digest != view["preview_digest"]:
                fail("PREVIEW_STALE", "预览已变化")
            stamp = self.clock()
            eid = str(uuid4())
            new = None
            if restore:
                new = self.copy_version(
                    s,
                    restore,
                    chunks,
                    pid="P4A-" + str(uuid4()),
                    version=restore.version + "-rollback-" + eid[:8],
                    start=body.effective_on,
                    end=p.effective_to,
                    rules=restore.authoritative_rules,
                    timestamp=stamp,
                )
            result = dict(
                kind=body.kind,
                event_id=eid,
                policy=summary(new),
                predecessor_id=pid,
                actor=actor,
                reason=body.reason,
                recorded_at=stamp.isoformat(),
            )
            s.add(
                PolicyPublicationEvent(
                    event_id=eid,
                    idempotency_key=body.idempotency_key,
                    request_sha256=fp,
                    kind=body.kind,
                    policy_version_id=new.policy_version_id if new else None,
                    predecessor_id=pid,
                    restore_id=body.restore_id,
                    upload_id=None,
                    review_id=None,
                    effective_on=body.effective_on,
                    recorded_at=stamp,
                    actor=actor,
                    summary=view,
                    result=result,
                )
            )
            s.flush()
            return result
