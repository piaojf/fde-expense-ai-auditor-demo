/**
 * DEMO MOCK 数据 —— 5 个面试 Demo Case
 *
 * 结构严格对齐 docs/api-contract.md v1.0.0（ClaimDetail / DecisionResult / AuditEvent）。
 * ⚠️ 这些是**演示用假数据**，不是后端决策结果：
 *    - 决策字段（decision / risk_level / rule_results / policy_evidence）由本文件预置，
 *      仅用于演示 UI 呈现真实 DecisionResult 时的样子；
 *    - 前端页面不包含任何“LOW→APPROVE / MEDIUM→HUMAN_REVIEW / HIGH→REJECT”推导逻辑；
 *    - 接入真实后端（USE_MOCK_API=false）后，全部数据来自后端 Decision Engine。
 */
import type { AuditEvent, ClaimDetail, DashboardMetrics, DemoOnlyMetrics } from '../api/types';

const sha = '0'.repeat(64);

/* ---------------- 制度证据（PolicyEvidence） ---------------- */

const POL_SZ_2026 = {
  policy_version_id: 'POL-SZ-2026',
  policy_key: 'CN-TRAVEL-HOTEL',
  version: '2026',
  effective_from: '2026-01-01',
  effective_to: '2027-01-01',
  chunk_id: 'SZ-HOTEL-1',
  source_uri: 'mock://policies/POL-SZ-2026',
  quote: '深圳住宿每晚限额 700 元；超出限额部分原则上不予报销。',
  relevance_score: 0.95,
};

const POL_SZ_2025 = {
  policy_version_id: 'POL-SZ-2025',
  policy_key: 'CN-TRAVEL-HOTEL',
  version: '2025',
  effective_from: '2025-01-01',
  effective_to: '2026-01-01',
  chunk_id: 'SZ-HOTEL-1',
  source_uri: 'mock://policies/POL-SZ-2025',
  quote: '深圳住宿每晚限额 900 元；超出限额部分原则上不予报销。',
  relevance_score: 0.71,
};

const POL_ENT = {
  policy_version_id: 'POL-ENT-2024',
  policy_key: 'CN-ENTERTAIN',
  version: '2024',
  effective_from: '2024-06-01',
  effective_to: null,
  chunk_id: 'ENT-1',
  source_uri: 'mock://policies/POL-ENT-2024',
  quote:
    '业务招待须事前审批；跨部门协作场景的招待限额本细则未作规定，由复核人结合事前审批记录裁量。',
  relevance_score: 0.58,
};

const POL_AIR_2026 = {
  policy_version_id: 'POL-AIR-2026',
  policy_key: 'CN-TRAVEL-AIR',
  version: '2026',
  effective_from: '2026-01-01',
  effective_to: '2027-01-01',
  chunk_id: 'SH-AIR-1',
  source_uri: 'mock://policies/POL-AIR-2026',
  quote: '一类城市间往返机票单程不超过 2000 元的，凭行程单据实报销。',
  relevance_score: 0.88,
};

/* ---------------- 5 个 Demo Case ---------------- */

