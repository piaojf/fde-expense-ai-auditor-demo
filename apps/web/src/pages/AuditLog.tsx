import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api, USE_MOCK_API } from '../lib/api/client';
import { getRevision } from '../lib/mock/mock-store';
import { getRunContext, saveRunContext } from '../lib/session/runContext';
import Layout from '../components/Layout';
import RecordId from '../components/RecordId';
import { DecisionBadge, DemoMockTag, RiskBadge, StatusBadge } from '../components/Badges';
import { Metric } from '../components/ui';
import { useAsync } from '../hooks/useAsync';
import AuditTimeline from '../components/AuditTimeline';
import { CATEGORY_LABEL, fmtDate, fmtDuration, fmtMoney } from '../utils/format';
import { describeApiError } from '../utils/errors';

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** M3：重试 / 故障恢复相关事件数（事件名全部来自后端，前端不创造） */
const M3_EVENTS = [
  'DEPENDENCY_CALL_ATTEMPTED',
  'DEPENDENCY_CALL_FAILED',
  'DEPENDENCY_RECOVERED',
  'DEPENDENCY_NON_RETRYABLE',
  'RETRY_SCHEDULED',
  'RETRY_ATTEMPTED',
  'RETRY_EXHAUSTED',
  'IDEMPOTENT_REPLAY',
  'RECOVERY_REQUIRED',
  'WORKFLOW_RECOVERY_RESUMED',
  'ERP_WRITE_SKIPPED',
];

export default function AuditLog() {
  const { id = '' } = useParams();
  const { data: claim } = useAsync(() => api.getClaim(id), [id]);
  const { data: log, loading, error, reload } = useAsync(() => api.getAuditLog(id, { limit: 50, offset: 0 }), [id]);

  const [resuming, setResuming] = useState(false);
  const [resumeMsg, setResumeMsg] = useState<string | null>(null);

  const ctx = getRunContext(id);
  const canResume =
    (claim?.status === 'PENDING_HUMAN' && !!ctx?.review_submitted) ||
    claim?.status === 'PENDING_ERP' ||
    claim?.erp_status === 'FAILED';

  async function handleResume() {
    setResuming(true);
    setResumeMsg(null);
    try {
      const res = await api.resume(id, {
        run_id: claim?.active_run_id ?? ctx?.run_id ?? '',
        expected_revision: USE_MOCK_API ? getRevision(id) : ctx?.revision ?? 0,
        idempotency_key: `ui-resume-${id}-${Date.now()}`,
      });
      saveRunContext(id, { run_id: res.run_id, revision: res.revision, review_submitted: false });
      setResumeMsg(`恢复请求已提交，系统将从上次中断的位置继续处理，请稍后查看记录。`);
      for (let i = 0; i < 10; i += 1) {
        await sleep(1200);
        const latest = await api.getClaim(id);
        if (latest.status === 'COMPLETED' || latest.status === 'FAILED') break;
      }
      reload();
    } catch (e) {
      setResumeMsg(describeApiError(e, '恢复处理'));
    } finally {
      setResuming(false);
    }
  }

  const items = log?.items ?? [];
  const durations = items
    .map((e) => e.payload?.duration_ms)
    .filter((v): v is number => typeof v === 'number');
  const totalMs = durations.reduce((s, v) => s + v, 0);
  const abnormal = items.filter((e) => e.payload?.status === 'FAILED' || e.payload?.status === 'RETRYING').length;
  const recoveryEvents = items.filter((e) => M3_EVENTS.includes(e.event_type)).length;
  const replayCount = items.filter((e) => e.event_type === 'IDEMPOTENT_REPLAY').length;
  const eventTotal = log?.total ?? 0;

  return (
    <Layout title="审计追踪">
      <div className="page-header">
        <div>
          <div className="page-title">审计追踪</div>
          <div className="page-desc">
            只追加事件流：事件编号 / 事件类型 / 操作者 / 发生时间 / 事件数据 —— 可审计 · 可追踪 · 可恢复
          </div>
        </div>
        <div className="page-actions">
          {canResume && (
            <button className="btn btn-primary" disabled={resuming} onClick={handleResume}>
              {resuming ? '恢复中…' : claim?.status === 'PENDING_ERP' ? '⟳ 恢复工作流' : '⟳ 恢复处理'}
            </button>
          )}
          <Link to={`/claims/${id}`} className="btn">
            返回单据详情
          </Link>
        </div>
      </div>

      {resumeMsg && (
        <div className="alert alert-success" role="status">
          {resumeMsg}
        </div>
      )}

      {claim && (
        <div className="card summary-card">
          <div className="summary-head">
            <div>
              <div className="summary-id">{claim.expense.city} · {CATEGORY_LABEL[claim.expense.category]}</div>
              <RecordId value={claim.claim_id} full copy />
              <div className="summary-badges">
                <StatusBadge status={claim.status} />
                <DecisionBadge decision={claim.decision_result?.decision ?? null} />
                <RiskBadge level={claim.decision_result?.risk_level ?? null} />
                {USE_MOCK_API && <DemoMockTag text="演示事件" />}
              </div>
              <div className="summary-meta">
                <span>
                  员工 <b>{claim.employee.name}</b>
                </span>
                <span>
                  部门 <b>{claim.employee.department}</b>
                </span>
                <span>
                  费用发生日 <b>{fmtDate(claim.expense.incurred_on)}</b>
                </span>
                <span>
                  申报金额 <b>{fmtMoney(claim.expense.amount)}</b>
                </span>
              </div>
            </div>
          </div>
          <div className="summary-metrics audit-metrics">
            <Metric label="事件总数" value={eventTotal} note="后端返回的审计事件条数" />
            <Metric
              label="累计处理耗时"
              value={durations.length > 0 ? fmtDuration(totalMs) : '—'}
              note={durations.length > 0 ? `由 ${durations.length} 条含耗时的事件累加` : '当前事件均未携带耗时'}
            />
            <Metric
              label="异常 / 重试事件"
              value={`${abnormal} 次`}
              tone={abnormal > 0 ? 'var(--danger)' : undefined}
              note="状态为失败或重试中"
            />
            <Metric label="故障恢复事件" value={`${recoveryEvents} 次`} note="依赖调用、重试与恢复类事件" />
            {replayCount > 0 && <Metric label="幂等复用" value={`${replayCount} 次`} note="未重复写入 ERP" />}
          </div>
        </div>
      )}

      <div className="card">
        <div className="card-title">
          <span>事件时间线</span>
          <span className="card-title-extra">共 {eventTotal} 条</span>
        </div>
        <AuditTimeline
          items={items}
          loading={loading}
          error={error}
          emptyHint="执行审核或复核后，可在此查看处理记录。"
        />
        <div className="section-note">
          审计记录用于追踪审核、人工复核与 ERP 同步的处理过程。
          事件类型、状态与事件数据均来自后端只追加事件流，前端不做推断。
        </div>
      </div>
    </Layout>
  );
}
