"""Controlled, append-only policy releases in one PostgreSQL transaction."""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from apps.api.errors import APIError
from database.models import PolicyChunk, PolicyPublicationEvent, PolicyRuleReview, PolicyVersion
from integrations.policies.rules import extract_hotel_rule
from rag.embeddings.providers import DeterministicTestEmbedding, validate_vectors
from rag.ingestion.postgres import chunk_policy
from rag.retrieval.pgvector import resolve_in_session

WARNING = "本机显式制度发布 Demo；非生产 RBAC，非双人审批"


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")
        ).encode()
    ).hexdigest()


def fail(code, message):
    raise APIError(409, code, message)


def scope(p):
    return p.legal_entity, p.city, p.category


def summary(p):
    if p is None:
        return None
    return dict(
        policy_version_id=p.policy_version_id,
        version=p.version,
        effective_from=p.effective_from.isoformat(),
        effective_to=p.effective_to.isoformat() if p.effective_to else None,
        amount=p.authoritative_rules.get("hotel_limit_per_night"),
        legal_entity=p.legal_entity,
        region=p.city,
        expense_type=p.category,
        rules=p.authoritative_rules,
        source_uri=p.source_uri,
        content_sha256=p.content_sha256,
    )


def review_json(r):
    return dict(
        review_id=r.review_id,
        revision=r.revision,
        extraction=r.extraction,
        confirmed_rule=r.confirmed_rule,
        actor=r.actor,
        reason=r.reason,
        recorded_at=r.recorded_at.isoformat(),
        file_sha256=r.file_sha256,
    )