export const DEMO_CLAIMS: ClaimDetail[] = [
  /* Case 1：深圳酒店 600 元 / 标准 700 → APPROVE（预置演示数据） */
  {
    claim_id: 'CLM-2026-1024',
    employee: { employee_id: 'EMP-0087', name: '张伟', department: '销售部', legal_entity: 'DEMO-CN' },
    expense: { category: 'HOTEL', city: '深圳', amount: '600.00', currency: 'CNY', incurred_on: '2026-09-28', nights: 1 },
    status: 'COMPLETED',
    created_at: '2026-10-07T01:12:33Z',
    updated_at: '2026-10-07T01:12:43Z',
    documents: [
      { document_id: 'DOC-1024-1', file_name: 'hotel-invoice-600.pdf', content_type: 'application/pdf', sha256: sha, parse_status: 'PARSED' },
      { document_id: 'DOC-1024-2', file_name: 'trip-itinerary.pdf', content_type: 'application/pdf', sha256: sha, parse_status: 'PARSED' },
    ],
    erp_status: 'SUCCEEDED',
    active_run_id: '00000000-0000-0000-0000-000000001024',
    data_source: 'mock',
    decision_result: {
      schema_version: '1.0.0',
      claim_id: 'CLM-2026-1024',
      run_id: '00000000-0000-0000-0000-000000001024',
      decision: 'APPROVE',
      risk_level: 'LOW',
      hard_gate_passed: true,
      rule_results: [
        { rule_id: 'HOTEL_CITY_LIMIT', outcome: 'PASS', severity: 'INFO', reason: '深圳住宿 600.00 未超过限额 700.00', actual: '600.00', limit: '700.00', policy_version_id: 'POL-SZ-2026' },
        { rule_id: 'INVOICE_VALID', outcome: 'PASS', severity: 'INFO', reason: '发票查验通过，抬头与报销主体一致', actual: null, limit: null, policy_version_id: null },
      ],
      policy_evidence: [POL_SZ_2026],
      reason: '深圳住宿 600.00 低于制度限额 700.00，发票合规，证据完整。',
      confidence: 0.96,
      requires_human: false,
      decided_at: '2026-10-07T01:12:41Z',
    },
  },

  /* Case 2：深圳酒店 800 元 / 标准 700 → REJECT（确定性 BLOCK） */
  {
    claim_id: 'CLM-2026-1025',
    employee: { employee_id: 'EMP-0123', name: '李娜', department: '市场部', legal_entity: 'DEMO-CN' },
    expense: { category: 'HOTEL', city: '深圳', amount: '800.00', currency: 'CNY', incurred_on: '2026-10-02', nights: 1 },
    status: 'COMPLETED',
    created_at: '2026-10-07T01:26:05Z',
    updated_at: '2026-10-07T01:26:15Z',
    documents: [
      { document_id: 'DOC-1025-1', file_name: 'hotel-invoice-800.pdf', content_type: 'application/pdf', sha256: sha, parse_status: 'PARSED' },
    ],
    erp_status: 'SUCCEEDED',
    active_run_id: '00000000-0000-0000-0000-000000001025',
    data_source: 'mock',
    decision_result: {
      schema_version: '1.0.0',
      claim_id: 'CLM-2026-1025',
      run_id: '00000000-0000-0000-0000-000000001025',
      decision: 'REJECT',
      risk_level: 'HIGH',
      hard_gate_passed: true,
      rule_results: [
        { rule_id: 'HOTEL_CITY_LIMIT', outcome: 'FAIL', severity: 'BLOCK', reason: '深圳住宿 800.00 超过制度限额 700.00', actual: '800.00', limit: '700.00', policy_version_id: 'POL-SZ-2026' },
        { rule_id: 'INVOICE_VALID', outcome: 'PASS', severity: 'INFO', reason: '发票查验通过', actual: null, limit: null, policy_version_id: null },
      ],
      policy_evidence: [POL_SZ_2026],
      reason: '确定性规则不通过：住宿金额超出深圳限额，且无事前审批。',
      confidence: 1.0,
      requires_human: false,
      decided_at: '2026-10-07T01:26:13Z',
    },
  },

  /* Case 3：2025 制度 900 / 2026 制度 700，2026 年发生 800 → 当前有效制度优先 → HUMAN_REVIEW */
  {
    claim_id: 'CLM-2026-1026',
    employee: { employee_id: 'EMP-0156', name: '王强', department: '行政部', legal_entity: 'DEMO-CN' },
    expense: { category: 'HOTEL', city: '深圳', amount: '800.00', currency: 'CNY', incurred_on: '2026-03-15', nights: 1 },
    status: 'PENDING_HUMAN',
    created_at: '2026-10-07T01:31:47Z',
    updated_at: '2026-10-07T01:32:00Z',
    documents: [
      { document_id: 'DOC-1026-1', file_name: 'hotel-invoice-800.pdf', content_type: 'application/pdf', sha256: sha, parse_status: 'PARSED' },
    ],
    erp_status: 'NOT_STARTED',
    active_run_id: '00000000-0000-0000-0000-000000001026',
    data_source: 'mock',
    decision_result: {
      schema_version: '1.0.0',
      claim_id: 'CLM-2026-1026',
      run_id: '00000000-0000-0000-0000-000000001026',
      decision: 'HUMAN_REVIEW',
      risk_level: 'MEDIUM',
      hard_gate_passed: true,
      rule_results: [
        { rule_id: 'HOTEL_CITY_LIMIT', outcome: 'UNKNOWN', severity: 'REVIEW', reason: '制度版本交界：按 POL-SZ-2026 超限额 100.00，按已失效 POL-SZ-2025 未超限额，需人工确认适用版本', actual: '800.00', limit: '700.00', policy_version_id: 'POL-SZ-2026' },
        { rule_id: 'INVOICE_VALID', outcome: 'PASS', severity: 'INFO', reason: '发票查验通过', actual: null, limit: null, policy_version_id: null },
      ],
      policy_evidence: [POL_SZ_2026, POL_SZ_2025],
      reason: '命中两个制度版本且结论冲突，规则无法确定性判定，转人工复核。',
      confidence: 0.62,
      requires_human: true,
      decided_at: null, // Contract：HUMAN_REVIEW 的 decided_at 为 null
    },
  },

  /* Case 4：制度未覆盖（招待费场景）→ HUMAN_REVIEW */
  {
    claim_id: 'CLM-2026-1027',
    employee: { employee_id: 'EMP-0201', name: '陈静', department: '财务部', legal_entity: 'DEMO-CN' },
    // Contract 无 ENTERTAINMENT category，此处降级使用 OTHER；已提 API_CHANGE_REQUEST
    expense: { category: 'OTHER', city: '广州', amount: '2000.00', currency: 'CNY', incurred_on: '2026-09-30', nights: 1 },
    status: 'PENDING_HUMAN',
    created_at: '2026-10-07T01:48:12Z',
    updated_at: '2026-10-07T01:48:25Z',
    documents: [
      { document_id: 'DOC-1027-1', file_name: 'dinner-invoice-2000.pdf', content_type: 'application/pdf', sha256: sha, parse_status: 'PARSED' },
    ],
    erp_status: 'NOT_STARTED',
    active_run_id: '00000000-0000-0000-0000-000000001027',
    data_source: 'mock',
    decision_result: {
      schema_version: '1.0.0',
      claim_id: 'CLM-2026-1027',
      run_id: '00000000-0000-0000-0000-000000001027',
      decision: 'HUMAN_REVIEW',
      risk_level: 'MEDIUM',
      hard_gate_passed: true,
      rule_results: [
        { rule_id: 'EXPENSE_STANDARD_MATCH', outcome: 'UNKNOWN', severity: 'REVIEW', reason: '制度未覆盖该场景金额标准，无法机判', actual: '2000.00', limit: null, policy_version_id: 'POL-ENT-2024' },
        { rule_id: 'AMOUNT_OUTLIER', outcome: 'UNKNOWN', severity: 'REVIEW', reason: '金额高于同类历史中位数，触发离群复核', actual: '2000.00', limit: '1800.00', policy_version_id: null },
      ],
      policy_evidence: [POL_ENT],
      reason: '制度未覆盖该场景且金额离群，置信度不足，转人工复核。',
      confidence: 0.41,
      requires_human: true,
      decided_at: null,
    },
  },

  /* Case 5：ERP API Timeout → PENDING_ERP（decision_result = null，等待恢复） */
  {
    claim_id: 'CLM-2026-1028',
    employee: { employee_id: 'EMP-0233', name: '刘洋', department: '技术部', legal_entity: 'DEMO-CN' },
    expense: { category: 'TRANSPORT', city: '上海', amount: '1500.00', currency: 'CNY', incurred_on: '2026-10-05', nights: 1 },
    status: 'PENDING_ERP',
    created_at: '2026-10-07T01:52:08Z',
    updated_at: '2026-10-07T01:55:38Z',
    documents: [
      { document_id: 'DOC-1028-1', file_name: 'air-ticket-1500.pdf', content_type: 'application/pdf', sha256: sha, parse_status: 'UNPARSED' },
    ],
    erp_status: 'FAILED',
    active_run_id: '00000000-0000-0000-0000-000000001028',
    data_source: 'mock',
    decision_result: null, // 流程挂起，尚未产生决策
  },
];

