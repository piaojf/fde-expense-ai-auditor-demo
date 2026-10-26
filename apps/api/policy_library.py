"""Read-only uploaded-file projection; publication is distinct from draft indexing."""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import PolicyPublicationEvent, PolicyUpload, PolicyVersion


def owned_file_ids(draft, resource_owners, company, now):
    """Recognize published pre-portal sources without backfilling tenant records.

    An explicit owner always wins. Unbound drafts are never inferred from metadata alone.
    """
    owned = {uid for uid, owner in resource_owners.items() if owner == company}
    with Session(draft.engine) as session:
        legacy = session.scalars(
            select(PolicyUpload.upload_id)
            .join(PolicyVersion,
                  PolicyVersion.authoritative_rules["upload_id"].astext == PolicyUpload.upload_id)
            .join(PolicyPublicationEvent,
                  PolicyPublicationEvent.policy_version_id == PolicyVersion.policy_version_id)
            .where(
                PolicyVersion.legal_entity == company, PolicyVersion.authority_level >= 100,
                PolicyVersion.status.in_(["ACTIVE", "RETIRED"]),
                PolicyVersion.published_at <= now,
                PolicyVersion.authoritative_rules["file_sha256"].astext == PolicyUpload.sha256,
                PolicyUpload.confirmed_metadata["legal_entity"].astext == company,
                PolicyUpload.state != "DELETED",
                PolicyPublicationEvent.kind.in_(["PUBLISH", "ROLLBACK"]),
                PolicyPublicationEvent.recorded_at <= now,
            )
        ).all()
    return owned | {uid for uid in legacy if uid not in resource_owners}


def publication_status(start, end, cutovers, as_of, *, retired=False):
    past = [kind for day, kind in cutovers if day <= as_of]
    if past:
        return "REPLACED" if any(kind in {"PUBLISH", "ROLLBACK"} for kind in past) else "RETIRED"
    if retired:
        return "RETIRED"
    if start > as_of:
        return "SCHEDULED"
    if end is not None and as_of >= end:
        return "EXPIRED"
    return "EFFECTIVE"


def uploaded_files(draft, owned_ids, company, now, travel):
    """Project only owned files; no parser, indexer, model or repository mutation."""
    as_of = now.astimezone(timezone(timedelta(hours=8))).date()
    if not owned_ids:
        return dict(items=[], as_of=as_of.isoformat())
    with Session(draft.engine) as session:
        uploads = session.execute(
            select(PolicyUpload.upload_id, PolicyUpload.filename, PolicyUpload.state,
                   PolicyUpload.confirmed_metadata)
            .where(PolicyUpload.upload_id.in_(owned_ids), PolicyUpload.state != "DELETED")
            .order_by(PolicyUpload.created_at, PolicyUpload.upload_id)
        ).all()
        versions = session.scalars(
            select(PolicyVersion).where(
                PolicyVersion.legal_entity == company, PolicyVersion.authority_level >= 100,
                PolicyVersion.authoritative_rules["upload_id"].astext.in_(owned_ids),
                PolicyVersion.published_at <= now,
            )
        ).all()
        events = session.scalars(
            select(PolicyPublicationEvent).where(
                PolicyPublicationEvent.predecessor_id.in_([v.policy_version_id for v in versions]),
                PolicyPublicationEvent.recorded_at <= now,
            )
        ).all()
        items = []
        retirements = travel.events(company, "TRAVEL_POLICY_RETIRED")
        for upload in uploads:
            metadata = upload.confirmed_metadata or {}
            statuses = []
            for version in versions:
                if version.authoritative_rules.get("upload_id") != upload.upload_id:
                    continue
                cutovers = [(e.effective_on, e.kind) for e in events
                            if e.predecessor_id == version.policy_version_id]
                statuses.append(publication_status(
                    version.effective_from, version.effective_to, cutovers, as_of,
                    retired=version.status == "RETIRED"))
            for version in travel.publications(company, upload.upload_id):
                if datetime.fromisoformat(version["published_at"]) > now:
                    continue
                scope = version["metadata"]
                statuses.append(publication_status(
                    date.fromisoformat(scope["effective_from"]),
                    date.fromisoformat(scope["effective_to"]) if scope["effective_to"] else None,
                    [(date.fromisoformat(e["effective_on"]), "RETIRE") for e in retirements
                     if e["policy_id"] == version["policy_id"]
                     and datetime.fromisoformat(e["at"]) <= now],
                    as_of))
            status = next((s for s in ("EFFECTIVE", "SCHEDULED", "REPLACED", "RETIRED", "EXPIRED")
                           if s in statuses), "DRAFT")
            if not statuses and upload.state == "FAILED":
                status = "IMPORT_FAILED"
            items.append(dict(upload_id=upload.upload_id, filename=upload.filename,
                              version=metadata.get("version"), status=status,
                              effective_from=metadata.get("effective_from"),
                              effective_to=metadata.get("effective_to")))
        return dict(items=items, as_of=as_of.isoformat())
