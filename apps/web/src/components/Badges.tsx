import type { ClaimStatus, Decision, RiskLevel, RuleOutcome, Severity } from '../lib/api/enums';
import type { ErpStatus, ExpenseCategory, ParseStatus } from '../lib/api/types';
import {
  CATEGORY_LABEL,
  CLAIM_STATUS_LABEL,
  DECISION_LABEL,
  ERP_STATUS_LABEL,
  OUTCOME_LABEL,
  PARSE_STATUS_LABEL,
  RISK_LABEL,
  SEVERITY_LABEL,
} from '../utils/format';

export function RiskBadge({ level }: { level: RiskLevel | null | undefined }) {
  if (!level) return <span className="badge badge-pending">—</span>;
  const cls = level === 'LOW' ? 'badge-low' : level === 'MEDIUM' ? 'badge-medium' : 'badge-high';
  return <span className={`badge ${cls}`}>{RISK_LABEL[level]}</span>;
}

export function DecisionBadge({ decision }: { decision: Decision | null | undefined }) {
  if (!decision) return <span className="badge badge-pending">未决策</span>;
  const map: Record<Decision, string> = {
    APPROVE: 'badge-approved',
    REJECT: 'badge-rejected',
    HUMAN_REVIEW: 'badge-human_review',
  };
  return <span className={`badge ${map[decision]}`}>{DECISION_LABEL[decision]}</span>;
}

export function StatusBadge({ status }: { status: ClaimStatus }) {
  const map: Record<ClaimStatus, string> = {
    NEW: 'badge-status-new',
    AUDITING: 'badge-status-auditing',
    PENDING_HUMAN: 'badge-status-pending_human',
    PENDING_ERP: 'badge-status-pending_erp',
    COMPLETED: 'badge-status-completed',
    FAILED: 'badge-status-failed',
  };
  return <span className={`badge ${map[status]}`}>{CLAIM_STATUS_LABEL[status]}</span>;
}

export function OutcomeBadge({ outcome }: { outcome: RuleOutcome }) {
  const map: Record<RuleOutcome, string> = {
    PASS: 'badge-tag-green',
    FAIL: 'badge-tag-red',
    UNKNOWN: 'badge-tag-gray',
  };
  return <span className={`badge badge-no-dot ${map[outcome]}`}>{OUTCOME_LABEL[outcome]}</span>;
}

export function SeverityBadge({ severity }: { severity: Severity }) {
  const map: Record<Severity, string> = {
    INFO: 'badge-tag-gray',
    REVIEW: 'badge-tag-orange',
    BLOCK: 'badge-tag-red',
  };
  return <span className={`badge badge-no-dot ${map[severity]}`}>{SEVERITY_LABEL[severity]}</span>;
}

export function ErpBadge({ status }: { status: ErpStatus }) {
  const map: Record<ErpStatus, string> = {
    NOT_STARTED: 'badge-tag-gray',
    PENDING: 'badge-tag-blue',
    SUCCEEDED: 'badge-tag-green',
    FAILED: 'badge-tag-red',
  };
  return (
    <span className={`badge badge-no-dot ${map[status]}`}>ERP：{ERP_STATUS_LABEL[status]}</span>
  );
}

export function ParseBadge({ status }: { status: ParseStatus }) {
  const map: Record<ParseStatus, string> = {
    UNPARSED: 'badge-tag-gray',
    PARSED: 'badge-tag-green',
    FAILED: 'badge-tag-red',
  };
  return <span className={`badge badge-no-dot ${map[status]}`}>{PARSE_STATUS_LABEL[status]}</span>;
}

export function CategoryBadge({ category }: { category: ExpenseCategory }) {
  return <span className="badge badge-no-dot badge-tag">{CATEGORY_LABEL[category] ?? category}</span>;
}

/** 标记为 DEMO MOCK 的演示数据 */
export function DemoMockTag({ text = 'DEMO MOCK' }: { text?: string }) {
  return <span className="badge badge-no-dot badge-tag-orange">{text}</span>;
}
