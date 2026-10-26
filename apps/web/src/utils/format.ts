import type { ErpStatus, ExpenseCategory, ParseStatus, PolicyEvidence, Severity } from '../lib/api/types';
import type { ClaimStatus, Decision, RiskLevel, RuleOutcome } from '../lib/api/enums';

/** Contract：金额是 decimal string（如 "800.00"），前端只做展示格式化 */
export function fmtMoney(amount: string | number): string {
  const s = typeof amount === 'number' ? amount.toFixed(2) : amount;
  return `¥${s}`;
}

export function fmtNumber(n: number): string {
  return n.toLocaleString('zh-CN');
}

export function fmtPercent(v: number, digits = 1): string {
  return `${(v * 100).toFixed(digits)}%`;
}

/**
 * 耗时格式化。
 * 后端 duration_ms 是浮点数，直接拼接会把 IEEE-754 尾数带上屏
 * （例如累计耗时曾显示为 219.85999999999996 ms），因此统一收敛精度。
 */
export function fmtDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '—';
  if (ms < 1) return `${ms.toFixed(3)} ms`;
  if (ms < 1000) return `${Number(ms.toFixed(2))} ms`;
  return `${Number((ms / 1000).toFixed(2))} s`;
}

/** Contract：时间戳为 UTC ISO 8601；展示时转为本地可读格式 */
export function fmtDateTime(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (x: number) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function fmtDate(iso: string | null): string {
  return iso ? iso.slice(0, 10) : '—';
}

/* ---------------- 枚举中文映射（严格对应 Contract 枚举） ---------------- */

export const CLAIM_STATUS_LABEL: Record<ClaimStatus, string> = {
  NEW: '新建',
  AUDITING: 'AI 审核中',
  PENDING_HUMAN: '待人工复核',
  PENDING_ERP: '等待 ERP 恢复',
  COMPLETED: '已完结',
  FAILED: '失败',
};

export const DECISION_LABEL: Record<Decision, string> = {
  APPROVE: '通过',
  REJECT: '拒绝',
  HUMAN_REVIEW: '人工复核',
};

export const RISK_LABEL: Record<RiskLevel, string> = {
  LOW: '低风险',
  MEDIUM: '中风险',
  HIGH: '高风险',
};

export const OUTCOME_LABEL: Record<RuleOutcome, string> = {
  PASS: '通过',
  FAIL: '未通过',
  UNKNOWN: '不确定',
};

export const SEVERITY_LABEL: Record<Severity, string> = {
  INFO: '提示',
  REVIEW: '需复核',
  BLOCK: '阻断',
};

export const ERP_STATUS_LABEL: Record<ErpStatus, string> = {
  NOT_STARTED: '未开始',
  PENDING: '进行中',
  SUCCEEDED: '已回写',
  FAILED: '失败',
};

export const PARSE_STATUS_LABEL: Record<ParseStatus, string> = {
  UNPARSED: '未解析',
  PARSED: '已解析',
  FAILED: '解析失败',
};

export const CATEGORY_LABEL: Record<ExpenseCategory, string> = {
  HOTEL: '酒店住宿',
  TRANSPORT: '交通',
  MEAL: '餐饮',
  OTHER: '其他',
};

/** 制度编码中文名（policy_key）—— 仅展示用，不影响后端判定 */
export const POLICY_KEY_LABEL: Record<string, string> = {
  'CN-TRAVEL-HOTEL': '差旅制度 · 住宿',
  'CN-TRAVEL-AIR': '差旅制度 · 机票',
  'CN-ENTERTAIN': '业务招待制度',
  'CN-MEAL': '差旅制度 · 餐饮',
};

/** 常用英文字段名中文展示（界面优先中文，字段名以灰字保留便于联调） */
export const FIELD_LABEL: Record<string, string> = {
  employee_id: '员工编号',
  legal_entity: '法人主体',
  category: '费用类型',
  city: '城市',
  amount: '金额',
  incurred_on: '费用发生日',
  nights: '住宿夜数',
  status: '单据状态',
  erp_status: 'ERP 同步状态',
  created_at: '创建时间',
  updated_at: '更新时间',
  run_id: '运行编号',
  expected_revision: '预期版本号',
  reviewer_id: '复核人',
  comment: '复核意见',
  confidence: '置信度',
  risk_level: '风险等级',
  requires_human: '需人工介入',
  decided_at: '决策时间',
  actual: '实际值',
  limit: '限额',
  chunk_id: '条款切片',
  relevance_score: '相关度',
  source_uri: '来源地址',
  policy_version_id: '制度版本编号',
  schema_version: '结构版本',
  data_source: '数据来源',
  actor: '操作者',
  outcome: '结果',
  severity: '严重级别',
  reason: '判定说明',
};

export function fieldLabel(key: string): string {
  return FIELD_LABEL[key] ?? key;
}

/**
 * 审计事件类型中文映射（展示口径）。
 * 真实后端事件流以后端 event_type 为准；未命中映射时直接显示原始 event_type。
 * 下面同时覆盖：真实后端 M2 事件 + DEMO MOCK 事件。
 */
export const EVENT_TYPE_LABEL: Record<string, string> = {
  /* ---- 真实后端 M2 事件 ---- */
  CLAIM_LOADED: '报销单加载',
  ERP_CONTEXT_FETCHED: 'ERP 上下文获取',
  DOCUMENTS_PARSED: '单据解析',
  HARD_GATE_CHECKED: '硬门校验',
  RULES_CHECKED: '规则检查',
  POLICY_RETRIEVED: '制度条款检索',
  LLM_ANALYSIS_COMPLETED: 'LLM 分析完成',
  RISK_ASSESSED: '风险评估',
  DECISION_MADE: '决策生成',
  HUMAN_REVIEW_REQUIRED: '需要人工复核',
  WORKFLOW_INTERRUPTED: '流程中断（等待人工）',
  HUMAN_REVIEW_SUBMITTED: '人工复核已提交',
  WORKFLOW_RESUMED: '流程已恢复',
  HUMAN_APPROVED: '人工批准',
  HUMAN_REJECTED: '人工拒绝',
  ERP_UPDATED: 'ERP 已回写',
  WORKFLOW_COMPLETED: '流程完成',
  AUTO_APPROVED: '自动批准',

  /* ---- 真实后端 M3 故障恢复事件（名称均来自 Backend，前端不创造事件） ---- */
  DEPENDENCY_CALL_ATTEMPTED: '外部系统调用',
  DEPENDENCY_CALL_FAILED: '外部系统调用失败',
  DEPENDENCY_RECOVERED: '外部系统已恢复',
  DEPENDENCY_NON_RETRYABLE: '外部系统不可重试错误',
  RETRY_SCHEDULED: '已安排重试',
  RETRY_ATTEMPTED: '正在重试',
  RETRY_EXHAUSTED: '重试次数已耗尽',
  RECOVERY_REQUIRED: '等待系统恢复',
  WORKFLOW_RECOVERY_RESUMED: '恢复工作流',
  IDEMPOTENT_REPLAY: '已复用原执行结果',
  ERP_WRITE_SKIPPED: 'ERP 写入跳过',
  WRITE_ERP_FAILED: 'ERP 写入失败',
  /** 制度条款检索节点失败（后端在 pgvector 检索失败时写入该事件） */
  RETRIEVE_POLICY_FAILED: '制度条款检索失败',

  /* ---- DEMO MOCK 事件 ---- */
  CLAIM_CREATED: '报销单创建',
  AUDIT_STARTED: '审核任务启动',
  ERP_CONTEXT_LOADED: 'ERP 上下文加载',
  RULE_EVALUATED: '规则引擎检查',
  LLM_ANALYSIS: 'LLM 综合分析',
  DECISION_PRODUCED: 'AI 决策生成',
  HUMAN_REVIEW_REQUESTED: '转人工复核',
  HUMAN_REVIEW_RECORDED: '人工复核记录',
  MORE_INFO_REQUESTED: '要求补充材料',
  ERP_RETRY_SCHEDULED: 'ERP 重试调度',
  ERP_WRITE_SUCCEEDED: 'ERP 回写成功',
  ERP_WRITE_FAILED: 'ERP 回写失败',
};

/** 审计事件 payload.status 中文映射（真实后端使用 PASS / FAIL 等） */
export const EVENT_STATUS_LABEL: Record<string, string> = {
  PASS: '通过',
  FAIL: '未通过',
  SUCCESS: '成功',
  FAILED: '失败',
  RETRYING: '重试中',
  SKIPPED: '跳过',
  IN_PROGRESS: '进行中',
  PENDING: '等待中',
  INFO: '提示',
  APPROVE: '批准',
  REJECT: '拒绝',
  HUMAN_REVIEW: '待人工复核',
  COMPLETED: '已完成',
};

export const EVENT_STATUS_CLASS: Record<string, string> = {
  PASS: 'badge-tag-green',
  SUCCESS: 'badge-tag-green',
  APPROVE: 'badge-tag-green',
  COMPLETED: 'badge-tag-green',
  FAILED: 'badge-tag-red',
  FAIL: 'badge-tag-red',
  REJECT: 'badge-tag-red',
  RETRYING: 'badge-tag-orange',
  SKIPPED: 'badge-tag-gray',
  IN_PROGRESS: 'badge-tag-blue',
  PENDING: 'badge-tag-blue',
  INFO: 'badge-tag-gray',
  HUMAN_REVIEW: 'badge-tag-orange',
};

/**
 * M3 故障恢复事件的人话说明（展示口径）。
 *
 * ⚠️ 仅把后端已返回的事件翻译成业务语言：
 *    - 不推断重试策略、不计算退避时间、不判定故障类型（全部由 Backend 决定）
 *    - 只使用真实 payload 中已存在的字段
 * 返回 null 表示无需额外说明。
 */
export function eventNote(eventType: string, payload: Record<string, unknown>): string | null {
  const attempt = payload?.attempt;
  const delay = typeof payload?.delay_seconds === 'number' ? payload.delay_seconds : null;

  switch (eventType) {
    case 'DEPENDENCY_CALL_ATTEMPTED':
      return attempt != null ? `第 ${String(attempt)} 次调用外部系统` : '调用外部系统';
    case 'DEPENDENCY_CALL_FAILED':
      return '外部系统本次调用未成功';
    case 'RETRY_SCHEDULED':
      return delay != null
        ? `已安排重试（后端设定等待 ${delay} 秒）`
        : '已安排重试';
    case 'RETRY_ATTEMPTED':
      return attempt != null ? `正在进行第 ${String(attempt)} 次尝试` : '正在重试';
    case 'RETRY_EXHAUSTED':
      return '重试次数已用尽，停止自动重试';
    case 'DEPENDENCY_RECOVERED':
      return '外部系统已恢复正常';
    case 'IDEMPOTENT_REPLAY':
      return '检测到重复操作（相同幂等键），已复用原执行结果，未重复写入 ERP';
    case 'RECOVERY_REQUIRED':
      return '业务审核已完成，但外部系统暂时不可用，流程挂起等待恢复';
    case 'WORKFLOW_RECOVERY_RESUMED':
      return '已从故障节点继续，不重新执行上游审核';
    case 'ERP_WRITE_SKIPPED':
      return '本次未写入 ERP';
    case 'WRITE_ERP_FAILED':
      return 'ERP 写入节点失败，自动批准已禁用';
    default:
      return null;
  }
}

/**
 * 审计事件 payload.error 的展示口径。
 *
 * 真实后端在节点失败时写入结构化错误对象（node / code / message / retryable），
 * 直接 String() 会渲染成 "[object Object]"。这里只做“翻译”，不加判断。
 */
export function formatAuditError(error: unknown): string | null {
  if (error == null) return null;
  if (typeof error === 'string') return error.trim() || null;
  if (typeof error === 'object') {
    const e = error as Record<string, unknown>;
    const parts: string[] = [];
    if (e.code) parts.push(String(e.code));
    if (e.message) parts.push(String(e.message));
    if (e.node) parts.push(`节点 ${String(e.node)}`);
    if (e.retryable === false) parts.push('不可重试');
    if (parts.length > 0) return parts.join(' · ');
    try {
      return JSON.stringify(error);
    } catch {
      return '（无法解析的错误对象）';
    }
  }
  return String(error);
}

/**
 * 展示辅助：判断费用发生日是否落在制度版本有效期内（半开区间 [from, to)）。
 * ⚠️ 仅用于界面标注“当前生效 / 已失效”，不构成任何决策逻辑；
 *    真正的制度适用性判定由后端 PolicyRetriever / Rule Engine 完成。
 */
export function isWithinWindow(p: PolicyEvidence, incurredOn: string): boolean {
  const from = p.effective_from;
  const to = p.effective_to;
  if (!from || !incurredOn) return false;
  if (incurredOn < from) return false;
  if (to && incurredOn >= to) return false;
  return true;
}
