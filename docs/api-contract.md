# API Contract — v1.0.0 (M0 冻结)

本文件是后端与 WorkBuddy 前端的 Source of Truth。所有路由没有 /api 前缀。

> 阶段说明：本文保留 M0 初始行为记录；其中“只读 / 写入返回 501”不描述当前 M1/M2 及以后的实现。当前基础 Mock 支持审核、人工复核和恢复处理。2026-10-27 项目改名仅更新 OpenAPI 的展示标题与说明；下列路径、DTO、状态码声明和枚举未改变。
本地 Base URL: http://localhost:8000。Swagger: /docs；机器协议: /openapi.json。
新增或修改字段、状态码、枚举：**先变更本文件，随后变更 Schema、测试和代码**。

## 1. 通用约定

- JSON UTF-8；字段 snake_case。ID 是 opaque string；run_id / event_id 使用 UUID。
- 时间戳是带时区的 ISO 8601 UTC；业务日期 YYYY-MM-DD，Asia/Shanghai。
- 金额固定 CNY，Decimal 精度 2 位，**JSON 字符串**（如 "800.00"），不使用 float。
- 枚举为大写字符串。未知字段返回 422。nullable 字段明确输出 null，不省略。
- 每个响应有 X-Request-ID（服务器生成 UUID），错误体中的 request_id 与该头一致。
- M0 是无认证、只读 Mock Demo；只绑定 loopback。生产 RBAC / tenant 由后续认证上下文提供，
  不能信任 body 中的 reviewer_id 作为实际身份。此版本没有生产上线宣称。
- 所有 GET 200；未找到 claim 为 404；输入验证为 422；M0 写路由为 501。
- M0 无分页包装 detail；list 使用 {items, total, limit, offset, data_source}。
- /health 是 liveness，不检查数据库或外部服务；database 字段 not_checked 不能视为 ready。
- 查询数据仅来自固定 Mock fixture；没有写入数据库，不会产生虚构审核决策或审计事件。

## 2. 路由与阶段状态

| 方法 / 路径 | 请求 | 响应 | M0 状态 |
|---|---|---|---|
| GET /health | 无 | HealthResponse | 已实现 |
| GET /claims | limit=20 (1..100), offset=0 (>=0), status? | ClaimList | 已实现 Mock |
| GET /claims/{claim_id} | opaque claim_id | ClaimDetail | 已实现 Mock |
| POST /claims/{claim_id}/audit | AuditRequest | OperationResponse；未来 202 | M0 501 |
| POST /claims/{claim_id}/human-review | HumanReviewRequest | OperationResponse；未来 200 | M0 501 |
| POST /claims/{claim_id}/resume | ResumeRequest | OperationResponse；未来 202 | M0 501 |
| GET /claims/{claim_id}/audit-log | limit / offset 同上 | AuditLogResponse | 已实现，空列表 |
| GET /dashboard/metrics | 无 | DashboardMetrics | 已实现 Mock |

写路由顺序：validate input → lookup claim → 501 FEATURE_NOT_IMPLEMENTED。
不存在的 claim 返回 404；无效请求体返回 422；不接受写入，不改变状态。
**WorkBuddy 应显示未开放状态，而不是将 501 当作审核结果。**

## 3. 核心对象

ClaimSummary:
- claim_id: string；employee: Employee；expense: Expense
- status: NEW | AUDITING | PENDING_HUMAN | PENDING_ERP | COMPLETED | FAILED
- created_at: datetime；updated_at: datetime
- decision_result: DecisionResult | null（M0 全部为 null）

ClaimDetail = ClaimSummary + documents: Document[] + erp_status: NOT_STARTED |
PENDING | SUCCEEDED | FAILED + active_run_id: UUID | null + data_source: "mock"。

Employee = {employee_id: string, name: string, department: string, legal_entity: string}。
Expense = {category: HOTEL | TRANSPORT | MEAL | OTHER, city: string,
amount: decimal-string, currency: "CNY", incurred_on: date, nights: integer >= 1}。
nights 仅用于住宿；非住宿保持 1，不用它推断日限额。
Document = {document_id: string, file_name: string, content_type: string,
sha256: string(64 hex), parse_status: UNPARSED | PARSED | FAILED}。
M0 没有上传 URL / OCR 结果 / 实际文件，Mock sha256 只是结构占位。

HealthResponse 固定：
```json
{"status":"ok","service":"fde-expense-ai-auditor","version":"0.1.0","milestone":"M0","data_source":"mock","database":"not_checked"}
```

ClaimList 示例：
```json
{"items":[{"claim_id":"CLM-2026-001","employee":{"employee_id":"EMP-001","name":"演示员工甲","department":"交付","legal_entity":"DEMO-CN"},"expense":{"category":"HOTEL","city":"深圳","amount":"800.00","currency":"CNY","incurred_on":"2026-10-01","nights":1},"status":"NEW","created_at":"2026-10-02T02:00:00Z","updated_at":"2026-10-02T02:00:00Z","decision_result":null}],"total":3,"limit":1,"offset":0,"data_source":"mock"}
```

## 4. 最终统一 DecisionResult（M0 已定义但不产生）

