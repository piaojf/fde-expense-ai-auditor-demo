"""Source-confirmed travel standards, append-only publication and pinned deterministic checks."""

import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.orm import Session

from apps.api.errors import APIError
from domain.enterprise_travel import TravelDetails
from domain.schemas import PolicyEvidence, RuleResult
from integrations.policies.enterprise_rules import reference_tables
from rules.allowance import full_day_allowance


def reject(code, message):
    raise APIError(422, code, message)


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def today():
    return datetime.now(UTC).astimezone(timezone(timedelta(hours=8))).date()


def scope_matches(values, value):
    return "*" in values or value in values


class EnterpriseTravel:
    def __init__(self, store, publisher, *, facts=None):
        self.store, self.publisher = store, publisher
        self.facts = facts

    def require_schema(self):
        with self.store.lock:
            names = {
                r[0]
                for r in self.store.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND name LIKE 'enterprise_travel_%'"
                )
            }
        if len(names) != 3:
            raise APIError(
                503, "TRAVEL_SCHEMA_REQUIRED", "差旅标准数据库保护尚未安装，请管理员执行独立迁移"
            )

    def events(self, company, kind):
        with self.store.lock:
            rows = self.store.db.execute(
                "SELECT body FROM enterprise_events WHERE company_id=? ORDER BY seq", (company,)
            ).fetchall()
        return [v for (raw,) in rows if (v := json.loads(raw)).get("kind") == kind]

    def profile(self, user, incurred):
        if self.facts is not None:
            record = self.facts.resolve(user["company_id"], "EMPLOYEE", user["id"], incurred)
            return {
                **{key: user[key] for key in (
                    "id", "company_id", "name", "department", "position",
                )},
                "grade": record["value"]["grade"] if record else "UNKNOWN",
                "grade_effective_on": record["effective_from"] if record else None,
                "authority": "p4a.1", "grade_fact": record,
            }
        profiles = [
            e
            for e in self.events(user["company_id"], "TRAVEL_PROFILE_SET")
            if e["user_id"] == user["id"] and e["effective_on"] <= str(incurred)
        ]
        event = max(profiles, key=lambda e: (e["effective_on"], e["at"]), default={})
        return {
            **{key: user[key] for key in ("id", "company_id", "name", "department", "position")},
            "grade": event.get("grade", "UNKNOWN"),
            "grade_effective_on": event.get("effective_on"),
        }

    def set_profile(self, actor, uid, body):
        if self.facts is not None:
            raise APIError(409, "P4A_ROUTE_REQUIRED", "请从住宿职级标准页面维护有效期职级档案")
        self.require_schema()
        user = self.store.user(uid)
        if not user or user["company_id"] != actor["company_id"]:
            raise APIError(404, "USER_NOT_FOUND", "账号不存在")
        if not body.reason.strip():
            reject("REASON_REQUIRED", "请填写档案依据")
        with self.store.lock, self.store.db:
            self.store.event(
                actor, "TRAVEL_PROFILE_SET", user_id=uid, **body.model_dump(mode="json")
            )
        return self.profile(user, body.effective_on)

    def employee_snapshot(self, user, fields):
        snapshot = self.profile(user, fields.incurred_on)
        if self.facts is None:
            return snapshot
        matches = [
            r for r in self.facts.list(user["company_id"])
            if r["kind"] == "CITY"
            and r["effective_from"] <= str(fields.incurred_on) < r["effective_to"]
            and fields.city in [r["value"]["name"], *r["value"].get("aliases", [])]
        ]
        city = matches[0] if len(matches) == 1 else None
        blockers = []
        for record, label in ((snapshot["grade_fact"], "EMPLOYEE_GRADE"), (city, "CITY_TIER")):
            if record is None:
                blockers.append(label + "_UNKNOWN")
            elif not (record["effective_from"] <= str(fields.trip_start)
                      and str(fields.trip_end) < record["effective_to"]):
                blockers.append(label + "_CHANGES_DURING_TRIP")
        return {**snapshot, "city_fact": city, "p4a_blockers": blockers}

    def publications(self, company, uid=None):
        values = self.events(company, "TRAVEL_POLICY_PUBLISHED")
        return [
            e["snapshot"] for e in values if uid is None or e["snapshot"]["upload_id"] == str(uid)
        ]

    def source(self, uid, actor):
        self.store.resource("policy", uid, actor)
        publisher = self.publisher()
        with Session(publisher.engine) as session:
            row = publisher.draft.get(session, uid)
            if row.state not in {"CONFIRMED", "INDEXED"} or not row.confirmed_metadata:
                reject("POLICY_CONFIRM_REQUIRED", "请先确认制度信息")
            if row.confirmed_metadata["legal_entity"] != actor["company_id"]:
                reject("POLICY_COMPANY_MISMATCH", "制度所属企业不一致")
            publisher.source(row)  # Independently verifies original SHA and local reparse.
            return dict(
                upload_id=str(uid),
                sha256=row.sha256,
                parsed=row.parsed,
                metadata=row.confirmed_metadata,
                tables=reference_tables(row, all_tables=True),
            )

    def read(self, uid, actor):
        source = self.source(uid, actor)
        versions = self.publications(actor["company_id"], uid)
        retired = self.events(actor["company_id"], "TRAVEL_POLICY_RETIRED")
        return {
            **source,
            "versions": versions,
            "revision": len(versions),
            "retirements": [e for e in retired if e["upload_id"] == str(uid)],
        }

    def publish(self, uid, actor, body):
        if self.facts is not None and any(r.expense_type == "HOTEL" for r in body.rules):
            raise APIError(409, "P4A_ROUTE_REQUIRED", "住宿规则仅通过 P4A 16 格矩阵发布")
        self.require_schema()
        source = self.source(uid, actor)
        metadata = source["metadata"]
        if date.fromisoformat(metadata["effective_from"]) <= today():
            reject("FUTURE_EFFECTIVE_REQUIRED", "新标准须从未来日期生效，以保留历史审核依据")
        if not body.reason.strip():
            reject("REASON_REQUIRED", "请填写发布依据")
        text, lines = source["parsed"]["text"], source["parsed"]["lines"]
        rules = []
        for index, rule in enumerate(body.rules):
            selected = [
                line
                for line in lines
                if line["start"] >= rule.source_start and line["end"] <= rule.source_end
            ]
            if (
                not selected
                or selected[0]["start"] != rule.source_start
                or selected[-1]["end"] != rule.source_end
                or rule.source_end - rule.source_start > 6000
                or any(
                    line.get("confidence") is not None and line["confidence"] < 0.85
                    for line in selected
                )
            ):
                reject("RULE_SOURCE_REQUIRED", "请引用连续完整原文段落，低置信识别内容需重新上传")
            quote = text[rule.source_start : rule.source_end]
            if any(text[line["start"] : line["end"]] != line["text"] for line in selected):
                reject("RULE_SOURCE_MISMATCH", "原文位置校验失败")
            if rule.limit is not None:
                unit_patterns = {
                    "NIGHT": r"每晚|每夜|每间每晚|[/／]晚|per night",
                    "DAY": r"每天|每日|[/／](?:人[/／])?[天日]|per day",
                    "TRIP": r"每次|每程|单程|[/／]次|per trip",
                }
                if (
                    not re.search(r"元|人民币|CNY", quote, re.I)
                    or re.search(r"美元|港币|USD|EUR|HKD", quote, re.I)
                    or not re.search(unit_patterns[rule.unit], quote, re.I)
                ):
                    reject("RULE_UNIT_SOURCE", "请引用包含人民币及每晚／每天／每次口径的原文依据")
            # Human confirmation interprets conditions; numbers and seat names still require source.
            tokens = re.findall(
                r"(?<![\d.,\-−])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?(?![\d.,])", quote
            )
            amounts = {Decimal(t.replace(",", "")) for t in tokens}
            if rule.limit is not None and rule.limit not in amounts:
                reject("RULE_AMOUNT_SOURCE", "金额必须在引用的原文中出现")
            if rule.expense_type == "TRANSPORT" and (
                rule.transport_mode not in quote or any(seat not in quote for seat in rule.seats)
            ):
                reject("RULE_TRANSPORT_SOURCE", "交通工具和席别必须在引用的原文中出现")
            for rate in (rule.breakfast_rate, rule.lunch_rate, rule.dinner_rate):
                if rate and not re.search(rf"(?<!\d){int(rate * 100)}\s*[%％]", quote):
                    reject("RULE_DEDUCTION_SOURCE", "供餐扣减比例须有原文百分比依据")
            rules.append(
                {**rule.model_dump(mode="json"), "rule_id": f"TRAVEL-{index + 1}", "quote": quote}
            )
        # Overlapping standards cannot be silently resolved by order/specificity.
        for index, rule in enumerate(rules):
            for other in rules[:index]:
                if rule["expense_type"] != other["expense_type"]:
                    continue
                if (
                    rule["expense_type"] == "TRANSPORT"
                    and rule["transport_mode"] != other["transport_mode"]
                ):
                    continue
                if all(
                    "*" in rule[key] or "*" in other[key] or set(rule[key]) & set(other[key])
                    for key in ("cities", "departments", "positions", "grades")
                ):
                    reject(
                        "RULE_SCOPE_CONFLICT", "同一适用范围内存在重叠标准，请拆分城市、职级或部门"
                    )
        scope = actor["id"] + ":travel-publish:" + str(uid)
        with self.store.lock, self.store.db:
            replay = self.store.replay(scope, body.idempotency_key, body.model_dump_json())
            if replay:
                return json.loads(replay[2])
            versions = self.publications(actor["company_id"], uid)
            if len(versions) != body.expected_revision:
                raise APIError(409, "REVISION_CONFLICT", "标准版本已变化，请刷新")
            if versions:
                reject("TRAVEL_IMMUTABLE", "已发布文件不可覆盖；请上传新制度版本并退休旧标准")
            snapshot = dict(
                policy_id="TRAVEL-POL-" + uuid4().hex,
                upload_id=str(uid),
                company_id=actor["company_id"],
                metadata=metadata,
                rules=rules,
                file_sha256=source["sha256"],
                text_sha256=hashlib.sha256(source["parsed"]["text"].encode()).hexdigest(),
                published_at=datetime.now(UTC).isoformat(),
                actor_id=actor["id"],
                revision=len(versions) + 1,
                reason=body.reason,
            )
            snapshot["sha256"] = fingerprint(snapshot)
            self.store.event(actor, "TRAVEL_POLICY_PUBLISHED", snapshot=snapshot)
            self.store.remember(
                scope, body.idempotency_key, body.model_dump_json(), str(uid), snapshot
            )
            return snapshot

    def retire(self, uid, actor, body):
        self.require_schema()
        self.store.resource("policy", uid, actor)
        if body.effective_on <= today() or not body.reason.strip():
            reject("FUTURE_EFFECTIVE_REQUIRED", "请填写未来退休日期及原因")
        scope = actor["id"] + ":travel-retire:" + str(uid)
        with self.store.lock, self.store.db:
            replay = self.store.replay(scope, body.idempotency_key, body.model_dump_json())
            if replay:
                return json.loads(replay[2])
            versions = self.publications(actor["company_id"], uid)
            if len(versions) != body.expected_revision or not versions:
                raise APIError(409, "REVISION_CONFLICT", "标准版本已变化，请刷新")
            existing = [
                e
                for e in self.events(actor["company_id"], "TRAVEL_POLICY_RETIRED")
                if e["upload_id"] == str(uid)
            ]
            if existing:
                reject("TRAVEL_ALREADY_RETIRED", "此标准已记录退休日期")
            result = dict(
                upload_id=str(uid),
                policy_id=versions[-1]["policy_id"],
                effective_on=str(body.effective_on),
                reason=body.reason,
            )
            self.store.event(actor, "TRAVEL_POLICY_RETIRED", **result)
            self.store.remember(
                scope, body.idempotency_key, body.model_dump_json(), str(uid), result
            )
            return result

    def match_standards(self, company, version, context, *, pinned_ids=None):
        """Explain the same immutable inputs used by the deterministic rule check."""
        fields, employee = version["fields"], version["employee_snapshot"]
        details = TravelDetails.model_validate(version["travel_details"])
        incurred, started = fields["incurred_on"], context["started_at"]
        kind = {"HOTEL": "住宿", "TRANSPORT": "交通", "ALLOWANCE": "差旅补助"}[
            details.expense_type
        ]
        city_names = [fields["city"]]
        if employee.get("authority") == "p4a.1" and employee.get("city_fact"):
            city = employee["city_fact"]["value"]
            city_names = [city["name"], *city.get("aliases", [])]
        published, typed, effective, eligible, available = [], [], [], [], []
        inactive_reasons = []
        for snapshot in self.publications(company):
            if fingerprint({k: v for k, v in snapshot.items() if k != "sha256"}) != snapshot[
                "sha256"
            ]:
                reject("TRAVEL_INTEGRITY", "标准快照校验失败，需管理员检查")
            if snapshot["published_at"] > started:
                continue
            published.append(snapshot)
            rules = [r for r in snapshot["rules"] if r["expense_type"] == details.expense_type]
            if not rules:
                continue
            typed.append(snapshot)
            metadata = snapshot["metadata"]
            if not (metadata["effective_from"] <= incurred and (
                not metadata["effective_to"] or incurred < metadata["effective_to"]
            )):
                cause = (f"尚未生效，生效日为{metadata['effective_from']}"
                         if incurred < metadata["effective_from"]
                         else f"已到期，截止日为{metadata['effective_to']}（截止日不含）")
                inactive_reasons.append(f"{metadata['title']}：{cause}")
                continue
            retired = [e for e in self.events(company, "TRAVEL_POLICY_RETIRED")
                       if e["policy_id"] == snapshot["policy_id"] and e["at"] <= started]
            retirement = next((e for e in retired if incurred >= e["effective_on"]), None)
            if retirement:
                inactive_reasons.append(
                    f"{metadata['title']}：已于{retirement['effective_on']}退休"
                )
                continue
            effective.append(snapshot)
            if pinned_ids is not None and snapshot["policy_id"] not in pinned_ids:
                continue
            eligible.extend((snapshot, r) for r in rules)
            for rule in rules:
                if all(scope_matches(rule[key], employee[field]) for key, field in (
                    ("departments", "department"), ("positions", "position"), ("grades", "grade")
                )) and any(scope_matches(rule["cities"], name) for name in city_names) and (
                    details.expense_type != "TRANSPORT"
                    or rule["transport_mode"] == details.transport_mode
                ):
                    available.append((snapshot, rule))
        if available:
            return available, ""
        if not published:
            return [], (f"本次审核开始时，企业尚未正式发布{kind}费标准，无法校验本笔费用；"
                        "请企业管理员核对制度原文并正式发布对应标准（上传或确认制度不等于发布标准）")
        if not typed:
            return [], f"企业已发布的差旅标准中没有{kind}费规则；请企业管理员核对原文并发布对应标准"
        if not effective:
            return [], (f"费用发生日{incurred}没有有效的{kind}费标准；"
                        + "；".join(dict.fromkeys(inactive_reasons))
                        + "。请制度管理员核实适用版本")
        if not eligible:
            return [], ("本次审核固定的制度版本中没有适用标准；"
                        "后续发布的标准不能追溯替换历史审核依据")
        if details.expense_type == "TRANSPORT" and not details.transport_mode:
            return [], "申请缺少交通工具信息；请员工补充交通工具和席别后重新提交"
        mismatches = []
        for snapshot, rule in eligible:
            reasons = []
            for key, value, label in (
                ("cities", city_names[0], "城市"),
                ("departments", employee["department"], "部门"),
                ("positions", employee["position"], "职位"),
                ("grades", employee["grade"], "职级"),
            ):
                matches = (any(scope_matches(rule[key], name) for name in city_names)
                           if key == "cities" else scope_matches(rule[key], value))
                if not matches:
                    reasons.append(f"{label}为{value}，标准适用{ '、'.join(rule[key]) }")
            if (details.expense_type == "TRANSPORT"
                    and rule["transport_mode"] != details.transport_mode):
                reasons.append(f"交通工具为{details.transport_mode}，标准适用{rule['transport_mode']}")
            mismatches.append(
                f"{snapshot['metadata']['title']}／{rule['rule_id']}：" + "；".join(reasons)
            )
        return [], "未匹配到适用标准。" + "。".join(dict.fromkeys(mismatches))

    def unmatched_diagnosis(self, company, version, context):
        """Read-only explanation; never replace or persist a historical audit result."""
        if not version.get("travel_details"):
            return None
        if not context.get("started_at"):
            # Older workflows did not persist travel's audit-start timestamp. Only
            # explain an absent publication set; never guess historical visibility.
            if not self.publications(company):
                kind = {"HOTEL": "住宿", "TRANSPORT": "交通", "ALLOWANCE": "差旅补助"}[
                    version["travel_details"]["expense_type"]
                ]
                return (f"企业尚未正式发布{kind}费标准，无法校验本笔费用；"
                        "请企业管理员核对制度原文并正式发布对应标准"
                        "（上传或确认制度不等于发布标准）")
            return None
        available, reason = self.match_standards(company, version, context)
        return reason if not available else None

    def evaluate(self, state, pinned=False):
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT company_id,version FROM enterprise_claims WHERE claim_id=?",
                (state.claim_id,),
            ).fetchone()
            if not row:
                return None
            raw = self.store.db.execute(
                "SELECT body FROM enterprise_versions WHERE claim_id=? AND version=?",
                (state.claim_id, row[1]),
            ).fetchone()
        version = json.loads(raw[0])
        details = version.get("travel_details")
        # Legacy submissions retain legacy workflow until explicitly using travel details.
        if not details:
            return None
        details = TravelDetails.model_validate(details)
        fields, employee = version["fields"], version["employee_snapshot"]
        available, unmatched_reason = self.match_standards(
            row[0], version, state.audit_context,
            pinned_ids=state.audit_context.get("travel_policy_ids", []) if pinned else None,
        )
        facts = {
            "expense_type": details.expense_type,
            "department": employee["department"],
            "position": employee["position"],
            "grade": employee["grade"],
            **details.model_dump(mode="json"),
        }
        result = dict(
            rule_id="TRAVEL_STANDARD",
            outcome="UNKNOWN",
            severity="REVIEW",
            reason=unmatched_reason,
            actual=fields["amount"],
            limit=None,
            policy_version_id=None,
        )
        evidence, ids = [], []
        if len(available) > 1:
            result["reason"] = "命中多条冲突标准，需管理员处理，不能自动判断合规"
        elif len(available) == 1:
            snapshot, rule = available[0]
            ids = [snapshot["policy_id"]]
            metadata = snapshot["metadata"]
            evidence = [
                PolicyEvidence(
                    policy_version_id=snapshot["policy_id"],
                    policy_key=snapshot["upload_id"],
                    version=metadata["version"],
                    effective_from=metadata["effective_from"],
                    effective_to=metadata["effective_to"],
                    chunk_id=snapshot["policy_id"] + ":" + rule["rule_id"],
                    source_uri="enterprise-policy://" + snapshot["upload_id"],
                    quote=rule["quote"],
                )
            ]
            result["policy_version_id"] = snapshot["policy_id"]
            trip_days = (
                date.fromisoformat(fields["trip_end"]) - date.fromisoformat(fields["trip_start"])
            ).days + 1
            amount, limit = Decimal(fields["amount"]), None
            missing = ""
            if rule["review_required"]:
                missing = "本标准含需人工核实的附加条件，不能自动确认合规"
            elif details.expense_type == "HOTEL":
                if not fields.get("nights") or fields["nights"] > trip_days - 1:
                    missing = "住宿晚数与行程不符，需核实"
                else:
                    limit = Decimal(rule["limit"]) * fields["nights"]
                    facts["calculation"] = f"{rule['limit']}元/晚 × {fields['nights']}晚"
            elif details.expense_type == "TRANSPORT":
                if not details.transport_mode or not details.seat:
                    missing = "缺少交通工具或席别"
                elif details.seat not in rule["seats"]:
                    result.update(
                        outcome="FAIL",
                        severity="BLOCK",
                        reason="交通席别超出此职位、部门及城市适用的标准",
                    )
                elif rule["unit"] == "DAY" and details.transport_count > trip_days:
                    missing = "交通发生天数超出行程，请核实"
                elif rule["limit"]:
                    count = details.transport_count
                    limit = Decimal(rule["limit"]) * count
                    unit_label = "天" if rule["unit"] == "DAY" else "次"
                    facts["calculation"] = f"{rule['limit']}元/{unit_label} × {count}{unit_label}"
                else:
                    result.update(
                        outcome="PASS",
                        severity="BLOCK",
                        reason="交通工具、席别符合适用标准；制度未规定金额上限",
                    )
            else:
                days = details.allowance_days
                if not days or days > trip_days:
                    missing = "缺少补助天数或申报天数超出行程"
                else:
                    limit = full_day_allowance(
                        rule["limit"], days, breakfasts=details.breakfasts,
                        lunches=details.lunches, dinners=details.dinners,
                        breakfast_rate=Decimal(rule["breakfast_rate"]),
                        lunch_rate=Decimal(rule["lunch_rate"]),
                        dinner_rate=Decimal(rule["dinner_rate"]),
                    )
                    units = Decimal(days) - sum(
                        (Decimal(count) * Decimal(rule[rate]) for count, rate in (
                            (details.breakfasts, "breakfast_rate"),
                            (details.lunches, "lunch_rate"), (details.dinners, "dinner_rate"))),
                        Decimal(0),
                    )
                    facts["calculation"] = (
                        f"{rule['limit']}元/天 × ({days}天 − 供餐扣减{Decimal(days) - units}天)"
                    )
            if missing:
                result["reason"] = missing
            elif limit is not None:
                result.update(
                    outcome="PASS" if amount <= limit else "FAIL",
                    severity="BLOCK",
                    limit=f"{limit:.2f}",
                    reason=(
                        f"{employee['department']} · {employee['position']} / "
                        f"{employee['grade']}级 · {fields['city']}："
                        f"{facts['calculation']} = {limit:.2f}元；申报{amount:.2f}元"
                    ),
                )
        if employee.get("authority") == "p4a.1" and employee.get("p4a_blockers"):
            result.update(outcome="UNKNOWN", severity="BLOCK", limit=None,
                          reason="; ".join(employee["p4a_blockers"]))
            facts["trusted_fact_blockers"] = employee["p4a_blockers"]
        facts["standard_outcome"] = result["outcome"]
        return {
            "rules": [RuleResult.model_validate(result)],
            "evidence": evidence,
            "facts": facts,
            "ids": ids,
        }


class TravelCatalog:
    def __init__(self, legacy, travel):
        self.legacy, self.travel = legacy, travel

    def __getattr__(self, name):
        return getattr(self.legacy, name)

    def evaluate_travel(self, state, pinned=False):
        return self.travel.evaluate(state, pinned=pinned)
