/**
 * Mock Store —— DEMO MOCK 专用内存态
 *
 * 边界声明：
 *  - 本文件只保存“预置的演示数据”与“事件流水”，**不做任何决策推导**
 *    （不存在 LOW→APPROVE / MEDIUM→HUMAN_REVIEW / HIGH→REJECT 之类的前端逻辑）。
 *  - 所有 decision / risk_level 均来自预置的 DecisionResult（模拟后端 Decision Engine 的返回）。
 *  - 接入真实后端（USE_MOCK_API=false）后，本文件整体不参与运行。
 */
import { useSyncExternalStore } from 'react';
import {
  ApiError,
  type AuditEvent,
  type AuditRequest,
  type ClaimDetail,
  type HumanReviewRequest,
  type OperationResponse,
  type ResumeRequest,
} from '../api/types';
import { DEMO_AUDIT_EVENTS, DEMO_CLAIMS, DEMO_RESUMED_DECISION } from './demo-cases';

interface MockState {
  claims: ClaimDetail[];
  auditLogs: Record<string, AuditEvent[]>;
  revisions: Record<string, number>;
}

let state: MockState = {
  claims: DEMO_CLAIMS,
  auditLogs: DEMO_AUDIT_EVENTS,
  revisions: Object.fromEntries(DEMO_CLAIMS.map((c) => [c.claim_id, 1])),
};

const listeners = new Set<() => void>();

function setState(next: Partial<MockState>) {
  state = { ...state, ...next };
  listeners.forEach((l) => l());
}

export function getLatestState(): MockState {
  return state;
}

export function useMockStore(): MockState {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    getLatestState,
    getLatestState
  );
}

function nowIso(): string {
  return new Date().toISOString();
}

function patchClaim(claim_id: string, patch: Partial<ClaimDetail>): ClaimDetail {
  let updated: ClaimDetail | null = null;
  const claims = state.claims.map((c) => {
    if (c.claim_id !== claim_id) return c;
    updated = { ...c, ...patch, updated_at: nowIso() };
    return updated;
  });
  setState({ claims });
  return updated ?? state.claims.find((c) => c.claim_id === claim_id)!;
}

function appendEvents(claim_id: string, run_id: string, events: Omit<AuditEvent, 'event_id' | 'claim_id' | 'run_id'>[]) {
  const existing = state.auditLogs[claim_id] ?? [];
  const added: AuditEvent[] = events.map((e, i) => ({
    ...e,
    event_id: `mock-${Date.now()}-${i}`,
    claim_id,
    run_id,
  }));
  setState({ auditLogs: { ...state.auditLogs, [claim_id]: [...existing, ...added] } });
}

function nextRevision(claim_id: string): number {
  const rev = (state.revisions[claim_id] ?? 1) + 1;
  setState({ revisions: { ...state.revisions, [claim_id]: rev } });
  return rev;
}

/** 当前 revision（用于 HumanReviewRequest / ResumeRequest 的 expected_revision） */
export function getRevision(claim_id: string): number {
  return state.revisions[claim_id] ?? 1;
}

function assertRevision(claim_id: string, expected_revision: number) {
  const current = getRevision(claim_id);
  if (expected_revision !== current) {
    throw new ApiError(409, 'REVISION_CONFLICT', `expected_revision=${expected_revision} 与当前 revision=${current} 不一致`);
  }
}

/**
 * MOCK：POST /claims/{claim_id}/audit
 * 说明：Mock 模式不执行真实审核，仅返回该单据**预置**的 DecisionResult（如有），
 *      用于在后端 M1 未完成前演示完整交互链路。不产生任何前端推导的决策。
 */
export function mockRunAudit(claim_id: string, _req: AuditRequest): OperationResponse {
  const claim = state.claims.find((c) => c.claim_id === claim_id);
  if (!claim) throw new Error(`Claim ${claim_id} not found`);
  const run_id = claim.active_run_id ?? '00000000-0000-0000-0000-000000000000';

  appendEvents(claim_id, run_id, [
    { event_type: 'AUDIT_STARTED', actor: 'system', occurred_at: nowIso(), payload: { status: 'SUCCESS', duration_ms: 5, message: 'DEMO MOCK：触发审核执行（真实执行由后端 LangGraph 提供）' } },
  ]);

  const decision_result = claim.decision_result ?? null;
  const status = claim.status === 'NEW' ? (decision_result ? 'COMPLETED' : 'AUDITING') : claim.status;
  if (status !== claim.status) patchClaim(claim_id, { status });

  const revision = nextRevision(claim_id);
  return { claim_id, run_id, status, revision, decision_result };
}

