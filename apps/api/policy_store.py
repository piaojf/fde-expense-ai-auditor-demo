"""PostgreSQL draft lifecycle; uploaded clauses never become executable rules."""

import hashlib
import re
import threading
from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from apps.api.errors import APIError
from database.models import PolicyChunk, PolicyPublicationEvent, PolicyUpload, PolicyVersion
from rag.embeddings.providers import validate_vectors
from rag.ingestion.postgres import chunk_policy
from rag.retrieval.draft import PgVectorDraftRetriever

DEMO_WARNING = "本机受控 Demo，非正式制度发布"


def safe_filename(filename):
    return (
        re.sub(r"[^\w.()\-\u4e00-\u9fff ]", "_", filename.replace("\\", "/").split("/")[-1])[:200]
        or "policy"
    )


def serialize(upload, *, clauses=None):
    return dict(
        upload_id=upload.upload_id,
        policy_version_id=upload.policy_version_id,
        status=upload.status,
        state=upload.state,
        filename=upload.filename,
        content_type=upload.content_type,
        size=upload.size,
        sha256=upload.sha256,
        source_uri=f"upload://policies/{upload.upload_id}",
        parsed=upload.parsed,
        metadata=upload.confirmed_metadata,
        error_code=upload.error_code,
        created_at=upload.created_at.isoformat(),
        clauses=clauses or [],
        demo_warning=DEMO_WARNING,
    )


