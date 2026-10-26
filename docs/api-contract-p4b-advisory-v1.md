# P4B Advisory v1 (explicit opt-in, offline engineering acceptance)

`p4b_advisory_enabled=false` by default. Enabling requires existing enterprise/P4A/P4B configuration. Paid generation still requires the separate `p4b_live_llm` opt-in. This change does not activate any existing service or paid API. New mode selects `AdvisoryCatalog`; old semantic.1/semantic.2 and p4b.1 meanings remain unchanged. No SQL migration, old report rewrite, new expense category or second policy authority.

## Output contract

`p4b.advisory.1` schema is frozen in `p4b-advisory-v1.schema.json`. Risk is LOW/MEDIUM/HIGH/UNDETERMINED. Recommendation is RECOMMEND_APPROVAL/RECOMMEND_REJECTION/REQUEST_EVIDENCE/ESCALATE/UNDETERMINED. `requires_human_review` is literal true. No confidence, approval action, ERP operation, or top-level free text. A recommendation has no authorization effect.

Each short issue has topic, basis, severity, actual policy ref IDs, fact paths, explanation and finance_check. Input uses existing compact source/fact projection, source SHA and effective-date checks, actual immutable positive-evidence provenance and unchanged deterministic rules. Both rules and valid source context must be present before semantic analysis; source-dependent arithmetic remains ordered where required. No model call precedes RAG evidence. Current RAG is source-backed local text; remote BGE is NOT_RUN.

Per-issue citation attribution, clause/basis compatibility, actual fact paths, bounded fact/amount/approval text guards reuse FIX3/P2 helpers. Invalid issues never become trusted facts; valid issues may be displayed as PARTIAL while errors and the original output remain separate. Invalid top-level references/DTO/wire produce FAILED. An approval recommendation with missing deterministic/positive prerequisites fails validation. No invented citations or VERIFIED upgrades. These finite guards do not prove arbitrary natural-language truth.

## Three independent dimensions

Optional enterprise detail field `advisory_analysis` has schema `p4b.advisory.report.1`:

- `deterministic_status`: PASS/FAIL/UNKNOWN from rules, Hard Gates, applicable published policy and required original/verified evidence. Model output cannot modify it.
- `ai_advisory_status`: SUCCEEDED/PARTIAL/FAILED/NOT_RUN. FAILED is always unsuccessful model analysis, even if finance later independently approves. Original output, private bounded raw response and structured errors are retained separately. Credentials echoed in wire responses are rejected and excluded from retention.
- `finance_decision_status`: PENDING/APPROVED/REJECTED/RETURNED/ESCALATED, derived from actual enterprise state and authenticated human review. ERP_SYNC_PENDING means an actual finance approval exists while ERP recovery remains outstanding. ESCALATED means a human recorded a need for exception handling; it grants no payment authority and does not create a new workflow stage.

The legacy compatibility envelope remains UNKNOWN/HUMAN_REVIEW/UNSUPPORTED, with no semantic clearance or compliance certificate. Existing old reports are preserved as-is. Old in-flight runs without a pinned advisory-mode context cannot silently switch protocols; use explicit new reaudit. Histories remain append-only, and reauditing supersedes old execution authority without erasing the old report.

## API changes

Existing claim submission, fact verification, source evidence, reauditing, finance-action APPROVE/REJECT/RETURN and retry routes remain. RETURN is the existing workflow operation used for RETURN_FOR_EVIDENCE. Current enterprise UI shows the new report only if present, preserving old status mapping.

| New route under /api/v1.5 | Role | Behavior |
| --- | --- | --- |
| GET /p4b/v2/claims/{cid} | owner or same-company FINANCE/ADMIN | Current advisory report, report digest, finance state, manual review and freshness |
| POST /p4b/v2/claims/{cid}/manual-review | FINANCE | Independent human review, never an approval or ERP operation |

Manual review requires expected_revision, exact report_digest, outcome CLEAR/RETURN_FOR_EVIDENCE/REJECT/ESCALATE, explicit exception_required bool, unreliable_advice_acknowledged=true, actual source_reference, basis and idempotency key. CLEAR + exception_required=true is rejected. Every record binds actor, current authority generation, company, run, claim version, candidate digest, fact revision, report/input digests and timestamp. Entry and mutation-boundary live session/role/company/generation checks prevent queued stale authorization. Claim owner cannot self-review.

Final APPROVE still requires a separate existing authenticated finance operation. It rechecks all original Hard Gates, PASS rules, applicable original published policy, invoice bytes and integrity, trusted master data, verified fact revision and full positive provenance; plus a fresh CLEAR manual review from the actual approving actor. A new application version, facts, run, source or authorization generation invalidates approval eligibility. It never calls the model again. No AI success/failure/recommendation value is sufficient to approve.

Valid source-bound exception findings or verified exception records remain blocked: this demo has no authorized exception payment channel. Ordinary finance cannot override over-limit rules or missing evidence with a review checkbox or APPROVE. Return/reject remain possible. AI failure can reach independent manual review and eventual approval only when all deterministic requirements and human review are independently satisfied.

P1 account/session/generation and intent fencing at review persistence, recovery and actual Mock ERP commit is retained. Enterprise execution additionally binds a request-local evidence validator before resume. For an uncommitted effect it rechecks original execution evidence both before resume and inside the same SQLite writer transaction as the first MockERP write, including after asynchronous adapter waits. A changed or unavailable invoice/source/fact/manual-review binding raises `409 FINANCE_EVIDENCE_INVALID`, preserves history and enters existing MANUAL_RECOVERY_REQUIRED. It never silently reapproves stale evidence. A matching already committed effect is only reconciled under the original P1 intent and live-requester fences, with no new payment effect. Conflicting effects remain rejected. Existing graph work_lock/service lock and SQLite writer transactions serialize changes; ERP idempotency keys stay claim/run-bound. This is a single-process Demo; no production or real ERP transactional guarantee is claimed.

## UI

Reuse WorkBuddy's existing application details, rule results, original policy clauses, provenance verification form and finance operations. New AI section shows truthful model state, risks, semantic findings, missing materials, errors and explicitly untrusted raw output, plus the independent human review form. Warning: “AI建议仅供参考，最终审批由授权财务人员负责。” Approval availability is an indication; the server performs final authorization and evidence checks.

No real model, remote vector retrieval, existing runtime browser session, production or real ERP acceptance is implied by offline tests/builds/rendering. Any later single paid advisory test requires a separate explicit authorization, closed old ledgers must not be reopened, and raw historical Git ancestry must not be pushed.