/** MOCK：POST /claims/{claim_id}/human-review */
export function mockHumanReview(claim_id: string, req: HumanReviewRequest): OperationResponse {
  const claim = state.claims.find((c) => c.claim_id === claim_id);
  if (!claim) throw new Error(`Claim ${claim_id} not found`);
  assertRevision(claim_id, req.expected_revision);

  appendEvents(claim_id, req.run_id, [
    {
      event_type: 'HUMAN_REVIEW_RECORDED',
      actor: req.reviewer_id,
      occurred_at: nowIso(),
      payload: {
        status: 'SUCCESS',
        duration_ms: 0,
        message: `人工复核意见已记录：${req.decision}${req.comment ? `；意见：${req.comment}` : ''}`,
      },
    },
  ]);

  const updated = patchClaim(claim_id, {
    status: 'COMPLETED',
    decision_result: claim.decision_result
      ? {
          ...claim.decision_result,
          decision: req.decision,
          requires_human: false,
          decided_at: nowIso(),
        }
      : null,
  });

  const revision = nextRevision(claim_id);
  return { claim_id, run_id: req.run_id, status: updated.status, revision, decision_result: updated.decision_result };
}

/** MOCK：POST /claims/{claim_id}/resume（演示“可恢复”） */
export function mockResume(claim_id: string, req: ResumeRequest): OperationResponse {
  const claim = state.claims.find((c) => c.claim_id === claim_id);
  if (!claim) throw new Error(`Claim ${claim_id} not found`);

  appendEvents(claim_id, req.run_id, [
    { event_type: 'ERP_CONTEXT_LOADED', actor: 'system', occurred_at: nowIso(), payload: { status: 'SUCCESS', duration_ms: 2310, attempt: 3, message: 'DEMO MOCK：Resume 触发，从 ERP 上下文加载断点继续' } },
    { event_type: 'RULE_EVALUATED', actor: 'system', occurred_at: nowIso(), payload: { status: 'SUCCESS', duration_ms: 1380, message: '执行 1 条规则：1 PASS / 0 FAIL / 0 UNKNOWN' } },
    { event_type: 'DECISION_PRODUCED', actor: 'system', occurred_at: nowIso(), payload: { status: 'SUCCESS', duration_ms: 3240, message: 'DecisionResult 生成（预置演示结果）：APPROVE / LOW' } },
    { event_type: 'ERP_WRITE_SUCCEEDED', actor: 'system', occurred_at: nowIso(), payload: { status: 'SUCCESS', duration_ms: 1180, message: '结果回写 ERP 成功' } },
  ]);

  const updated = patchClaim(claim_id, {
    status: 'COMPLETED',
    erp_status: 'SUCCEEDED',
    decision_result: { ...DEMO_RESUMED_DECISION, claim_id },
  });

  const revision = nextRevision(claim_id);
  return { claim_id, run_id: req.run_id, status: updated.status, revision, decision_result: updated.decision_result };
}

/** MOCK：REQUEST_INFO（Contract 未定义的动作，见 API_CHANGE_REQUESTS；仅演示 UI） */
export function mockRequestInfo(claim_id: string, reviewer_id: string, comment: string): OperationResponse {
  const claim = state.claims.find((c) => c.claim_id === claim_id);
  if (!claim) throw new Error(`Claim ${claim_id} not found`);
  const run_id = claim.active_run_id ?? '';

  appendEvents(claim_id, run_id, [
    {
      event_type: 'MORE_INFO_REQUESTED',
      actor: reviewer_id,
      occurred_at: nowIso(),
      payload: { status: 'IN_PROGRESS', duration_ms: 0, message: `DEMO MOCK：要求补充材料${comment ? `；意见：${comment}` : ''}` },
    },
  ]);

  const updated = patchClaim(claim_id, { status: 'PENDING_HUMAN' });
  const revision = nextRevision(claim_id);
  return { claim_id, run_id, status: updated.status, revision, decision_result: updated.decision_result };
}