class PolicyPublicationStore:
    def __init__(self, draft, embedding, *, app_env="development", clock=None):
        self.draft, self.engine, self.embedding = draft, draft.engine, embedding
        self.app_env = app_env
        self.clock = clock or (lambda: datetime.now(UTC))

    def lock(self, session):
        # One local governance lock avoids lock-order inversions across related scopes.
        # Intake serializes upload quota separately; both paths lock the upload row.
        session.execute(text("SELECT pg_advisory_xact_lock(1330133058)"))

    def today(self):
        return self.clock().astimezone(timezone(timedelta(hours=8))).date()

    def future(self, value):
        if value <= self.today():
            fail("FUTURE_EFFECTIVE_REQUIRED", "发布、退休和回退必须从未来日期生效，不改写历史")

    def latest_review(self, session, uid):
        return session.scalar(
            select(PolicyRuleReview)
            .where(PolicyRuleReview.upload_id == str(uid))
            .order_by(PolicyRuleReview.revision.desc())
            .limit(1)
        )

    def source(self, row):
        if row.original is None or hashlib.sha256(row.original).hexdigest() != row.sha256:
            fail("FILE_INTEGRITY", "制度原文件 SHA-256 校验失败")
        parsed = self.draft.parser.parse(row.original, row.filename)
        if parsed != row.parsed:
            fail("PARSE_INTEGRITY", "解析文本、原始提取或来源位置与原文件不一致")
        return extract_hotel_rule(parsed)

    def read(self, uid):
        with Session(self.engine) as s, s.begin():
            row = self.draft.get(s, uid)
            extraction = extract_hotel_rule(row.parsed)
            reviews = s.scalars(
                select(PolicyRuleReview)
                .where(PolicyRuleReview.upload_id == str(uid))
                .order_by(PolicyRuleReview.revision)
            ).all()
            events = s.scalars(
                select(PolicyPublicationEvent)
                .where(PolicyPublicationEvent.upload_id == str(uid))
                .order_by(PolicyPublicationEvent.recorded_at)
            ).all()
            return dict(
                upload_id=str(uid),
                extraction=extraction,
                reviews=[review_json(r) for r in reviews],
                publications=[e.result for e in events],
                demo_warning=WARNING,
            )

    def confirmation_scope(self, row):
        if not row.confirmed_metadata or row.confirmed_metadata["expense_type"] != "HOTEL":
            fail("HOTEL_METADATA_REQUIRED", "请先确认 HOTEL 制度元数据；其他费用保持 UNKNOWN")
        return {}

    def publication_metadata(self, row, review):
        return row.confirmed_metadata

    def publication_source(self, source, row, review):
        return source

    def extraction_guard(self, extraction):
        if extraction["blockers"]:
            fail("RULE_EXTRACTION_BLOCKED", "限额缺失、冲突或解析不确定，不允许确认发布规则")

    def confirmation_candidate(self, row, body, extraction):
        self.extraction_guard(extraction)
        candidate = next(
            (
                c
                for c in extraction["candidates"]
                if c["start"] == body.clause_start
                and c["end"] == body.clause_end
                and Decimal(c["amount"]) == body.amount
            ),
            None,
        )
        if candidate is None:
            fail("RULE_SOURCE_MISMATCH", "确认金额必须在选定原文条款中有可验证依据")
        return candidate

    def confirmation_details(self, row, body, candidate, extraction):
        return {}

    def review_matches(self, row, review, extraction):
        return review.confirmed_rule["clause"] in extraction["candidates"]

    def snapshot_rule_matches(self, session, policy, content, extraction):
        limit = policy.authoritative_rules.get("hotel_limit_per_night")
        return (
            not extraction["blockers"]
            and bool(limit)
            and all(Decimal(c["amount"]) == Decimal(limit) for c in extraction["candidates"])
        )

    def confirm(self, uid, body):
        with Session(self.engine) as s, s.begin():
            self.lock(s)
            row = self.draft.get(s, uid)
            if s.scalar(
                select(PolicyPublicationEvent.event_id).where(
                    PolicyPublicationEvent.upload_id == str(uid)
                )
            ):
                fail("ALREADY_PUBLISHED", "已发布来源及审核不可修改，请新建版本")
            rule_scope = self.confirmation_scope(row)
            extraction = self.source(row)
            candidate = self.confirmation_candidate(row, body, extraction)
            latest = self.latest_review(s, uid)
            revision = latest.revision if latest else 0
            if body.expected_revision != revision:
                fail("REVIEW_STALE", "审核记录已变化，请重新读取后确认")
            m = row.confirmed_metadata
            confirmed = dict(
                rule_type=body.rule_type,
                amount=f"{body.amount:.2f}",
                currency=body.currency,
                clause=candidate,
                metadata=m,
                original_metadata=row.parsed["extracted_metadata"],
                metadata_changes={
                    k: dict(original=v["value"], confirmed=m[k])
                    for k, v in row.parsed["extracted_metadata"].items()
                    if v["value"] != (m[k] or "UNKNOWN")
                },
                source_uri=f"upload://policies/{uid}",
                file_sha256=row.sha256,
                **rule_scope,
                **self.confirmation_details(row, body, candidate, extraction),
            )
            r = PolicyRuleReview(
                review_id=str(uuid4()),
                upload_id=str(uid),
                revision=revision + 1,
                file_sha256=row.sha256,
                extraction=extraction,
                confirmed_rule=confirmed,
                actor=body.actor,
                reason=body.reason,
                recorded_at=self.clock(),
            )
            s.add(r)
            s.flush()
            return review_json(r)

    def chunks(self, s, p, *, row=None):
        chunks = s.scalars(
            select(PolicyChunk)
            .where(PolicyChunk.policy_version_id == p.policy_version_id)
            .order_by(PolicyChunk.section)
        ).all()
        provider = self.embedding
        if provider is None:
            fail("RETRIEVER_NOT_PGVECTOR", "发布需要实际配置的 PostgreSQL 权威检索器")
        if isinstance(provider, DeterministicTestEmbedding) and self.app_env != "test":
            fail("TEST_EMBEDDING_FORBIDDEN", "测试向量不能用于真实模型检索")
        if not chunks or provider.dimension != 1024:
            fail("EMBEDDING_MISMATCH", "向量索引缺失或维度与实际 Provider 不兼容")
        for c in chunks:
            if c.chunk_hash != hashlib.sha256(c.content.encode()).hexdigest():
                fail("CHUNK_INTEGRITY", "条款摘要不匹配")
            if (
                c.source_metadata.get("embedding_model") != provider.model_name
                or c.source_metadata.get("embedding_dimension") != provider.dimension
            ):
                fail("EMBEDDING_MISMATCH", "索引与实际 Embedding Provider 模型及维度不匹配")
            try:
                validate_vectors(
                    [list(c.embedding)] if c.embedding is not None else [],
                    dimension=provider.dimension,
                    count=1,
                )
            except (ValueError, TypeError) as exc:
                raise APIError(409, "EMBEDDING_MISMATCH", "向量缺失或无效") from exc
            if row is not None:
                meta = c.source_metadata
                start, end = meta.get("start"), meta.get("end")
                if (
                    meta.get("upload_id") != row.upload_id
                    or meta.get("file_sha256") != row.sha256
                    or meta.get("metadata") != row.confirmed_metadata
                    or not isinstance(start, int)
                    or not isinstance(end, int)
                    or row.parsed["text"][start:end] != c.content
                ):
                    fail("CHUNK_SOURCE_MISMATCH", "条款与原文件或已确认元数据关联不一致")
        if row is not None and [c.content for c in chunks] != chunk_policy(row.parsed["text"]):
            fail("CHUNK_INTEGRITY", "制度条款索引不完整")
        if row is None:
            # Verify an immutable seed/release against its complete original content.
            # Seed chunk windows use the established 800/100 ingestion boundaries.
            ordered = sorted(chunks, key=lambda c: int(c.section.rsplit("-", 1)[-1]))
            content = ordered[0].content
            for c in ordered[1:]:
                if content[-100:] != c.content[:100]:
                    fail("CONTENT_INTEGRITY", "历史条款窗口无法验证原文完整性")
                content += c.content[100:]
            if hashlib.sha256(content.encode()).hexdigest() != p.content_sha256:
                fail("CONTENT_INTEGRITY", "历史制度正文与摘要不一致")
            extraction = extract_hotel_rule(
                dict(
                    text=content,
                    lines=[
                        dict(
                            text=line,
                            start=start,
                            end=start + len(line),
                            line=i + 1,
                            page=None,
                            method="validated-snapshot",
                            confidence=None,
                        )
                        for i, (start, line) in enumerate(self.content_lines(content))
                    ],
                )
            )
            if not self.snapshot_rule_matches(s, p, content, extraction):
                fail("RESTORE_RULE_SOURCE_INVALID", "历史住宿规则无法与原文条款一致验证")
        return chunks

    @staticmethod
    def content_lines(content):
        start = 0
        for line in content.split("\n"):
            yield start, line
            start += len(line) + 1

    def scope_rows(self, s, p):
        return s.scalars(
            select(PolicyVersion)
            .where(
                PolicyVersion.status == "ACTIVE",
                PolicyVersion.legal_entity == p.legal_entity,
                PolicyVersion.city.in_([p.city, "*"]),
                PolicyVersion.category == p.category,
            )
            .order_by(PolicyVersion.policy_version_id)
        ).all()

    def check_replacement(self, s, p, predecessor, *, exclude=()):
        # Explicit successor defines priority. Bare effective dates never legitimize a conflict.
        if p.city == "*" or p.category != "HOTEL":
            fail("UNSUPPORTED_SCOPE", "本阶段只发布明确地区的 HOTEL 规则")
        existing = [x for x in self.scope_rows(s, p) if x.authority_level >= 100]
        if any(x.version == p.version for x in existing if x.policy_version_id not in exclude):
            fail("VERSION_CONFLICT", "相同范围的制度版本已存在")
        overlaps = []
        events = s.scalars(select(PolicyPublicationEvent)).all()
        for x in existing:
            if x.policy_version_id in exclude:
                continue
            ends = [e.effective_on for e in events if e.predecessor_id == x.policy_version_id]
            end = min([*ends, *([x.effective_to] if x.effective_to else [])], default=date.max)
            if x.effective_from < (p.effective_to or date.max) and p.effective_from < end:
                overlaps.append(x)
        if predecessor is None:
            if overlaps:
                fail("PREDECESSOR_REQUIRED", "存在日期重叠，必须显式确认唯一旧版本替代关系")
        elif (
            len(overlaps) != 1
            or overlaps[0].policy_version_id != predecessor
            or scope(overlaps[0]) != scope(p)
            or overlaps[0].effective_from >= p.effective_from
            or overlaps[0].authority_level != 100
        ):
            fail("POLICY_CONFLICT", "制度范围、时间或权威冲突，无法唯一确定旧版本")
        if predecessor and any(e.predecessor_id == predecessor for e in events):
            fail("SUCCESSOR_CONFLICT", "旧版本已有退休或后继，禁止并发安排另一个后继")
        return overlaps[0] if overlaps else None

    def preview_in_session(self, s, uid, predecessor):
        row = self.draft.get(s, uid)
        if row.state != "INDEXED":
            fail("INDEX_REQUIRED", "请先建立草稿索引")
        extraction = self.source(row)
        self.extraction_guard(extraction)
        r = self.latest_review(s, uid)
        if r is None:
            fail("RULE_CONFIRM_REQUIRED", "请先人工确认住宿限额及原文来源")
        p = self.draft.indexed_version(s, row)
        if (
            r.file_sha256 != row.sha256
            or r.extraction != extraction
            or r.confirmed_rule["metadata"] != row.confirmed_metadata
            or not self.review_matches(row, r, extraction)
        ):
            fail("REVIEW_INTEGRITY", "规则审核与原文来源不一致")
        publication_metadata = self.publication_metadata(row, r)
        for key in (
            "legal_entity",
            "region",
            "expense_type",
            "version",
            "effective_from",
            "effective_to",
        ):
            original = row.parsed["extracted_metadata"][key]["value"]
            confirmed = publication_metadata[key]
            if key == "effective_to" and confirmed is None and original == "UNKNOWN":
                continue
            if original == "UNKNOWN" or original != confirmed:
                fail(
                    "METADATA_SOURCE_MISMATCH",
                    "适用范围、版本和日期必须在原文件中可验证；请上传修订原文",
                )
        if not any(
            r.confirmed_rule["clause"]["quote"] in c.content for c in self.chunks(s, p, row=row)
        ):
            fail("RULE_CLAUSE_NOT_INDEXED", "权威规则原文条款未完整关联到索引，禁止发布")
        self.future(p.effective_from)
        chunks = self.chunks(s, p, row=row)
        if p.content_sha256 != hashlib.sha256(row.parsed["text"].encode()).hexdigest():
            fail("CONTENT_INTEGRITY", "制度正文摘要不匹配")
        p = self.publication_source(p, row, r)
        old = self.check_replacement(s, p, predecessor)
        filters = dict(
            legal_entity=p.legal_entity,
            category=p.category,
            city=p.city,
            audit_started_at=self.clock(),
        )
        current = resolve_in_session(s, **filters, incurred_on=self.today())
        at_start = resolve_in_session(s, **filters, incurred_on=p.effective_from)
        events = s.scalars(
            select(PolicyPublicationEvent).order_by(PolicyPublicationEvent.event_id)
        ).all()
        view = dict(
            upload_id=str(uid),
            review_id=r.review_id,
            revision=r.revision,
            current=summary(current),
            old=summary(old),
            authority_at_start=summary(at_start),
            new={**summary(p), "amount": r.confirmed_rule["amount"]},
            changes=dict(
                hotel_limit_per_night=dict(
                    old=old.authoritative_rules.get("hotel_limit_per_night") if old else None,
                    new=r.confirmed_rule["amount"],
                )
            ),
            impact=dict(
                legal_entity=p.legal_entity,
                region=p.city,
                expense_type=p.category,
                from_date=p.effective_from.isoformat(),
                to_date=p.effective_to.isoformat() if p.effective_to else None,
                historical_and_inflight="Preserved at audit-start snapshot",
            ),
            clause=r.confirmed_rule["clause"],
            file_sha256=row.sha256,
            embedding_model=self.embedding.model_name,
            dimension=self.embedding.dimension,
            real_semantic_validation="NOT_RUN"
            if isinstance(self.embedding, DeterministicTestEmbedding)
            else "PROVIDER_COMPATIBLE_ONLY",
            blockers=[],
            demo_warning=WARNING,
            scope_snapshot=digest([summary(x) for x in self.scope_rows(s, p)]),
            governance_snapshot=digest([e.event_id for e in events]),
            chunk_snapshot=digest(
                [
                    dict(
                        id=c.chunk_id,
                        hash=c.chunk_hash,
                        meta=c.source_metadata,
                        vector=list(c.embedding),
                    )
                    for c in chunks
                ]
            ),
        )
        view["preview_digest"] = digest(view)
        return view, row, p, r, chunks

    def preview(self, uid, body):
        with Session(self.engine) as s, s.begin():
            self.lock(s)
            try:
                return self.preview_in_session(s, uid, body.predecessor_id)[0]
            except APIError as e:
                if e.status != 409:
                    raise
                return dict(blockers=[e.code], message=e.message, demo_warning=WARNING)

    def replay(self, s, key, fingerprint):
        e = s.scalar(
            select(PolicyPublicationEvent).where(PolicyPublicationEvent.idempotency_key == key)
        )
        if e:
            if e.request_sha256 != fingerprint:
                fail("IDEMPOTENCY_CONFLICT", "重复提交键绑定了不同操作")
            return e.result
        return None

    def copy_version(self, s, source, chunks, *, pid, version, start, end, rules, timestamp):
        p = PolicyVersion(
            policy_version_id=pid,
            policy_key="PUBLICATION-" + digest(scope(source))[:32],
            title=source.title,
            version=version,
            status="ACTIVE",
            authority_level=100,
            legal_entity=source.legal_entity,
            city=source.city,
            category=source.category,
            effective_from=start,
            effective_to=end,
            published_at=timestamp,
            authoritative_rules=rules,
            source_uri=source.source_uri,
            content_sha256=source.content_sha256,
        )
        s.add(p)
        s.flush()
        for i, c in enumerate(chunks):
            s.add(
                PolicyChunk(
                    chunk_id=f"{pid}:{c.chunk_hash[:32]}",
                    policy_version_id=pid,
                    content=c.content,
                    chunk_hash=c.chunk_hash,
                    section=f"section-{i + 1:03d}",
                    embedding=list(c.embedding),
                    source_metadata={
                        **c.source_metadata,
                        "version": version,
                        "copied_from_policy_version_id": source.policy_version_id,
                    },
                )
            )
        s.flush()
        return p

    def publish(self, uid, body):
        fp = digest({"upload_id": str(uid), **body.model_dump(mode="json", exclude={"token"})})
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
                fail("ALREADY_PUBLISHED", "此草稿已发布，请用原提交键重试")
            view, row, source, r, chunks = self.preview_in_session(s, uid, body.predecessor_id)
            if view["preview_digest"] != body.preview_digest:
                fail("PREVIEW_STALE", "发布预览已变化，请重新查看并显式确认")
            stamp, eid, pid = self.clock(), str(uuid4()), "PUB-" + str(uuid4())
            rules = dict(
                hotel_limit_per_night=r.confirmed_rule["amount"],
                currency="CNY",
                rule_review_id=r.review_id,
                source=r.confirmed_rule["clause"],
                upload_id=row.upload_id,
                file_sha256=row.sha256,
            )
            p = self.copy_version(
                s,
                source,
                chunks,
                pid=pid,
                version=source.version,
                start=source.effective_from,
                end=source.effective_to,
                rules=rules,
                timestamp=stamp,
            )
            result = dict(
                event_id=eid,
                kind="PUBLISH",
                policy=summary(p),
                actor=body.actor,
                recorded_at=stamp.isoformat(),
                predecessor_id=body.predecessor_id,
                review_id=r.review_id,
                demo_warning=WARNING,
            )
            s.add(
                PolicyPublicationEvent(
                    event_id=eid,
                    idempotency_key=body.idempotency_key,
                    request_sha256=fp,
                    kind="PUBLISH",
                    policy_version_id=pid,
                    predecessor_id=body.predecessor_id,
                    restore_id=None,
                    upload_id=str(uid),
                    review_id=r.review_id,
                    effective_on=p.effective_from,
                    recorded_at=stamp,
                    actor=body.actor,
                    summary=view,
                    result=result,
                )
            )
            s.flush()
            return result

    def lifecycle_view(self, s, pid, body):
        self.future(body.effective_on)
        target = s.get(PolicyVersion, pid, with_for_update=True)
        if target is None or target.status != "ACTIVE" or target.category != "HOTEL":
            fail("ACTIVE_REQUIRED", "必须选择已发布的 HOTEL 版本")
        if body.effective_on <= target.effective_from:
            fail("CUTOVER_DATE_INVALID", "退休或回退日期必须晚于目标版本开始日期")
        if target.effective_to and body.effective_on >= target.effective_to:
            fail("CUTOVER_DATE_INVALID", "目标制度在该日期已经失效")
        filters = dict(
            legal_entity=target.legal_entity,
            city=target.city,
            category=target.category,
            incurred_on=body.effective_on,
            audit_started_at=self.clock(),
        )
        active = resolve_in_session(s, **filters)
        if active is None or active.policy_version_id != pid:
            fail("POLICY_CONFLICT", "该日期的制度并非唯一目标版本")
        if s.scalar(
            select(PolicyPublicationEvent.event_id).where(
                PolicyPublicationEvent.predecessor_id == pid
            )
        ):
            fail("SUCCESSOR_CONFLICT", "版本已有未来后继或退休安排")
        self.chunks(s, target)
        restore = None
        if body.kind == "ROLLBACK":
            restore = s.get(PolicyVersion, body.restore_id or "", with_for_update=True)
            if (
                restore is None
                or restore.status != "ACTIVE"
                or scope(restore) != scope(target)
                or restore.policy_version_id == pid
                or restore.authority_level != 100
                or restore.effective_from >= target.effective_from
            ):
                fail("RESTORE_SCOPE_INVALID", "回退必须选同范围、较早的权威制度快照")
            amount = restore.authoritative_rules.get("hotel_limit_per_night")
            if not amount or Decimal(amount) <= 0:
                fail("RESTORE_RULE_INVALID", "历史制度缺少权威住宿规则")
            self.chunks(s, restore)
        elif body.restore_id is not None:
            fail("RESTORE_SCOPE_INVALID", "退休不能指定回退来源")
        all_rows = self.scope_rows(s, target)
        view = dict(
            kind=body.kind,
            target=summary(target),
            restore=summary(restore),
            effective_on=body.effective_on.isoformat(),
            blockers=[],
            demo_warning=WARNING,
            scope_snapshot=digest([summary(x) for x in all_rows]),
        )
        view["preview_digest"] = digest(view)
        return view, target, restore

    def lifecycle_preview(self, pid, body):
        with Session(self.engine) as s, s.begin():
            self.lock(s)
            try:
                return self.lifecycle_view(s, pid, body)[0]
            except APIError as e:
                return dict(blockers=[e.code], message=e.message, demo_warning=WARNING)

    def change_lifecycle(self, pid, body):
        fp = digest({"target": pid, **body.model_dump(mode="json", exclude={"token"})})
        with Session(self.engine) as s, s.begin():
            self.lock(s)
            replay = self.replay(s, body.idempotency_key, fp)
            if replay:
                return replay
            view, target, restore = self.lifecycle_view(s, pid, body)
            if view["preview_digest"] != body.preview_digest:
                fail("PREVIEW_STALE", "退休或回退预览已变化，请重新确认")
            eid, stamp, release = str(uuid4()), self.clock(), None
            if restore:
                chunks = self.chunks(s, restore)
                release = self.copy_version(
                    s,
                    restore,
                    chunks,
                    pid="ROLL-" + str(uuid4()),
                    version="rollback-" + eid,
                    start=body.effective_on,
                    end=target.effective_to,
                    rules={
                        **restore.authoritative_rules,
                        "restored_from": restore.policy_version_id,
                    },
                    timestamp=stamp,
                )
                self.check_replacement(s, release, pid, exclude=(release.policy_version_id,))
            result = dict(
                event_id=eid,
                kind=body.kind,
                policy=summary(release),
                predecessor_id=pid,
                restore_id=body.restore_id,
                actor=body.actor,
                reason=body.reason,
                recorded_at=stamp.isoformat(),
                effective_on=body.effective_on.isoformat(),
                demo_warning=WARNING,
            )
            s.add(
                PolicyPublicationEvent(
                    event_id=eid,
                    idempotency_key=body.idempotency_key,
                    request_sha256=fp,
                    kind=body.kind,
                    policy_version_id=release.policy_version_id if release else None,
                    predecessor_id=pid,
                    restore_id=body.restore_id,
                    upload_id=None,
                    review_id=None,
                    effective_on=body.effective_on,
                    recorded_at=stamp,
                    actor=body.actor,
                    summary=view,
                    result=result,
                )
            )
            s.flush()
            return result

    def history(self):
        with Session(self.engine) as s:
            events = s.scalars(
                select(PolicyPublicationEvent).order_by(
                    PolicyPublicationEvent.recorded_at, PolicyPublicationEvent.event_id
                )
            ).all()
            versions = s.scalars(
                select(PolicyVersion)
                .where(PolicyVersion.status == "ACTIVE")
                .order_by(PolicyVersion.effective_from)
            ).all()
            return dict(
                events=[e.result for e in events],
                versions=[summary(p) for p in versions],
                demo_warning=WARNING,
            )
