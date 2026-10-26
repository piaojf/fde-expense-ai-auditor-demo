/**
 * API 类型定义 —— 严格对齐 docs/api-contract.md v1.0.0（M0 冻结）
 *
 * 规则（Contract §1）：
 *  - 路由无 /api 前缀；字段 snake_case；ID 为 opaque string
 *  - 金额固定 CNY，Decimal 精度 2 位，**JSON 字符串**（"800.00"），不使用 float
 *  - 时间戳为带时区 ISO 8601 UTC；业务日期 YYYY-MM-DD（Asia/Shanghai）
 *  - 枚举为大写字符串；nullable 字段输出 null，不省略
 *
 * ⚠️ 前端不得自行推导决策（LOW→APPROVE / MEDIUM→HUMAN_REVIEW / HIGH→REJECT）。
 *    决策全部来自后端 DecisionResult（后端 Decision Engine 负责）。
 */
import type { ClaimStatus, Decision, RiskLevel, RuleOutcome, Severity } from './enums';

export type { ClaimStatus, Decision, RiskLevel, RuleOutcome, Severity };

export type ExpenseCategory = 'HOTEL' | 'TRANSPORT' | 'MEAL' | 'OTHER';
export type ErpStatus = 'NOT_STARTED' | 'PENDING' | 'SUCCEEDED' | 'FAILED';
export type ParseStatus = 'UNPARSED' | 'PARSED' | 'FAILED';
export type AuditMode = 'LIVE' | 'SHADOW';

/* ---------------- 核心对象 ---------------- */

export interface Employee {
  employee_id: string;
  name: string;
  department: string;
  legal_entity: string;
}

export interface Expense {
  category: ExpenseCategory;
  city: string;
  amount: string; // decimal string，如 "800.00"
  currency: 'CNY';
  incurred_on: string; // YYYY-MM-DD
  nights: number; // >= 1，仅住宿使用，非住宿恒为 1
}

export interface Document {
  document_id: string;
  file_name: string;
  content_type: string;
  sha256: string; // 64 hex；M0 为结构占位
  parse_status: ParseStatus;
}

export interface RuleResult {
  rule_id: string;
  outcome: RuleOutcome;
  severity: Severity;
  reason: string;
  actual: string | null; // decimal string | null
  limit: string | null; // decimal string | null
  policy_version_id: string | null;
}

export interface PolicyEvidence {
  policy_version_id: string;
  policy_key: string;
  version: string;
  effective_from: string;
  effective_to: string | null; // 半开区间 [from, to)
  chunk_id: string;
  source_uri: string;
  quote: string;
  relevance_score: number | null;
}

export interface DecisionResult {
  schema_version: string;
  claim_id: string;
  run_id: string;
  decision: Decision;
  risk_level: RiskLevel;
  hard_gate_passed: boolean;
  rule_results: RuleResult[];
  policy_evidence: PolicyEvidence[];
  reason: string;
  confidence: number; // 0..1，表示证据完整度，不是审批权
  requires_human: boolean;
  decided_at: string | null; // HUMAN_REVIEW 时为 null
  manual_approval?: { decision: 'APPROVE'; reviewer_id: string; comment: string; reviewed_at: string };
}

export interface ClaimSummary {
  claim_id: string;
  employee: Employee;
  expense: Expense;
  status: ClaimStatus;
  created_at: string;
  updated_at: string;
  decision_result: DecisionResult | null;
}

export interface ClaimDetail extends ClaimSummary {
  documents: Document[];
  erp_status: ErpStatus;
  active_run_id: string | null;
  data_source: string;
}

export interface ClaimList {
  items: ClaimSummary[];
  total: number;
  limit: number;
  offset: number;
  data_source: string;
}

/* ---------------- 审计与看板 ---------------- */

export interface AuditEvent {
  event_id: string;
  claim_id: string;
  run_id: string;
  event_type: string;
  actor: string;
  occurred_at: string;
  payload: Record<string, unknown>;
}

export interface AuditLogResponse {
  claim_id: string;
  items: AuditEvent[];
  total: number;
  limit: number;
  offset: number;
  data_source: string;
}

export interface DashboardMetrics {
  total_claims: number;
  by_status: Record<ClaimStatus, number>;
  by_decision: Record<Decision, number>;
  pending_human: number;
  pending_erp: number;
  data_source: string;
}

export interface HealthResponse {
  status: string;
  service: string;
  version: string;
  milestone: string;
  data_source: string;
  database: string;
}

/**
 * Contract 未定义的演示指标（DEMO MOCK ONLY）。
 * 仅 Mock 模式展示，真实模式不显示。见 docs/ui/API_CHANGE_REQUESTS.md。
 */
export interface DemoOnlyMetrics {
  automation_rate: number;
  human_review_rate: number;
  false_approval_rate: number;
  avg_processing_ms: number;
}

/* ---------------- 请求体 ---------------- */

export interface AuditRequest {
  idempotency_key: string; // 1..128，必填
  mode?: AuditMode; // 默认 LIVE
}

export interface HumanReviewRequest {
  run_id: string;
  expected_revision: number;
  decision: 'APPROVE' | 'REJECT'; // Contract 仅支持这两种
  reviewer_id: string;
  comment: string; // 1..2000
  idempotency_key: string;
}

export interface ResumeRequest {
  run_id: string;
  expected_revision: number;
  idempotency_key: string;
}

export interface OperationResponse {
  claim_id: string;
  run_id: string;
  status: ClaimStatus;
  revision: number;
  decision_result: DecisionResult | null;
}

/* ---------------- 错误体 ---------------- */

export interface ErrorResponse {
  error: {
    code: string;
    message: string;
    details: Record<string, unknown>;
  };
  request_id: string;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public requestId: string = '',
    public details: Record<string, unknown> = {}
  ) {
    super(message);
    this.name = 'ApiError';
  }

  /** M0 写路由统一返回 501：应展示“未开放”，不得当作审核结果 */
  get isNotImplemented(): boolean {
    return this.status === 501 && this.code === 'FEATURE_NOT_IMPLEMENTED';
  }
}
