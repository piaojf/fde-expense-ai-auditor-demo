/** Display mappings only; no approval, risk or policy decisions are made here. */
export const RULE_CHECK_LABEL: Record<string, string> = { PASS: '符合', FAIL: '不符合', UNKNOWN: '待核实' };
export const ADVISORY_STATUS_LABEL: Record<string, string> = {
  SUCCEEDED: '建议校验通过', PARTIAL: '部分建议无效', FAILED: '分析失败', NOT_RUN: '未运行',
};
export const FINANCE_DECISION_LABEL: Record<string, string> = {
  PENDING: '待财务审核', NOT_DECIDED: '待财务审核', APPROVED: '已批准',
  REJECTED: '未批准', RETURNED: '退回补证', ESCALATED: '等待例外复核',
};

/** Format decimal strings without doing floating-point business calculations. */
export function recordMoney(value: string | null | undefined): string {
  if (!value || !/^\d+(\.\d{1,2})?$/.test(value)) return '—';
  const [whole, fraction = ''] = value.split('.');
  return `¥${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}.${fraction.padEnd(2, '0')}`;
}

export function lodgingCheck(report: { decision: string; reason: string; exceeded_amount: string | null } | null) {
  if (!report) return { label: '尚未完成检查', tone: 'gray' };
  if (report.decision === 'PASS') return { label: '未超住宿限额', tone: 'green' };
  if (report.decision === 'FAIL') return {
    label: report.exceeded_amount && recordMoney(report.exceeded_amount) !== '—'
      ? `超出限额 ${recordMoney(report.exceeded_amount)}` : '不符合住宿标准', tone: 'red',
  };
  return { label: report.reason === 'STAY_UNVERIFIED' ? '住宿信息待核实' : '待核实', tone: 'orange' };
}