```json
{
  "schema_version":"1.0.0",
  "claim_id":"CLM-2026-001",
  "run_id":"00000000-0000-0000-0000-000000000001",
  "decision":"REJECT",
  "risk_level":"HIGH",
  "hard_gate_passed":true,
  "rule_results":[{"rule_id":"HOTEL_CITY_LIMIT","outcome":"FAIL","severity":"BLOCK","reason":"深圳住宿 800.00 超过制度限额 700.00","actual":"800.00","limit":"700.00","policy_version_id":"POL-SZ-2026"}],
  "policy_evidence":[{"policy_version_id":"POL-SZ-2026","policy_key":"CN-TRAVEL-HOTEL","version":"2026","effective_from":"2026-01-01","effective_to":"2027-01-01","chunk_id":"SZ-HOTEL-1","source_uri":"mock://policies/POL-SZ-2026","quote":"深圳住宿每晚限额 700 元","relevance_score":0.95}],
  "reason":"确定性规则不通过",
  "confidence":1.0,
  "requires_human":false,
  "decided_at":"2026-10-07T02:00:00Z"
}
```

- decision: APPROVE | REJECT | HUMAN_REVIEW；risk_level: LOW | MEDIUM | HIGH。
- confidence: 0..1，表示证据完整度，不是审批权，也不是授权概率。
- rule_results: RuleResult[]；outcome PASS | FAIL | UNKNOWN；
  severity INFO | REVIEW | BLOCK；actual / limit: decimal-string | null；
  policy_version_id: string | null。
- policy_evidence: PolicyEvidence[]；relevance_score nullable 0..1，避免将未归一化
  reranker logit 当作概率；有效期为 [effective_from, effective_to)，结束日期可 null。
- HUMAN_REVIEW 当且仅当 requires_human=true；HUMAN_REVIEW 的 decided_at=null，
  最终 APPROVE/REJECT 的 decided_at 必须带时区。
- hard_gate_passed=false 只允许 HIGH/REJECT；BLOCK/FAIL 只允许 REJECT。
  APPROVE 只允许 LOW、所有硬门通过、没有 REVIEW/FAIL 或 UNKNOWN，
  且至少一条有效制度证据。MEDIUM 允许 HUMAN_REVIEW 或人工 REJECT。
- HIGH 固定 REJECT；人工批准 MEDIUM 后重新检查确定性约束，满足后降至 LOW/APPROVE。
  人工决定不能绕过硬门或 BLOCK 规则。
- 人工审批与 ERP 同步是两个不同状态，decision_result 的 APPROVE 不等于 ERP SUCCEEDED。

## 5. 写操作契约（为 M1 冻结，M0 不实现生命周期）

AuditRequest:
```json
{"idempotency_key":"demo-audit-001","mode":"LIVE"}
```
mode: LIVE | SHADOW，默认 LIVE；idempotency_key: string(1..128)，必填。

HumanReviewRequest:
```json
{"run_id":"00000000-0000-0000-0000-000000000001","expected_revision":1,"decision":"APPROVE","reviewer_id":"FIN-001","comment":"核实后批准","idempotency_key":"demo-review-001"}
```
decision 仅 APPROVE | REJECT；expected_revision >= 0；comment 1..2000。
reviewer_id 是 Demo 展示字段，生产身份必须从认证上下文解析。

ResumeRequest:
```json
{"run_id":"00000000-0000-0000-0000-000000000001","expected_revision":2,"idempotency_key":"demo-resume-001"}
```
恢复原因和节点由服务器 checkpoint 决定；body 不能注入 state 或跳过节点。

OperationResponse（未来成功体）:
{claim_id: string, run_id: UUID, status: ClaimStatus, revision: integer >= 0,
decision_result: DecisionResult | null}。
audit / resume 返回 202 仅表示任务已持久化；前端轮询 GET detail。
human-review 返回 200 仅表示人工意见已持久化，不隐式 resume。

未来语义：相同 claim+operation+idempotency_key+payload 重放原响应；同 key 不同
payload 返回 409 IDEMPOTENCY_CONFLICT；错误 run/revision/status 返回
409 INVALID_TRANSITION 或 REVISION_CONFLICT；不存在的 run 返回 404 RUN_NOT_FOUND。
SHADOW 使用独立 run，禁止 ERP 写入，不覆盖 LIVE claim 的状态和最终结果。

## 6. 审计与看板

AuditLogResponse = {claim_id: string, items: AuditEvent[], total: integer,
limit: integer, offset: integer, data_source: "mock"}。
AuditEvent = {event_id: UUID, claim_id: string, run_id: UUID,
event_type: string, actor: string, occurred_at: datetime,
payload: object}。后续 append-only；禁止写原始发票、密钥或全量个人资料。

DashboardMetrics:
{total_claims: integer, by_status: object(所有 ClaimStatus 都存在且为 integer),
by_decision: object(APPROVE/REJECT/HUMAN_REVIEW 全部存在),
pending_human: integer, pending_erp: integer, data_source: "mock"}。
M0 total_claims=3，NEW=3，其他全为 0。

## 7. 错误体

```json
{"error":{"code":"FEATURE_NOT_IMPLEMENTED","message":"M0 does not execute audit operations","details":{}},"request_id":"<server-generated-uuid>"}
```
ErrorResponse = {error: {code: string, message: string, details: object}, request_id: UUID}。
422 code=VALIDATION_ERROR，details.issues 为验证错误列表（不回显输入）。
404 code=CLAIM_NOT_FOUND；501 code=FEATURE_NOT_IMPLEMENTED。
M0 同时统一未知路径 404 HTTP_ERROR 和方法错误 405 HTTP_ERROR。
客户端不能用 message 做程序分支，应使用 HTTP status + error.code。

