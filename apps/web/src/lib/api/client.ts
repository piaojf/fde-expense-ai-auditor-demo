/**
 * API Client —— 严格对齐 docs/api-contract.md v1.0.0
 *
 * 模式切换（环境变量）：
 *   VITE_USE_MOCK_API=true          → 使用 DEMO MOCK 数据（本地演示）
 *   VITE_USE_MOCK_API=false（默认） → 请求真实后端，VITE_API_BASE 默认 http://localhost:8000
 *
 * Contract 要点：
 *   - 路由无 /api 前缀；金额是 decimal string；时间戳是 UTC ISO 8601
 *   - list 返回 {items,total,limit,offset,data_source}；audit-log 同理
 *   - 写路由 M0 返回 501 FEATURE_NOT_IMPLEMENTED：前端展示“未开放”，不得当作审核结果
 *   - 错误体 {error:{code,message,details},request_id}：用 status + error.code 分支，不用 message
 */
import {
  ApiError,
  type AuditLogResponse,
  type AuditRequest,
  type ClaimDetail,
  type ClaimList,
  type ClaimStatus,
  type DashboardMetrics,
  type DemoOnlyMetrics,
  type ErrorResponse,
  type HealthResponse,
  type HumanReviewRequest,
  type OperationResponse,
  type ResumeRequest,
} from './types';
import { demoMetrics, DEMO_CLAIMS, DEMO_ONLY_METRICS } from '../mock/demo-cases';
import {
  getLatestState,
  mockHumanReview,
  mockRequestInfo,
  mockResume,
  mockRunAudit,
} from '../mock/mock-store';

export const USE_MOCK_API: boolean = (import.meta.env.VITE_USE_MOCK_API ?? 'false') !== 'false';
export const API_BASE: string = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

/**
 * 真实联调用例（Backend M2 fixture）：Integration Gate 使用这一个真实 Claim ID。
 * Mock Case（CLM-2026-1024 ~ 1028）继续保留，只用于离线演示。
 */
export const INTEGRATION_CASE_ID = 'CLM-2026-00004';

/* ---------------- 真实 HTTP ---------------- */

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    });
  } catch (e) {
    throw new ApiError(0, 'NETWORK_ERROR', `无法连接后端 ${API_BASE}：${e instanceof Error ? e.message : String(e)}`);
  }

  const text = await res.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }

  if (!res.ok) {
    const err = (body ?? {}) as ErrorResponse;
    const error = err.error ?? { code: 'UNKNOWN', message: `HTTP ${res.status}`, details: {} };
    throw new ApiError(res.status, error.code, error.message, err.request_id ?? '');
  }

  return body as T;
}

const realApi = {
  getHealth: () => http<HealthResponse>('/health'),

  listClaims: (params: { limit?: number; offset?: number; status?: ClaimStatus } = {}) => {
    const qs = new URLSearchParams();
    if (params.limit != null) qs.set('limit', String(params.limit));
    if (params.offset != null) qs.set('offset', String(params.offset));
    if (params.status) qs.set('status', params.status);
    const s = qs.toString();
    return http<ClaimList>(`/claims${s ? `?${s}` : ''}`);
  },

  getClaim: (claim_id: string) => http<ClaimDetail>(`/claims/${claim_id}`),

  getAuditLog: (claim_id: string, params: { limit?: number; offset?: number } = {}) => {
    const qs = new URLSearchParams();
    if (params.limit != null) qs.set('limit', String(params.limit));
    if (params.offset != null) qs.set('offset', String(params.offset));
    const s = qs.toString();
    return http<AuditLogResponse>(`/claims/${claim_id}/audit-log${s ? `?${s}` : ''}`);
  },

  getDashboardMetrics: () => http<DashboardMetrics>('/dashboard/metrics'),

  runAudit: (claim_id: string, req: AuditRequest) =>
    http<OperationResponse>(`/claims/${claim_id}/audit`, { method: 'POST', body: JSON.stringify(req) }),

  submitHumanReview: (claim_id: string, req: HumanReviewRequest) =>
    http<OperationResponse>(`/claims/${claim_id}/human-review`, { method: 'POST', body: JSON.stringify(req) }),

  resume: (claim_id: string, req: ResumeRequest) =>
    http<OperationResponse>(`/claims/${claim_id}/resume`, { method: 'POST', body: JSON.stringify(req) }),
};

/* ---------------- DEMO MOCK ---------------- */

const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

const mockApi = {
  async getHealth(): Promise<HealthResponse> {
    await delay(60);
    return {
      status: 'ok',
      service: 'fde-expense-ai-auditor',
      version: '0.1.0',
      milestone: 'M0',
      data_source: 'mock',
      database: 'not_checked',
    };
  },

  async listClaims(params: { limit?: number; offset?: number; status?: ClaimStatus } = {}): Promise<ClaimList> {
    await delay(100);
    const { claims } = getLatestState();
    const filtered = params.status ? claims.filter((c) => c.status === params.status) : claims;
    const offset = params.offset ?? 0;
    const limit = params.limit ?? 20;
    return {
      items: filtered.slice(offset, offset + limit),
      total: filtered.length,
      limit,
      offset,
      data_source: 'mock',
    };
  },

  async getClaim(claim_id: string): Promise<ClaimDetail> {
    await delay(100);
    const claim = getLatestState().claims.find((c) => c.claim_id === claim_id);
    if (!claim) throw new ApiError(404, 'CLAIM_NOT_FOUND', `未找到报销单 ${claim_id}`);
    return claim;
  },

  async getAuditLog(claim_id: string, params: { limit?: number; offset?: number } = {}): Promise<AuditLogResponse> {
    await delay(80);
    const items = getLatestState().auditLogs[claim_id] ?? [];
    const offset = params.offset ?? 0;
    const limit = params.limit ?? 50;
    return {
      claim_id,
      items: items.slice(offset, offset + limit),
      total: items.length,
      limit,
      offset,
      data_source: 'mock',
    };
  },

  async getDashboardMetrics(): Promise<DashboardMetrics> {
    await delay(120);
    return demoMetrics(getLatestState().claims);
  },

  async runAudit(claim_id: string, req: AuditRequest): Promise<OperationResponse> {
    await delay(400);
    return mockRunAudit(claim_id, req);
  },

  async submitHumanReview(claim_id: string, req: HumanReviewRequest): Promise<OperationResponse> {
    await delay(300);
    return mockHumanReview(claim_id, req);
  },

  async resume(claim_id: string, req: ResumeRequest): Promise<OperationResponse> {
    await delay(400);
    return mockResume(claim_id, req);
  },
};

export const api = USE_MOCK_API ? mockApi : realApi;

/** Contract 未定义的演示指标：仅 Mock 模式返回，真实模式返回 null */
export function getDemoOnlyMetrics(): DemoOnlyMetrics | null {
  return USE_MOCK_API ? DEMO_ONLY_METRICS : null;
}

/** MOCK ONLY：Contract 未定义的 Request More Information 动作 */
export function requestMoreInfo(claim_id: string, reviewer_id: string, comment: string): OperationResponse {
  if (!USE_MOCK_API) {
    throw new ApiError(501, 'FEATURE_NOT_IMPLEMENTED', 'M0 human-review 仅支持 APPROVE / REJECT');
  }
  return mockRequestInfo(claim_id, reviewer_id, comment);
}

export { DEMO_CLAIMS };
