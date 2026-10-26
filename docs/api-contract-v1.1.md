# Claim Submission API — v1.1.0 (UI-P1)

独立子应用 `/api/v1.1`，OpenAPI `/api/v1.1/openapi.json`。主应用 `/openapi.json`、`docs/openapi.json`、`docs/api-contract.md` 与 v1.0.0 DTO 原样保留。

## POST /api/v1.1/claims — 202

必填：`idempotency_key` (1..128), `applicant_name` (1..200), `department` (1..128), `position` (1..128), `category` (HOTEL/TRANSPORT/MEAL/OTHER), `amount` (正数 Decimal 字符串，最多两位小数), `incurred_on` (YYYY-MM-DD，不晚于上海业务日), `purpose` (1..2000), `city` (标准中文城市枚举), `trip_start`, `trip_end` (合法日期，结束不早于开始，最多366天), `seller` (1..200)。`notes` (0..2000，默认空)。HOTEL 必须 `nights` (严格整数1..366)；其他类型必须 null/省略。不接受审批结果、法人、运行模式等额外字段。文字 strip 后不得空白。住宿晚数不等于出差天数。

支持城市：深圳、广州、北京、上海、杭州、南京、苏州、成都、重庆、武汉、西安、天津、长沙、厦门、福州、郑州、济南、青岛、合肥、宁波、东莞、佛山、珠海、昆明、南宁、贵阳、海口、沈阳、大连、长春、哈尔滨、石家庄、太原、南昌、南通、无锡、温州、兰州、银川、西宁、乌鲁木齐、拉萨、呼和浩特。无适用政策沿用 UNKNOWN/人工处理，不推断额度。

服务器生成 UUID 单号和员工标识，固定 Mock ERP 法人 DEMO-CN。事务保存申请、claims、创建事件、全局创建幂等键后启动原 LIVE 审核（LIVE 为业务工作流模式，不等于付费 LLM）。持久化源仍是现有 SQLite repository；PostgreSQL/pgvector 用于配置选择的 RAG authority。不声称 PostgreSQL claims 已写入。

响应：`{schema_version:"1.1.0", claim:ClaimDetail, submission:SubmissionFields, trip_days:integer, operation:OperationResponse|null, human_review_submitted:boolean, llm_analysis:LLMAnalysis|null, erp_effects:object[]}`。ClaimDetail/OperationResponse 使用原DTO，业务状态来自持久化记录。同键同规范化 payload 返回同单当前状态，不重复创建/ERP；同键不同 payload 409 IDEMPOTENCY_CONFLICT。并发与服务重启保留幂等键。审核异常且状态已 AUDITING 时仅返回真实状态，不重新启动图；NEW 状态可同键重试启动。

## GET /api/v1.1/claims/{claim_id} — 200

返回同上动态申请信息及当前操作 revision/LLM analysis/ERP effect；预置单号或不存在的动态单号返回404。主应用原GET/人工审核/resume/audit-log亦支持动态单号。

错误沿用 ErrorResponse：422 VALIDATION_ERROR；409 IDEMPOTENCY_CONFLICT；404 CLAIM_NOT_FOUND。输入不回显。

## 声明与安全边界

`structured-submission.json` 是服务器保存的用户声明，sha256 校验该声明快照，绝不是已验证发票。原 REQUIRED_FIELDS/DOCUMENTS_VALID 不删减，新增申报路径校验声明匹配而非伪造发票。无 OCR、附件上传、发票真实性、外部员工验证。特殊说明或费用发生日/住宿期间超出行程保守进入业务歧义；LLM 输入低信任文字，无审批权。Hard Block 不可人工覆盖。非住宿规则当前 UNKNOWN，可能无法批准恢复。

默认 Mock LLM / in-memory RAG；仅显式 `LLM_PROVIDER=deepseek` 选用真实收费模型；已有 `POLICY_RETRIEVER=pgvector` 和 BGE-M3 可直接复用，不隐式启用网络服务。
