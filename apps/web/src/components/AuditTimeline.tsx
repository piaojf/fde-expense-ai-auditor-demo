import type { AuditEvent } from '../lib/api/types';
import { EmptyState } from './ui';
import {
  EVENT_STATUS_CLASS,
  EVENT_STATUS_LABEL,
  EVENT_TYPE_LABEL,
  eventNote,
  fmtDateTime,
  fmtDuration,
  formatAuditError,
} from '../utils/format';

/**
 * 审计时间线（共用展示组件）
 *
 * 只渲染后端真实返回的事件；无事件时显示真实空状态。
 * 事件中文名、状态色、说明文案均来自 utils/format 的映射，
 * 前端不创造事件、不推断重试策略、不判定故障类型。
 */
export default function AuditTimeline({
  items,
  loading,
  error,
  compact = false,
  emptyText = '暂无审计事件',
  emptyHint,
}: {
  items: AuditEvent[];
  loading: boolean;
  error?: string | null;
  compact?: boolean;
  emptyText?: string;
  emptyHint?: React.ReactNode;
}) {
  if (loading) return <div className="loading">加载中…</div>;
  if (error) return <div className="empty">加载失败：{error}</div>;
  if (items.length === 0) return <EmptyState text={emptyText} hint={emptyHint} />;
  return (
    <div className={`timeline${compact ? ' timeline-compact' : ''}`}>
      {items.map((e) => (
        <TimelineItem key={e.event_id} event={e} />
      ))}
    </div>
  );
}

export function TimelineItem({ event }: { event: AuditEvent }) {
  const status = String(event.payload?.status ?? '');
  const duration = event.payload?.duration_ms;
  const attempt = event.payload?.attempt;
  // 真实后端 payload 用 summary，DEMO MOCK 用 message
  const rawMessage = event.payload?.summary ?? event.payload?.message ?? '';
  const message = String(rawMessage);
  const error = event.payload?.error;
  /** M3：把后端事件翻译为业务语言（不推断策略，只用真实 payload） */
  const note = eventNote(event.event_type, event.payload);
  /** 后端标记：本次没有产生新的实际写入（幂等复用） */
  const noSideEffect = event.payload?.actual_side_effect === false;
  /** 后端把 event_type 原样写进 summary 时不再重复展示一行 */
  const showMessage = message.length > 0 && message !== event.event_type;
  /** payload.error 可能是结构化对象，统一交给 formatAuditError 翻译，避免 [object Object] */
  const errorText = formatAuditError(error);
  const label = EVENT_TYPE_LABEL[event.event_type] ?? event.event_type;

  return (
    <div className="tl-item">
      <div className={`tl-dot ${status.toLowerCase() || 'skipped'}`} />
      <div className="tl-head">
        <span className="tl-step">{label}</span>
        {status && (
          <span className={`badge badge-no-dot ${EVENT_STATUS_CLASS[status] ?? 'badge-tag-gray'}`}>
            {EVENT_STATUS_LABEL[status] ?? status}
          </span>
        )}
        {attempt != null && <span className="badge badge-no-dot badge-tag-orange">第 {String(attempt)} 次尝试</span>}
        {noSideEffect && (
          <span className="badge badge-no-dot badge-tag-blue">未重复写入 ERP</span>
        )}
      </div>
      <div className="tl-time">
        {fmtDateTime(event.occurred_at)}
        {typeof duration === 'number' ? ` · 耗时 ${fmtDuration(duration)}` : ''} · 操作人 {event.actor}
      </div>
      {note && <div className="tl-result">{note}</div>}
      {showMessage && <div className="tl-result tl-result-raw">{message}</div>}
      {errorText && <div className="tl-result tl-result-error">错误：{errorText}</div>}
      <details className="tl-detail">
        <summary>查看事件详情</summary>
        <pre>{JSON.stringify(event.payload, null, 2)}</pre>
      </details>
    </div>
  );
}
