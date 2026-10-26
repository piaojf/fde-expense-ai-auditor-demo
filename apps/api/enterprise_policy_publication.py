"""Separate document classification from source-verified executable hotel scope."""

from decimal import Decimal
from types import SimpleNamespace

from apps.api.policy_publication_store import PolicyPublicationStore, fail
from database.models import PolicyRuleReview, PolicyVersion
from integrations.policies.enterprise_rules import (
    CONDITIONAL,
    execution_blockers,
    manual_candidate,
    reference_tables,
)


class EnterprisePolicyPublicationStore(PolicyPublicationStore):
    def read(self, uid):
        from sqlalchemy.orm import Session

        result = super().read(uid)
        with Session(self.engine) as session:
            row = self.draft.get(session, uid)
            result["source_lines"] = row.parsed["lines"]
            result["reference_tables"] = reference_tables(row)
            result["condition_warning"] = bool(CONDITIONAL.search(row.parsed["text"]))
        return result

    def extraction_guard(self, extraction):
        # MANUAL records retain automatic warnings. Confirmation and preview separately
        # revalidate the chosen source; invalid spans are never recoverable by editing.
        if "SOURCE_SPAN_INVALID" in extraction["blockers"]:
            fail("RULE_SOURCE_MISMATCH", "原文位置校验失败，请重新上传文件")

    def confirmation_candidate(self, row, body, extraction):
        if body.source_mode == "MANUAL":
            return manual_candidate(row, body.amount, body.clause_start, body.clause_end)
        PolicyPublicationStore.extraction_guard(self, extraction)
        return super().confirmation_candidate(row, body, extraction)

    def confirmation_details(self, row, body, candidate, extraction):
        blockers = execution_blockers(row, candidate, body.conditions, extraction)
        return dict(
            source_mode=body.source_mode,
            conditions=body.conditions,
            execution_status="REFERENCE_ONLY" if blockers else "READY",
            execution_blockers=blockers,
        )

    def review_matches(self, row, review, extraction):
        rule = review.confirmed_rule
        candidate = rule["clause"]
        if rule.get("source_mode") == "MANUAL":
            actual = manual_candidate(
                row, Decimal(rule["amount"]), candidate["start"], candidate["end"]
            )
            if actual != candidate:
                return False
        else:
            PolicyPublicationStore.extraction_guard(self, extraction)
            if not super().review_matches(row, review, extraction):
                return False
        blockers = execution_blockers(row, candidate, rule.get("conditions", ""), extraction)
        if blockers or rule.get("execution_status", "READY") != "READY":
            fail(
                "RULE_REFERENCE_ONLY",
                "此记录为原文规则草稿；职级、多个标准或缺少单位等条件尚不能自动执行，请按提示处理",
            )
        return (
            Decimal(candidate["amount"]) == Decimal(rule["amount"])
            and rule.get("execution_blockers", []) == blockers
        )

    def snapshot_rule_matches(self, session, policy, content, extraction):
        rules = policy.authoritative_rules
        review = session.get(PolicyRuleReview, rules.get("rule_review_id", ""))
        if review is None or review.confirmed_rule.get("source_mode") != "MANUAL":
            return super().snapshot_rule_matches(session, policy, content, extraction)
        row = self.draft.get(session, review.upload_id)
        original_extraction = self.source(row)
        metadata = self.publication_metadata(row, review)
        return (
            content == row.parsed["text"]
            and review.file_sha256 == row.sha256 == rules.get("file_sha256")
            and review.extraction == original_extraction
            and review.confirmed_rule["metadata"] == row.confirmed_metadata
            and rules.get("upload_id") == row.upload_id
            and rules.get("source") == review.confirmed_rule["clause"]
            and rules.get("hotel_limit_per_night") == review.confirmed_rule["amount"]
            and (policy.legal_entity, policy.city, policy.category)
            == (metadata["legal_entity"], metadata["region"], metadata["expense_type"])
            and self.review_matches(row, review, original_extraction)
        )

    def rule_scope(self, row):
        metadata = row.confirmed_metadata
        if not metadata:
            fail("HOTEL_METADATA_REQUIRED", "请先确认制度信息")
        original = row.parsed["extracted_metadata"]
        region = original["region"]["value"]
        expense = original["expense_type"]["value"]
        if region in {"UNKNOWN", "*", "ALL", "全部", "全国", "全部地区"} or expense != "HOTEL":
            fail(
                "RULE_SCOPE_SOURCE_REQUIRED",
                "住宿规则需要原文明确具体地区及费用类型；文档选择全部不会扩大规则范围",
            )
        if metadata["region"] not in {"*", region} or metadata["expense_type"] not in {
            "ALL",
            "HOTEL",
        }:
            fail("RULE_SCOPE_MISMATCH", "住宿规则适用范围与已确认的文档范围不符")
        return dict(region=region, expense_type="HOTEL")

    def confirmation_scope(self, row):
        return {"rule_scope": self.rule_scope(row)}

    def publication_metadata(self, row, review):
        rule_scope = self.rule_scope(row)
        recorded = review.confirmed_rule.get("rule_scope")
        # Preserve earlier narrow, source-verified reviews without rewriting them.
        if recorded != rule_scope and not (
            recorded is None
            and row.confirmed_metadata["region"] == rule_scope["region"]
            and row.confirmed_metadata["expense_type"] == "HOTEL"
        ):
            fail("REVIEW_INTEGRITY", "规则适用范围与原文或确认记录不一致")
        return {**row.confirmed_metadata, **rule_scope}

    def publication_source(self, source, row, review):
        metadata = self.publication_metadata(row, review)
        # A detached view keeps the immutable DRAFT and its broad document scope intact.
        values = {
            column.key: getattr(source, column.key) for column in PolicyVersion.__table__.columns
        }
        return SimpleNamespace(**{**values, "city": metadata["region"], "category": "HOTEL"})