/* ---------------- 审计事件（DEMO MOCK） ----------------
 * Contract 的 AuditEvent = {event_id, claim_id, run_id, event_type, actor, occurred_at, payload}
 * 演示事件放在 payload：{status, duration_ms, message, attempt?}
 * 真实模式下事件流以后端 LangGraph 事件为准。
 */

let seq = 0;
function ev(
  claim_id: string,
  run_id: string,
  event_type: string,
  occurred_at: string,
  payload: Record<string, unknown>,
  actor = 'system'
): AuditEvent {
  seq += 1;
  return {
    event_id: `00000000-0000-0000-0000-${String(seq).padStart(12, '0')}`,
    claim_id,
    run_id,
    event_type,
    actor,
    occurred_at,
    payload,
  };
}

export const DEMO_AUDIT_EVENTS: Record<string, AuditEvent[]> = {
  'CLM-2026-1024': [
    ev('CLM-2026-1024', '00000000-0000-0000-0000-000000001024', 'CLAIM_CREATED', '2026-10-07T01:12:33Z', { status: 'SUCCESS', duration_ms: 8, message: '报销单创建成功：HOTEL / 深圳 / 600.00 CNY' }),
    ev('CLM-2026-1024', '00000000-0000-0000-0000-000000001024', 'ERP_CONTEXT_LOADED', '2026-10-07T01:12:35Z', { status: 'SUCCESS', duration_ms: 2360, message: 'ERP 上下文加载成功：员工主数据 / 成本中心 / 预算余量' }),
    ev('CLM-2026-1024', '00000000-0000-0000-0000-000000001024', 'RULE_EVALUATED', '2026-10-07T01:12:36Z', { status: 'SUCCESS', duration_ms: 1420, message: '执行 2 条规则：2 PASS / 0 FAIL / 0 UNKNOWN' }),
    ev('CLM-2026-1024', '00000000-0000-0000-0000-000000001024', 'POLICY_RETRIEVED', '2026-10-07T01:12:38Z', { status: 'SUCCESS', duration_ms: 1200, message: '命中制度条款：POL-SZ-2026（2026 版，生效中）' }),
    ev('CLM-2026-1024', '00000000-0000-0000-0000-000000001024', 'DECISION_PRODUCED', '2026-10-07T01:12:41Z', { status: 'SUCCESS', duration_ms: 3230, message: 'DecisionResult 生成：APPROVE / LOW / confidence 0.96' }),
    ev('CLM-2026-1024', '00000000-0000-0000-0000-000000001024', 'ERP_WRITE_SUCCEEDED', '2026-10-07T01:12:42Z', { status: 'SUCCESS', duration_ms: 1180, message: '审核结果回写 ERP 成功（凭证 ACC-2026-100871）' }),
  ],
  'CLM-2026-1025': [
    ev('CLM-2026-1025', '00000000-0000-0000-0000-000000001025', 'CLAIM_CREATED', '2026-10-07T01:26:05Z', { status: 'SUCCESS', duration_ms: 7, message: '报销单创建成功：HOTEL / 深圳 / 800.00 CNY' }),
    ev('CLM-2026-1025', '00000000-0000-0000-0000-000000001025', 'ERP_CONTEXT_LOADED', '2026-10-07T01:26:07Z', { status: 'SUCCESS', duration_ms: 2450, message: 'ERP 上下文加载成功' }),
    ev('CLM-2026-1025', '00000000-0000-0000-0000-000000001025', 'RULE_EVALUATED', '2026-10-07T01:26:09Z', { status: 'SUCCESS', duration_ms: 1340, message: '执行 2 条规则：1 PASS / 1 FAIL（HOTEL_CITY_LIMIT，severity BLOCK）' }),
    ev('CLM-2026-1025', '00000000-0000-0000-0000-000000001025', 'POLICY_RETRIEVED', '2026-10-07T01:26:10Z', { status: 'SUCCESS', duration_ms: 1180, message: '命中制度条款：POL-SZ-2026（2026 版，生效中）' }),
    ev('CLM-2026-1025', '00000000-0000-0000-0000-000000001025', 'DECISION_PRODUCED', '2026-10-07T01:26:13Z', { status: 'SUCCESS', duration_ms: 3210, message: 'DecisionResult 生成：REJECT / HIGH / confidence 1.0' }),
    ev('CLM-2026-1025', '00000000-0000-0000-0000-000000001025', 'ERP_WRITE_SUCCEEDED', '2026-10-07T01:26:14Z', { status: 'SUCCESS', duration_ms: 1210, message: '拒绝结果回写 ERP 成功，原因已推送员工' }),
  ],
  'CLM-2026-1026': [
    ev('CLM-2026-1026', '00000000-0000-0000-0000-000000001026', 'CLAIM_CREATED', '2026-10-07T01:31:47Z', { status: 'SUCCESS', duration_ms: 9, message: '报销单创建成功：HOTEL / 深圳 / 800.00 CNY' }),
    ev('CLM-2026-1026', '00000000-0000-0000-0000-000000001026', 'ERP_CONTEXT_LOADED', '2026-10-07T01:31:49Z', { status: 'SUCCESS', duration_ms: 2480, message: 'ERP 上下文加载成功' }),
    ev('CLM-2026-1026', '00000000-0000-0000-0000-000000001026', 'RULE_EVALUATED', '2026-10-07T01:31:51Z', { status: 'SUCCESS', duration_ms: 1390, message: '执行 2 条规则：1 PASS / 1 UNKNOWN（制度版本冲突）' }),
    ev('CLM-2026-1026', '00000000-0000-0000-0000-000000001026', 'POLICY_RETRIEVED', '2026-10-07T01:31:52Z', { status: 'SUCCESS', duration_ms: 1270, message: '命中 2 个制度版本：POL-SZ-2026（生效中）、POL-SZ-2025（已失效）' }),
    ev('CLM-2026-1026', '00000000-0000-0000-0000-000000001026', 'DECISION_PRODUCED', '2026-10-07T01:31:55Z', { status: 'SUCCESS', duration_ms: 3120, message: 'DecisionResult 生成：HUMAN_REVIEW / MEDIUM / requires_human=true' }),
    ev('CLM-2026-1026', '00000000-0000-0000-0000-000000001026', 'HUMAN_REVIEW_REQUESTED', '2026-10-07T01:32:00Z', { status: 'IN_PROGRESS', duration_ms: 0, message: '已进入人工复核队列，等待财务人员处理（SLA 4h）' }),
  ],
  'CLM-2026-1027': [
    ev('CLM-2026-1027', '00000000-0000-0000-0000-000000001027', 'CLAIM_CREATED', '2026-10-07T01:48:12Z', { status: 'SUCCESS', duration_ms: 8, message: '报销单创建成功：OTHER / 广州 / 2000.00 CNY' }),
    ev('CLM-2026-1027', '00000000-0000-0000-0000-000000001027', 'ERP_CONTEXT_LOADED', '2026-10-07T01:48:14Z', { status: 'SUCCESS', duration_ms: 2470, message: 'ERP 上下文加载成功' }),
    ev('CLM-2026-1027', '00000000-0000-0000-0000-000000001027', 'RULE_EVALUATED', '2026-10-07T01:48:16Z', { status: 'SUCCESS', duration_ms: 1360, message: '执行 2 条规则：0 PASS / 2 UNKNOWN（制度未覆盖 + 金额离群）' }),
    ev('CLM-2026-1027', '00000000-0000-0000-0000-000000001027', 'POLICY_RETRIEVED', '2026-10-07T01:48:17Z', { status: 'SUCCESS', duration_ms: 1260, message: '命中制度条款：POL-ENT-2024（未覆盖该场景限额）' }),
    ev('CLM-2026-1027', '00000000-0000-0000-0000-000000001027', 'DECISION_PRODUCED', '2026-10-07T01:48:20Z', { status: 'SUCCESS', duration_ms: 3190, message: 'DecisionResult 生成：HUMAN_REVIEW / MEDIUM / confidence 0.41' }),
    ev('CLM-2026-1027', '00000000-0000-0000-0000-000000001027', 'HUMAN_REVIEW_REQUESTED', '2026-10-07T01:48:25Z', { status: 'IN_PROGRESS', duration_ms: 0, message: '已进入人工复核队列，等待财务人员处理（SLA 4h）' }),
  ],
  'CLM-2026-1028': [
    ev('CLM-2026-1028', '00000000-0000-0000-0000-000000001028', 'CLAIM_CREATED', '2026-10-07T01:52:08Z', { status: 'SUCCESS', duration_ms: 7, message: '报销单创建成功：TRANSPORT / 上海 / 1500.00 CNY' }),
    ev('CLM-2026-1028', '00000000-0000-0000-0000-000000001028', 'ERP_CONTEXT_LOADED', '2026-10-07T01:52:38Z', { status: 'FAILED', duration_ms: 30050, attempt: 1, message: 'ERP 上下文加载失败：ERP API Timeout（SAP-PI 网关 30s 无响应）' }),
    ev('CLM-2026-1028', '00000000-0000-0000-0000-000000001028', 'ERP_RETRY_SCHEDULED', '2026-10-07T01:52:38Z', { status: 'RETRYING', duration_ms: 0, attempt: 2, message: '重试已调度（指数退避 2/3）；claim 置为 PENDING_ERP，支持 Resume 从断点恢复' }),
  ],
};