class PolicyDraftStore:
    def __init__(self, engine, parser, embedding_factory):
        self.engine, self.parser, self.embedding_factory = engine, parser, embedding_factory
        self.lock = threading.RLock()  # Explicitly controlled single-worker local demo.

    def get(self, session, upload_id, *, mutable=True):
        row = session.scalar(
            select(PolicyUpload)
            .where(
                PolicyUpload.upload_id == str(upload_id),
            )
            .with_for_update()
        )
        if row is None:
            raise APIError(404, "POLICY_NOT_FOUND", "制度草稿不存在")
        if mutable and row.state == "DELETED":
            raise APIError(409, "POLICY_DELETED", "制度草稿已删除")
        if row.status != "DRAFT":
            raise APIError(409, "DRAFT_REQUIRED", "仅允许操作草稿制度")
        return row

    def list(self):
        with Session(self.engine) as session:
            rows = session.scalars(
                select(PolicyUpload)
                .where(
                    PolicyUpload.state != "DELETED",
                )
                .order_by(PolicyUpload.created_at.desc())
                .limit(50)
            ).all()
            return {"items": [serialize(row) for row in rows], "demo_warning": DEMO_WARNING}

    def upload(self, raw, filename):
        digest = hashlib.sha256(raw).hexdigest()
        with self.lock, Session(self.engine) as session, session.begin():
            # Serializes quota and duplicate allocation across workers; no other app uses this key.
            session.execute(text("SELECT pg_advisory_xact_lock(1330133057)"))
            existing = session.scalar(
                select(PolicyUpload).where(
                    PolicyUpload.sha256 == digest,
                    PolicyUpload.state != "DELETED",
                )
            )
            if existing:
                expected = ".pdf" if existing.content_type == "application/pdf" else ".docx"
                if not filename.lower().endswith(expected):
                    raise APIError(422, "INVALID_FILE", "文件扩展名与真实内容不匹配")
                return serialize(existing)
            if (
                session.scalar(
                    select(func.count())
                    .select_from(PolicyUpload)
                    .where(
                        PolicyUpload.state != "DELETED",
                    )
                )
                >= 50
            ):
                raise APIError(409, "POLICY_QUOTA", "本机最多保留 50 份草稿，请先清理")
            parsed = self.parser.parse(raw, filename)
            uid = str(uuid4())
            row = PolicyUpload(
                upload_id=uid,
                policy_version_id="DRAFT-" + uid,
                state="PARSED",
                status="DRAFT",
                filename=safe_filename(filename),
                content_type=parsed["content_type"],
                size=len(raw),
                sha256=digest,
                original=raw,
                parsed=parsed,
                confirmed_metadata=None,
                error_code=None,
                created_at=datetime.now(UTC),
            )
            session.add(row)
            session.flush()
            return serialize(row)

    def read(self, uid):
        with self.lock, Session(self.engine) as session, session.begin():
            row = self.get(session, uid, mutable=False)
            chunks = session.scalars(
                select(PolicyChunk)
                .where(
                    PolicyChunk.policy_version_id == row.policy_version_id,
                )
                .order_by(PolicyChunk.section)
            ).all()
            return serialize(
                row,
                clauses=[
                    dict(chunk_id=c.chunk_id, quote=c.content, source=c.source_metadata)
                    for c in chunks
                ],
            )

    def original(self, uid):
        with self.lock, Session(self.engine) as session, session.begin():
            row = self.get(session, uid)
            if row.original is None or hashlib.sha256(row.original).hexdigest() != row.sha256:
                raise APIError(409, "FILE_INTEGRITY", "原文件完整性校验失败")
            return row.original, row.content_type, row.filename

    def confirm(self, uid, body):
        metadata = body.metadata.model_dump(mode="json")
        with self.lock, Session(self.engine) as session, session.begin():
            row = self.get(session, uid)
            if row.confirmed_metadata is not None and row.confirmed_metadata != metadata:
                raise APIError(409, "METADATA_FROZEN", "已确认元数据不可覆盖，请新建对应版本")
            if row.state == "PARSED":
                row.confirmed_metadata = metadata
                row.state = "CONFIRMED"
            return serialize(row)

    def indexed_version(self, session, row):
        version = session.get(PolicyVersion, row.policy_version_id, with_for_update=True)
        m = row.confirmed_metadata
        if (
            version is None
            or version.status != "DRAFT"
            or not m
            or version.source_uri != f"upload://policies/{row.upload_id}"
            or version.authoritative_rules != {}
            or version.authority_level != 0
            or version.legal_entity != m["legal_entity"]
            or version.city != m["region"]
            or version.category != m["expense_type"]
            or version.version != m["version"]
            or version.effective_from.isoformat() != m["effective_from"]
            or (version.effective_to.isoformat() if version.effective_to else None)
            != m["effective_to"]
        ):
            raise APIError(409, "DRAFT_REQUIRED", "草稿制度状态或来源不匹配")
        return version

    def ingest(self, uid, *, remote_authorized=False):
        # No model construction/call on upload, confirmation or indexed replay.
        with self.lock:
            try:
                with Session(self.engine) as session, session.begin():
                    row = self.get(session, uid)
                    if row.state == "INDEXED":
                        self.indexed_version(session, row)
                        return serialize(row)
                    if not row.confirmed_metadata:
                        raise APIError(409, "CONFIRM_REQUIRED", "请先人工确认全部元数据")
                    if (
                        row.original is None
                        or hashlib.sha256(row.original).hexdigest() != row.sha256
                    ):
                        raise APIError(409, "FILE_INTEGRITY", "原文件完整性校验失败")
                    embedding = self.embedding_factory(remote_authorized)
                    if embedding.dimension != PolicyChunk.__table__.c.embedding.type.dim:
                        raise ValueError("Provider dimension mismatch")
                    chunks = chunk_policy(row.parsed["text"])
                    vectors = validate_vectors(
                        embedding.embed_documents(chunks),
                        dimension=embedding.dimension,
                        count=len(chunks),
                    )
                    metadata = row.confirmed_metadata
                    # INSERT only. No upsert can overwrite an existing ACTIVE version.
                    session.add(
                        PolicyVersion(
                            policy_version_id=row.policy_version_id,
                            policy_key=row.policy_version_id,
                            title=metadata["title"],
                            version=metadata["version"],
                            status="DRAFT",
                            authority_level=0,
                            legal_entity=metadata["legal_entity"],
                            category=metadata["expense_type"],
                            city=metadata["region"],
                            effective_from=date.fromisoformat(metadata["effective_from"]),
                            effective_to=(
                                date.fromisoformat(metadata["effective_to"])
                                if metadata["effective_to"]
                                else None
                            ),
                            published_at=datetime.now(UTC),
                            authoritative_rules={},
                            source_uri=f"upload://policies/{row.upload_id}",
                            content_sha256=hashlib.sha256(row.parsed["text"].encode()).hexdigest(),
                        )
                    )
                    session.flush()
                    for index, (content, vector) in enumerate(zip(chunks, vectors, strict=True)):
                        digest = hashlib.sha256(content.encode()).hexdigest()
                        start = row.parsed["text"].find(content)
                        end = start + len(content)
                        sources = [
                            line
                            for line in row.parsed["lines"]
                            if line["end"] > start and line["start"] < end
                        ]
                        session.add(
                            PolicyChunk(
                                chunk_id=f"{row.policy_version_id}:{digest[:32]}",
                                policy_version_id=row.policy_version_id,
                                content=content,
                                chunk_hash=digest,
                                section=f"section-{index + 1:03d}",
                                embedding=vector,
                                source_metadata=dict(
                                    upload_id=row.upload_id,
                                    file_sha256=row.sha256,
                                    source_uri=f"upload://policies/{row.upload_id}",
                                    embedding_model=embedding.model_name,
                                    embedding_dimension=embedding.dimension,
                                    version=metadata["version"],
                                    metadata=metadata,
                                    start=start,
                                    end=end,
                                    lines=sources,
                                ),
                            )
                        )
                    session.flush()
                    row.state, row.error_code = "INDEXED", None
                    return serialize(row)
            except APIError:
                raise
            except Exception as exc:
                # Failed transaction contains no partial versions/chunks; preserve retryable intake.
                with Session(self.engine) as session, session.begin():
                    row = self.get(session, uid, mutable=False)
                    if row.state not in {"DELETED", "INDEXED"}:
                        row.state, row.error_code = "FAILED", "DRAFT_IMPORT_FAILED"
                raise APIError(
                    503,
                    "DRAFT_IMPORT_FAILED",
                    "草稿导入失败，未改变正式制度；请检查模型或数据库后重试",
                ) from exc

    def preview(self, uid, body):
        with self.lock, Session(self.engine) as session, session.begin():
            row = self.get(session, uid)
            if row.state != "INDEXED":
                raise APIError(409, "INDEX_REQUIRED", "请先建立草稿索引")
            metadata = row.confirmed_metadata
            for key, value in (
                ("legal_entity", body.legal_entity),
                ("region", body.region),
                ("expense_type", body.expense_type),
                ("version", body.version),
            ):
                if metadata[key] != value:
                    raise APIError(
                        409, "DRAFT_SCOPE_MISMATCH", "查询公司、地区、费用类型或版本不符"
                    )
            if (
                body.incurred_on < date.fromisoformat(metadata["effective_from"])
                or metadata["effective_to"]
                and body.incurred_on
                >= date.fromisoformat(
                    metadata["effective_to"],
                )
            ):
                raise APIError(409, "DRAFT_DATE_MISMATCH", "查询日期不在草稿生效区间内")
            self.indexed_version(session, row)
            embedding = self.embedding_factory(body.remote_authorized)
            try:
                results = PgVectorDraftRetriever(embedding).retrieve(
                    session, row.policy_version_id, body
                )
            except ValueError as exc:
                raise APIError(409, "EMBEDDING_MISMATCH", "模型或向量维度不匹配") from exc
            except Exception as exc:
                raise APIError(503, "DRAFT_PREVIEW_FAILED", "草稿检索依赖不可用") from exc
            return dict(
                policy_version_id=row.policy_version_id,
                results=results,
                status="DRAFT",
                model=embedding.model_name,
                dimension=embedding.dimension,
                demo_warning=DEMO_WARNING,
            )

    def delete(self, uid):
        with self.lock, Session(self.engine) as session, session.begin():
            row = self.get(session, uid, mutable=False)
            if row.state == "DELETED":
                return serialize(row)
            if session.scalar(select(PolicyPublicationEvent.event_id).where(
                PolicyPublicationEvent.upload_id == str(uid)
            )):
                raise APIError(409, "PUBLISHED_SOURCE_PROTECTED", "已发布来源必须保留，不允许删除")
            version = session.get(PolicyVersion, row.policy_version_id, with_for_update=True)
            if version is not None:
                if version.status != "DRAFT" or version.source_uri != f"upload://policies/{uid}":
                    raise APIError(409, "DRAFT_REQUIRED", "禁止删除已发布或不属于此上传的制度")
                session.execute(
                    delete(PolicyChunk).where(
                        PolicyChunk.policy_version_id == row.policy_version_id,
                    )
                )
                session.delete(version)
            row.state, row.original, row.parsed = "DELETED", None, {}
            row.error_code = None
            return serialize(row)
