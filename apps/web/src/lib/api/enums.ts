/** Contract v1.0.0 枚举（大写字符串） */

export type ClaimStatus =
  | 'NEW'
  | 'AUDITING'
  | 'PENDING_HUMAN'
  | 'PENDING_ERP'
  | 'COMPLETED'
  | 'FAILED';

export type Decision = 'APPROVE' | 'REJECT' | 'HUMAN_REVIEW';

export type RiskLevel = 'LOW' | 'MEDIUM' | 'HIGH';

export type RuleOutcome = 'PASS' | 'FAIL' | 'UNKNOWN';

export type Severity = 'INFO' | 'REVIEW' | 'BLOCK';

export const CLAIM_STATUSES: ClaimStatus[] = [
  'NEW',
  'AUDITING',
  'PENDING_HUMAN',
  'PENDING_ERP',
  'COMPLETED',
  'FAILED',
];

export const DECISIONS: Decision[] = ['APPROVE', 'REJECT', 'HUMAN_REVIEW'];