/* ---------------- Dashboard（DEMO MOCK） ---------------- */

export function demoMetrics(claims: ClaimDetail[]): DashboardMetrics {
  const by_status = { NEW: 0, AUDITING: 0, PENDING_HUMAN: 0, PENDING_ERP: 0, COMPLETED: 0, FAILED: 0 };
  const by_decision = { APPROVE: 0, REJECT: 0, HUMAN_REVIEW: 0 };
  claims.forEach((c) => {
    by_status[c.status] += 1;
    if (c.decision_result) by_decision[c.decision_result.decision] += 1;
  });
  return {
    total_claims: claims.length,
    by_status,
    by_decision,
    pending_human: by_status.PENDING_HUMAN,
    pending_erp: by_status.PENDING_ERP,
    data_source: 'mock',
  };
}

/** Contract 未定义的演示指标（仅 Mock 模式展示，标注 DEMO MOCK） */
export const DEMO_ONLY_METRICS: DemoOnlyMetrics = {
  automation_rate: 0.4,
  human_review_rate: 0.4,
  false_approval_rate: 0.008,
  avg_processing_ms: 4200,
};

/* ---------------- Case 5 Resume 后的预置结果（DEMO MOCK） ---------------- */

export const DEMO_RESUMED_DECISION = {
  schema_version: '1.0.0',
  claim_id: 'CLM-2026-1028',
  run_id: '00000000-0000-0000-0000-000000001028',
  decision: 'APPROVE' as const,
  risk_level: 'LOW' as const,
  hard_gate_passed: true,
  rule_results: [
    { rule_id: 'AIR_CITY_LIMIT', outcome: 'PASS' as const, severity: 'INFO' as const, reason: '上海往返机票 1500.00 未超过限额 2000.00', actual: '1500.00', limit: '2000.00', policy_version_id: 'POL-AIR-2026' },
  ],
  policy_evidence: [POL_AIR_2026],
  reason: 'ERP 上下文恢复后完成审核：机票金额在制度限额内，证据完整。',
  confidence: 0.94,
  requires_human: false,
  decided_at: '2026-10-07T02:05:12Z',
};
