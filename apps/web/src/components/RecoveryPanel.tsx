import type { ClaimStatus, Decision } from '../lib/api/enums';
import type { ErpStatus } from '../lib/api/types';
import { CLAIM_STATUS_LABEL, DECISION_LABEL, ERP_STATUS_LABEL } from '../utils/format';

/**
 * ERP 故障恢复面板（M3）
 *
 * 只在 Backend 公开状态为 PENDING_ERP 时展示。
 *
 * 设计要点（重要 FDE 区分）：
 *  - PENDING_ERP = 业务审核已完成，但外部系统（ERP）暂时不可用
 *  - REJECT      = 业务审核拒绝
 *  二者语义完全不同，界面绝不能把 ERP 故障显示成“报销被拒绝”。
 *
 * 边界：前端不判断故障类型、不自行恢复状态，
 *       点击按钮实际调用 POST /claims/{id}/resume，由 Backend 决定如何恢复。
 */
export default function RecoveryPanel({
  status,
  erpStatus,
  decision,
  busy,
  onResume,
}: {
  status: ClaimStatus;
  erpStatus: ErpStatus;
  decision: Decision | null;
  busy: boolean;
  onResume: () => void;
}) {
  if (status !== 'PENDING_ERP') return null;

  return (
    <div className="card" style={{ borderLeft: '3px solid #d97706', marginBottom: 12 }}>
      <div className="card-title" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        ERP 写回失败
        <span className="badge badge-no-dot badge-tag-orange">基础设施故障</span>
      </div>

      <div className="kv-list" style={{ marginTop: 4 }}>
        <div className="kv">
          <span className="k">状态</span>
          <span className="v">{CLAIM_STATUS_LABEL[status]}</span>
        </div>
        <div className="kv">
          <span className="k">
            ERP 同步状态
          </span>
          <span className="v">{ERP_STATUS_LABEL[erpStatus]}</span>
        </div>
        <div className="kv">
          <span className="k">业务审核结论</span>
          <span className="v">{decision ? DECISION_LABEL[decision] : '—'}</span>
        </div>
      </div>

      <div style={{ fontSize: 12, color: 'var(--text-2)', lineHeight: 1.9, marginTop: 8 }}>
        业务审核已经完成{decision ? `（${DECISION_LABEL[decision]}）` : ''}，但 ERP 暂时不可用，
        报销单挂起等待外部系统恢复。
        <br />
        <b style={{ color: '#d97706' }}>
          这不是报销被拒绝：审核结论不受影响，恢复后流程会继续，不会重新执行上游审核。
        </b>
      </div>

      <div style={{ marginTop: 10 }}>
        <button className="btn btn-primary" disabled={busy} onClick={onResume}>
          {busy ? '恢复中…' : '⟳ 恢复工作流'}
        </button>
      </div>

      <div style={{ fontSize: 11, color: 'var(--text-3)', lineHeight: 1.7, marginTop: 8 }}>
        确认 ERP 服务已恢复后，点击「恢复工作流」继续同步，并在审计追踪中查看结果。
      </div>
    </div>
  );
}
