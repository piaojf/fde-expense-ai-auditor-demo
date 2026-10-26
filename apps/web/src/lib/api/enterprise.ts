import type { TravelDetails } from './travel';
import type { ComplianceAnalysis } from './compliance';
import { API_BASE } from './client';
import { ApiError } from './types';
import type { SubmissionFields } from './submission';
import type { InvoiceEvidence, Draft } from './invoices';
import type { ClaimDetail } from './types';

export const ENTERPRISE_MODE = import.meta.env.VITE_ENTERPRISE_MODE === 'true';
export const HOTEL_DEMO_MODE = ENTERPRISE_MODE && import.meta.env.VITE_P4A_ENABLED === 'true'
  && import.meta.env.VITE_M6_DEMO === 'true';
export type Role = 'EMPLOYEE' | 'FINANCE' | 'ADMIN';
export interface User {
  id: string; company_id: string; username: string; name: string;
  department: string; position: string; role: Role; enabled: boolean;
}
export const ROLE_LABEL: Record<Role, string> = { EMPLOYEE: '员工', FINANCE: '财务', ADMIN: '企业管理员' };
export const STAGE_LABEL: Record<string, string> = {
  AI_AUDITING: 'AI 审核中', FINANCE_REVIEW: '财务审核中', FINANCE_APPLYING: '财务审核中',
  RETURNED: '已退回 · 待修改', APPROVED: '审核通过', REJECTED: '审核不通过',
  ERP_SYNC_PENDING: '审核通过 · 入账待恢复', SYSTEM_ERROR: '审核暂时中断',
};
export interface PortalEvent {
  kind: string; actor_name: string; actor_id: string; role: Role; at: string;
  reason?: string; claim_id: string | null; stage?: string; version?: number;
}
export interface PortalVersion {
  version: number; fields: SubmissionFields; submitted_at: string; stage: string;
  ai_result?: ClaimDetail['decision_result']; finance_action?: string;
  finance_reason?: string; reviewer_id?: string; reviewed_at?: string;
  invoice_evidence?: InvoiceEvidence | null; run_id?: string;
}
export interface PortalClaim {
  compliance_analysis?: ComplianceAnalysis | null;
  advisory_analysis?: import('./compliance').AdvisoryReport | null;
  p4a?: boolean;
  claim_id: string; company_id: string; owner_id: string; stage: string;
  version: number; revision: number; reason: string; created_at: string;
  fields: SubmissionFields; claim: ClaimDetail; ai_result: ClaimDetail['decision_result'];
  travel_details?: TravelDetails | null;
  travel_standard_diagnosis?: string | null;
  employee_snapshot?: { department: string; position: string; grade: string };
  ai_analysis?: { summary: string; provider: string; model: string; anomalies: string[] } | null;
  history: PortalVersion[]; timeline: PortalEvent[]; invoice_evidence: InvoiceEvidence | null;
  invoice_draft?: Draft; erp_sync_status: string;
}
let currentUserId = 'anonymous';
export function setStorageUser(user: User | null) { currentUserId = user?.id ?? 'anonymous'; }
export function storageKey(key: string) { return ENTERPRISE_MODE ? `ep-${currentUserId}-${key}` : key; }
export async function portalHttp<T>(path: string, body?: unknown): Promise<T> {
  try {
    const response = await fetch(`${API_BASE}/api/v1.5${path}`, {
      method: body === undefined ? 'GET' : 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(120000),
    });
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 401 && path !== '/auth/login') window.dispatchEvent(new Event('enterprise-auth-expired'));
      throw new ApiError(response.status, data.error?.code ?? 'PORTAL_ERROR', data.error?.message ?? '请求失败，请重试', data.request_id, data.error?.details);
    }
    return data;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError(0, 'NETWORK_ERROR', '服务连接失败或超时，原请求编号已保留，可安全重试。');
  }
}
export async function portalFile(path: string): Promise<Blob> {
  const response = await fetch(`${API_BASE}/api/v1.5${path}`, { credentials: 'include', signal: AbortSignal.timeout(30000) });
  if (!response.ok) {
    const data = await response.json();
    throw new ApiError(response.status, data.error?.code ?? 'FILE_ERROR', data.error?.message ?? '发票文件不可用');
  }
  return response.blob();
}
