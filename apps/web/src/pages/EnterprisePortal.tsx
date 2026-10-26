import HotelMatrix, { HotelAuditRecords } from './HotelMatrix';
import ComplianceReport from '../components/ComplianceReport';
import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from 'react';
import { Link, Navigate, Route, Routes, useNavigate, useSearchParams } from 'react-router-dom';
import Layout from '../components/Layout';
import RecordId, { shortRecordId } from '../components/RecordId';
import { ADVISORY_STATUS_LABEL, recordMoney } from '../utils/recordPresentation';
import { RiskBadge } from '../components/Badges';
import { CheckItem, EmptyState, Metric } from '../components/ui';
import NewClaim from './NewClaim';
import Login from './Login';
import Policies from './Policies';
import TravelProfiles from '../components/TravelProfiles';
import { portalFile, portalHttp, HOTEL_DEMO_MODE, ROLE_LABEL, STAGE_LABEL, type PortalClaim, type PortalEvent, type Role, type User } from '../lib/api/enterprise';
import { useAuth } from '../lib/session/AuthContext';
import type { RiskLevel } from '../lib/api/enums';
import { CATEGORY_LABEL, fmtMoney, fmtPercent } from '../utils/format';

const EVENT_LABEL: Record<string, string> = {
  P4A_SUBMITTED: '提交住宿申请', P4A_RESUBMITTED: '修改后重新提交住宿申请',
  P4B_SUBMITTED: '提交报销申请', P4B_REAUDIT_REQUESTED: '补证后重新审核',
  EMPLOYEE_STAY_VERIFIED: '核实住宿信息', P4B_FACTS_VERIFIED: '核实报销事实',
  AI_ADVISORY_RECORDED: '记录 AI 审核建议', ADVISORY_MANUALLY_REVIEWED: '财务独立核查',
  TRAVEL_POLICY_PUBLISHED: '发布差旅标准', TRAVEL_POLICY_RETIRED: '退休差旅标准', TRAVEL_PROFILE_SET: '维护差旅职级',
  SUBMITTED: '提交报销', RESUBMITTED: '修改后重新提交', AI_AUDIT_COMPLETED: 'AI 审核完成',
  AI_AUDIT_INTERRUPTED: 'AI 审核中断', FINANCE_RETURN: '财务退回修改', FINANCE_APPROVE: '财务审核通过',
  FINANCE_REJECT: '财务审核不通过', RETRY: '发起恢复', USER_CREATED: '创建账号',
  USER_ENABLED: '启用账号', USER_DISABLED: '停用账号', LOGIN: '账号登录', POLICY_UPLOADED: '上传制度',
};
const EVENT_ROLE_LABEL: Record<string, string> = { ...ROLE_LABEL, SYSTEM: '系统服务' };
function dateText(value: string) {
  return new Date(value).toLocaleString('zh-CN', { hour12: false });
}
function errorText(error: unknown) { return error instanceof Error ? error.message : '操作失败，请重试'; }
function Badge({ stage }: { stage: string }) {
  return <span className={`badge badge-no-dot portal-status status-${stage.toLowerCase()}`}>{STAGE_LABEL[stage] ?? '处理中'}</span>;
}
function PortalTimeline({ events }: { events: PortalEvent[] }) {
  return events.length === 0 ? <EmptyState text="暂无过程记录" /> : <div className="timeline timeline-compact">
    {events.map((event, index) => <div className="tl-item" key={index}>
      <div className="tl-dot info" />
      <div className="tl-head"><span className="tl-step">{EVENT_LABEL[event.kind] ?? '流程处理'}</span></div>
      <div className="tl-time">{dateText(event.at)} · {event.actor_name} · {EVENT_ROLE_LABEL[event.role] ?? '角色待确认'}</div>
      {event.reason && <div className="tl-result">{event.reason}</div>}
    </div>)}
  </div>;
}
function Guard({ roles, children }: { roles?: Role[]; children: ReactNode }) {
  const { user, loading } = useAuth();
  if (loading) return <div className="portal-loading" role="status">正在确认登录状态…</div>;
  if (!user) return <Navigate to="/login" replace />;
  if (roles && !roles.includes(user.role)) return <Navigate to={user.role === 'EMPLOYEE' ? '/my-claims' : '/workbench'} replace />;
  return <>{children}</>;
}
function Home() {
  const { user, loading } = useAuth();
  return loading ? <div className="portal-loading">正在载入…</div> :
    <Navigate to={!user ? '/login' : user.role === 'EMPLOYEE' ? '/my-claims' : '/workbench'} replace />;
}

function ClaimInfo({ item, employee, onEdit, onBack, financeQueue, refresh }: {
  item: PortalClaim; employee: boolean; onEdit: () => void; onBack?: () => void; financeQueue?: boolean; refresh: () => Promise<void>;
}) {
  const { user } = useAuth();
  const [decision, setDecision] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [retryRequest, setRetryRequest] = useState<{ action: string; reason: string; expected_revision: number; idempotency_key: string; advisory_report_digest?: string } | null>(null);
  const [invoicePreview, setInvoicePreview] = useState('');
  useEffect(() => {
    let cancelled = false; let url = '';
    if (item.invoice_evidence) portalFile(`/claims/${item.claim_id}/invoice-preview`).then(blob => {
      url = URL.createObjectURL(blob); if (cancelled) URL.revokeObjectURL(url); else setInvoicePreview(url);
    }).catch(e => { if (!cancelled) setError(errorText(e)); });
    return () => { cancelled = true; if (url) URL.revokeObjectURL(url); };
  }, [item.claim_id, item.version, !!item.invoice_evidence]);
  async function downloadInvoice(version = item.version) {
    try {
      const blob = await portalFile(`/claims/${item.claim_id}/invoice-original?version=${version}`);
      const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url;
      link.download = item.history.find(v => v.version === version)?.invoice_evidence?.attachment.file_name ?? 'invoice';
      link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) { setError(errorText(e)); }
  }
  const ai = item.ai_result;
  const compliance = item.compliance_analysis;
  const advisory = item.advisory_analysis;
  const advisoryRisk = advisory && ['LOW', 'MEDIUM', 'HIGH'].includes(advisory.risk_level)
    ? advisory.risk_level as RiskLevel : null;
  const blocked = advisory ? advisory.deterministic_status === 'FAIL' ||
    (ai && (!ai.hard_gate_passed || ai.rule_results.some(r => r.outcome === 'FAIL' && r.severity === 'BLOCK'))) : (ai && (!ai.hard_gate_passed || ai.rule_results.some(r => r.outcome !== 'PASS'))) ||
    (compliance && (compliance.analysis_status !== 'SUCCEEDED' || compliance.compliance_status !== 'CHECK_PASSED' || compliance.missing_evidence.length > 0 || compliance.conflicts.length > 0));
  const currentDataAdvice = advisory?.analysis_basis === 'CURRENT_DATA_WITH_RAG';
  const manualReviewNeeded = advisory && ((!currentDataAdvice && advisory.deterministic_status === 'UNKNOWN') || advisory.ai_advisory_status !== 'SUCCEEDED');
  async function review(event: FormEvent) {
    event.preventDefault(); if (busy || !decision || !reason.trim()) return;
    setBusy(true); setError('');
    const request = retryRequest ?? { action: decision, reason: reason.trim(), expected_revision: item.revision, idempotency_key: crypto.randomUUID(),
      ...(advisory ? { advisory_report_digest: advisory.report_digest } : {}) };
    setRetryRequest(request);
    try { await portalHttp(`/claims/${item.claim_id}/finance-action`, request); setRetryRequest(null); setDecision(''); setReason(''); await refresh(); }
    catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  async function retry() {
    if (busy) return; setBusy(true); setError('');
    try { await portalHttp(`/claims/${item.claim_id}/retry`, { expected_revision: item.revision, idempotency_key: crypto.randomUUID() }); await refresh(); }
    catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  const last = item.history[item.history.length - 1];
  const financeDone = !!last?.finance_action;
  const financeReview = financeQueue && item.stage === 'FINANCE_REVIEW';
  return <section className={`card summary-card portal-detail${financeReview ? ' portal-finance-review' : ''}`} aria-label="报销进度与详情">
    {financeQueue && <div className="portal-review-toolbar">
      <button type="button" className="portal-back-button" onClick={onBack}>← 返回申请队列</button>
      <span>当前审核 <b>{item.fields.purpose || '报销申请'}</b></span>
    </div>}
    <div className={`portal-detail-body${financeReview ? ' is-finance-review' : ''}`}>
      <div className={`portal-detail-main${financeReview ? ' is-finance-review' : ''}`}>
    <div className="summary-head"><div className="portal-detail-heading">
      <div className="portal-eyebrow">报销进度</div><h2 className="summary-id">{item.fields.purpose}</h2>
      <div className="summary-badges"><Badge stage={item.stage} />{advisory ? <><span className="portal-ai-label">AI 建议风险</span>{advisoryRisk ? <RiskBadge level={advisoryRisk} /> : <span className="badge badge-pending">待确定</span>}</> : ai && <><span className="portal-ai-label">AI 风险</span><RiskBadge level={ai.risk_level} /></>}</div>
      <div className="summary-meta"><RecordId key={item.claim_id} value={item.claim_id} full copy /><span>第 <b>{item.version}</b> 次提交</span><span>申请人 <b>{item.fields.applicant_name}</b></span></div>
    </div><div className="portal-detail-amount"><Metric label="申报金额" value={fmtMoney(item.fields.amount)} note="人民币 · CNY" /></div></div>
    {item.reason && <div className={`portal-reason portal-detail-reason ${item.stage === 'APPROVED' ? 'reason-approved' : ''}`}><strong>{item.stage === 'RETURNED' ? '退回理由' : item.stage === 'REJECTED' ? '审核不通过理由' : '审核说明'}</strong><p>{item.reason}</p></div>}
    <ol className="portal-progress" aria-label="审核过程">
      <li className="done"><span>1</span><strong>已提交</strong><small>{dateText(last?.submitted_at ?? item.created_at)}</small></li>
      <li className={ai ? 'done' : 'current'}><span>2</span><strong>{advisory ? 'AI 建议已记录' : ai ? 'AI 审核完成' : 'AI 审核中'}</strong><small>{advisory ? `建议状态：${ADVISORY_STATUS_LABEL[advisory.ai_advisory_status] ?? '状态待确认'}，${financeDone ? '财务决定已记录' : '等待财务决定'}` : ai ? '已完成制度校验与风险分析' : '正在校验制度与费用信息'}</small></li>
      <li className={financeDone ? 'done' : ai ? 'current' : ''}><span>3</span><strong>{financeDone ? '财务已审核' : ai ? '财务审核中' : '财务审核'}</strong><small>{financeDone ? dateText(last.reviewed_at ?? item.created_at) : ai ? '等待财务人工确认' : 'AI 审核完成后进入'}</small></li>
      <li className={financeDone ? 'current' : ''}><span>4</span><strong>{financeDone ? STAGE_LABEL[item.stage] : '审核结果'}</strong><small>{item.stage === 'RETURNED' ? '修改后重新提交' : item.stage === 'APPROVED' ? '已完成入账同步' : item.stage === 'ERP_SYNC_PENDING' ? '财务已通过，等待入账恢复' : '通过、退回修改或不通过'}</small></li>
    </ol>
    <dl className="portal-facts"><div><dt>申请人</dt><dd>{item.fields.applicant_name} · {item.fields.department}</dd></div><div><dt>费用类型</dt><dd>{item.travel_details?.expense_type === 'ALLOWANCE' ? '差旅补助' : CATEGORY_LABEL[item.fields.category]}</dd></div>
      <div><dt>报销金额</dt><dd className="portal-amount">¥ {item.fields.amount}</dd></div><div><dt>费用发生日期</dt><dd>{item.fields.incurred_on}</dd></div><div><dt>城市 / 行程</dt><dd>{item.fields.city} · {item.fields.trip_start} 至 {item.fields.trip_end}</dd></div><div><dt>报销事项</dt><dd>{item.fields.purpose}</dd></div></dl>
    {ai && !advisory && <div className="portal-ai-summary"><h3>AI 与制度校验</h3><p>审核建议：{ai.decision === 'APPROVE' ? '建议通过' : ai.decision === 'REJECT' ? '建议不通过' : '需要人工核实'} · 财务拥有最终审核权。</p>
      {blocked && <div className="alert alert-danger">此申请存在硬性阻断、不符合标准或待核实项，无法批准；可退回补充或审核不通过。</div>}
      {item.ai_analysis && <div className="portal-reason"><strong>{item.ai_analysis.provider === 'mock' ? '模拟 AI 分析' : 'AI 分析'} · {item.ai_analysis.model}</strong><p>{item.ai_analysis.summary}</p></div>}
      {item.employee_snapshot && <p>适用档案：{item.employee_snapshot.department} · {item.employee_snapshot.position} · {item.employee_snapshot.grade === 'UNKNOWN' ? '职级未确认' : item.employee_snapshot.grade + '级'}</p>}
      <div className="check-list">{ai.rule_results.map(rule => <CheckItem key={rule.rule_id}
        state={rule.outcome === 'PASS' ? 'pass' : rule.outcome === 'FAIL' ? 'fail' : 'warn'}
        label={rule.outcome === 'PASS' ? '符合' : rule.outcome === 'FAIL' ? '不符合' : '待核实'}
        detail={<>{rule.rule_id === 'TRAVEL_STANDARD' && rule.outcome === 'UNKNOWN' ? item.travel_standard_diagnosis ?? rule.reason : rule.reason}{rule.actual && rule.limit ? `（申报 ${rule.actual} 元 / 额度 ${rule.limit} 元）` : ''}</>} />)}</div>
      {ai.policy_evidence.length > 0 && <details><summary>查看审核依据</summary>{ai.policy_evidence.map(e => <blockquote key={e.chunk_id}>{e.quote}<small>制度版本 {e.version} · 生效 {e.effective_from}</small></blockquote>)}</details>}
    </div>}
    <ComplianceReport key={`${item.claim_id}:${item.version}`} item={item} finance={user?.role === 'FINANCE'} refresh={refresh} />
    {item.invoice_evidence && <details id="portal-invoice-source" className="portal-history portal-invoice-details"><summary>发票原件、确认与核实提示</summary><p>{item.invoice_evidence.attachment.file_name} · 已本人确认，未做税务真伪核验。</p>
      {invoicePreview && <img className="invoice-preview" src={invoicePreview} alt="当前提交发票第一页" />}<p>预览第一页，请下载原件核对所有页面。</p>
      <button className="btn btn-sm" onClick={() => void downloadInvoice()}>下载发票原件</button>
      <p>确认金额 {item.invoice_evidence.confirmed_fields.total_amount ?? '未确定'} 元 · 销售方 {item.invoice_evidence.confirmed_fields.seller_name}</p>
      {item.invoice_evidence.warnings.map(w => <p key={w.code}>{w.message}</p>)}<p>{item.invoice_evidence.discrepancy_reason}</p></details>}
    {employee && item.stage === 'RETURNED' && (item.compliance_analysis && !item.p4a
      ? <p className="portal-reason">请向财务补充业务凭据，由财务记录核实并重审。申报金额或事项改变时请另建申请；原申请和退回记录保留。</p>
      : <button className="btn btn-primary" onClick={() => { if (item.p4a) window.location.assign(`/hotel-matrix?edit=${encodeURIComponent(item.claim_id)}`); else onEdit(); }}>修改并重新提交</button>)}
    {item.stage === 'SYSTEM_ERROR' && financeDone && <p className="alert alert-danger">财务决定已记录，但同步执行中断，请联系财务排查；申请不能重新修改。</p>}
    {((employee && item.stage === 'SYSTEM_ERROR' && !financeDone) || (user?.role === 'FINANCE' && ['ERP_SYNC_PENDING', 'FINANCE_APPLYING'].includes(item.stage))) && <button className="btn" disabled={busy} onClick={retry}>{busy ? '恢复中…' : item.stage === 'SYSTEM_ERROR' ? '重试 AI 审核' : item.stage === 'FINANCE_APPLYING' ? '继续已提交的审核决定' : '重试入账同步'}</button>}
    {error && <div className={`alert alert-danger${financeReview ? ' portal-review-error' : ''}`} role="alert">{error}</div>}
    <details className="portal-history portal-submission-history"><summary>提交与审核历史（{item.history.length} 次提交）</summary>{item.history.map(version => <article key={version.version}><strong>第 {version.version} 次提交 · {dateText(version.submitted_at)}</strong><p>{version.fields.purpose} · ¥ {version.fields.amount}</p>
      <p>{version.finance_action === 'RETURN' ? '退回修改' : version.finance_action === 'APPROVE' ? '审核通过' : version.finance_action === 'REJECT' ? '审核不通过' : STAGE_LABEL[version.stage]}{version.finance_reason ? `：${version.finance_reason}` : ''}</p>
      {version.invoice_evidence && <><p>票据：{version.invoice_evidence.attachment.file_name} · 确认金额 {version.invoice_evidence.confirmed_fields.total_amount ?? '未确定'} 元</p><button className="btn btn-sm" onClick={() => void downloadInvoice(version.version)}>下载该版本发票</button></>}</article>)}</details>
    <details className="portal-history portal-process-history"><summary>过程记录</summary><PortalTimeline events={item.timeline} /></details>
      </div>
      {financeReview && <aside className="portal-finance-aside" aria-label="财务决定">
        <div className={`portal-review-gate${blocked ? ' is-blocked' : ' is-ready'}`}>
          <strong>{blocked ? '当前申请存在制度阻断' : manualReviewNeeded ? '可由财务人工核实后决定' : '请选择处理方式并填写原因'}</strong>
          <p>{blocked ? '存在硬性阻断或明确不符合制度，可选择退回或不通过。' : currentDataAdvice ? '参考现有申报、发票和 AI 建议，自行决定是否核实、通过、不通过或退回，并填写原因。' : manualReviewNeeded ? '系统校验尚有待核实项或 AI 建议不完整。请核对原始材料；选择通过时，在决定原因中填写人工核实依据。' : '参考 AI 建议和原始材料，在下方一次提交财务决定。'}</p>
        </div>

        <form onSubmit={review} className="portal-finance-form"><h3>财务决定</h3>
          <label className="label" htmlFor="finance-decision">处理方式</label><select className="select" id="finance-decision" required value={decision} disabled={busy || !!retryRequest} onChange={e => setDecision(e.target.value)}>
            <option value="">请选择处理方式</option><option value="APPROVE" disabled={!!blocked}>审核通过</option><option value="RETURN">退回修改</option><option value="REJECT">审核不通过</option></select>
          <label className="label" htmlFor="finance-reason">决定原因 *</label><textarea className="textarea" id="finance-reason" required maxLength={2000} value={reason} disabled={busy || !!retryRequest} onChange={e => setReason(e.target.value)} placeholder={manualReviewNeeded && decision === 'APPROVE' ? '请填写人工核对的材料、核实依据及通过理由，员工可见。' : '请填写通过、不通过或退回的原因，员工可见。'} />
          <button className="btn btn-primary" disabled={busy || !decision || !reason.trim()}>{busy ? '正在处理…' : retryRequest ? '重试同一审核操作' : '确认审核决定'}</button>
          {retryRequest && !busy && <button className="btn" type="button" onClick={() => { setRetryRequest(null); void refresh(); }}>刷新状态并重新核对</button>}
        </form>
        <p className="portal-finance-aside-note">AI 仅提供建议。财务决定须由有权限的审核人员提交；ERP 写回只会在明确批准后执行。</p>
      </aside>}
    </div>
  </section>;
}

function ClaimWorkspace({ employee = false, initialNew = false }: { employee?: boolean; initialNew?: boolean }) {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const requestedClaim = params.get('claim');
  const { user } = useAuth();
  const [items, setItems] = useState<PortalClaim[]>([]);
  const [selected, setSelected] = useState<PortalClaim | null>(null);
  const [tab, setTab] = useState('all');
  const [form, setForm] = useState<'new' | 'edit' | null>(initialNew ? 'new' : null);
  const [error, setError] = useState('');
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    setForm(initialNew ? 'new' : null);
    setSelected(null);
  }, [initialNew]);
  const refresh = useCallback(async () => {
    try {
      const data = await portalHttp<{ items: PortalClaim[] }>('/claims');
      setItems(data.items); setSelected(previous => previous ? data.items.find(item => item.claim_id === previous.claim_id) ?? null : null);
      setError(''); setLoaded(true);
    } catch (e) { setError(errorText(e)); }
  }, []);
  useEffect(() => { void refresh(); const timer = setInterval(() => { void refresh(); }, 3000); return () => clearInterval(timer); }, [refresh]);
  const tabs = [{ key: 'all', label: '全部' }, { key: 'processing', label: '处理中' }, { key: 'RETURNED', label: '已退回' }, { key: 'APPROVED', label: '已通过' }, { key: 'REJECTED', label: '未通过' }];
  function matches(item: PortalClaim, key: string) {
    return key === 'all' || key === 'processing' && ['AI_AUDITING', 'FINANCE_REVIEW', 'FINANCE_APPLYING', 'SYSTEM_ERROR'].includes(item.stage) ||
      key === 'APPROVED' && ['APPROVED', 'ERP_SYNC_PENDING'].includes(item.stage) || item.stage === key;
  }
  function closeForm() {
    setForm(null);
    if (initialNew) navigate('/my-claims', { replace: true });
  }
  async function saved(item: PortalClaim) {
    setForm(null); selectClaim(item); setTab('all'); await refresh();
    if (initialNew) navigate('/my-claims', { replace: true });
  }
  useEffect(() => {
    if (loaded) setSelected(requestedClaim ? items.find(item => item.claim_id === requestedClaim) ?? null : null);
  }, [requestedClaim, loaded, items]);
  function selectClaim(item: PortalClaim | null) {
    setSelected(item);
    setParams(previous => { const next = new URLSearchParams(previous); if (item) next.set('claim', item.claim_id); else next.delete('claim'); return next; });
  }
  const title = employee ? '我的报销' : user?.role === 'FINANCE' ? '财务审核' : '企业报销';
  const financeQueue = !employee && user?.role === 'FINANCE';
  return <Layout title={title}><div className="page-header"><div><h1 className="page-title">{title}</h1><p className="page-desc">{employee ? HOTEL_DEMO_MODE ? '申请住宿费、查看审核进度，或修改被退回的申请。' : '新建报销、查看审核进度，或修改被退回的申请。' : '查看本企业报销与审核依据，所有处理均保留记录。'}</p></div>
    {employee && <button className="btn btn-primary" onClick={() => { if (HOTEL_DEMO_MODE) { navigate('/hotel-matrix'); return; } if (initialNew && form === 'new') { closeForm(); return; } setForm(form === 'new' ? null : 'new'); selectClaim(null); }}>{HOTEL_DEMO_MODE ? '+ 申请住宿费' : form === 'new' ? '收起表单' : '+ 新建报销'}</button>}</div>
    {error && <div role="alert" className="alert alert-danger">{error}</div>}
    {form && user && <section className="portal-new-form"><button className="btn" onClick={closeForm}>返回我的报销</button><NewClaim key={form === 'edit' ? `${selected?.claim_id}-${selected?.revision}` : 'new'} portalUser={user} existing={form === 'edit' ? selected ?? undefined : undefined} onSaved={item => void saved(item)} /></section>}
    {!form && <>{(!financeQueue || !selected) && <div className="portal-tabs" role="tablist" aria-label="报销状态筛选">{tabs.map(t => <button key={t.key} role="tab" aria-selected={tab === t.key} onClick={() => setTab(t.key)}>{t.label}<span>{items.filter(item => matches(item, t.key)).length}</span></button>)}</div>}
      {(!financeQueue || !selected) && <section className="card portal-claim-list"><div className="list-toolbar"><h2 className="portal-list-heading">{employee ? '我的申请' : '企业申请'}</h2><div className="list-toolbar-meta">当前显示 <b>{items.filter(item => matches(item, tab)).length}</b> 条 · 共 <b>{items.length}</b> 条<button className="btn btn-sm" onClick={() => void refresh()}>刷新</button></div></div>
        {!loaded ? <p role="status">正在读取报销…</p> : !items.filter(item => matches(item, tab)).length ? <EmptyState text={items.length ? '此状态下暂无报销' : '暂无报销申请'} hint={employee ? HOTEL_DEMO_MODE ? '点击“申请住宿费”，提交第一笔住宿申请。' : '点击“新建报销”，提交第一笔申请。' : '员工提交后，会在这里显示。'} /> : <div className="table-wrap portal-table-wrap"><table className="table table-dense portal-table"><thead><tr><th className="record-sequence" scope="col">序号</th><th>报销事项</th>{!employee && <th>申请人</th>}<th className="cell-num">金额</th><th>进度与说明</th><th className="col-actions">操作</th></tr></thead><tbody>{items.filter(item => matches(item, tab)).map((item, index) => <tr key={item.claim_id} className={selected?.claim_id === item.claim_id ? 'selected' : ''}><td className="record-sequence">{index + 1}</td><td><strong>{item.fields.purpose || '报销申请'}</strong><small>{item.travel_details?.expense_type === 'ALLOWANCE' ? '差旅补助' : CATEGORY_LABEL[item.fields.category]} · {item.fields.city} · {item.fields.incurred_on}</small><RecordId value={item.claim_id} /></td>{!employee && <td>{item.fields.applicant_name}<small>{item.fields.department}</small></td>}<td className="cell-money cell-num">{recordMoney(item.fields.amount)}</td><td><Badge stage={item.stage} />{item.reason && <p className="portal-row-reason">{item.reason}</p>}</td><td className="col-actions"><button className="btn btn-sm" onClick={() => selectClaim(item)}>{item.stage === 'FINANCE_REVIEW' && user?.role === 'FINANCE' ? '查看并审核' : '查看进度'}</button></td></tr>)}</tbody></table></div>}
      </section>}{selected && <ClaimInfo key={`${selected.claim_id}-${selected.version}`} item={selected} employee={employee} financeQueue={financeQueue} onBack={() => selectClaim(null)} onEdit={() => setForm('edit')} refresh={refresh} />}</>}
  </Layout>;
}

function Workbench() {
  const { user } = useAuth();
  const [metrics, setMetrics] = useState<{ total: number; by_stage: Record<string, number> } | null>(null);
  const [error, setError] = useState('');
  useEffect(() => { portalHttp<{ total: number; by_stage: Record<string, number> }>('/dashboard').then(setMetrics).catch(e => setError(errorText(e))); }, []);
  const cards = [
    { label: '报销单总量', value: metrics?.total, note: '本企业报销申请', tone: '' },
    { label: '待财务审核', value: metrics ? metrics.by_stage.FINANCE_REVIEW + metrics.by_stage.FINANCE_APPLYING : undefined, note: '等待或正在处理人工决定', tone: 'kpi-purple' },
    { label: '待员工修改', value: metrics?.by_stage.RETURNED, note: '财务已退回，等待重新提交', tone: 'kpi-orange' },
    { label: '入账待恢复', value: metrics?.by_stage.ERP_SYNC_PENDING, note: '财务已通过，等待同步恢复', tone: 'kpi-orange' },
  ];
  return <Layout title="企业工作台"><div className="page-header"><div><h1 className="page-title">企业工作台</h1><p className="page-desc">{user?.company_id} · 报销处理与制度治理</p></div><div className="page-actions"><Link className="btn btn-primary" to="/company/claims">{user?.role === 'FINANCE' ? '处理待审核报销' : '查看企业报销'}</Link>{user?.role === 'ADMIN' && <Link className="btn" to="/policies">上传与发布制度</Link>}</div></div>
    {error && <div className="alert alert-danger">{error}</div>}
    <div className="kpi-grid portal-metrics">{cards.map(metric => <article className={`kpi-card ${metric.tone}`} key={metric.label}><div className="kpi-label">{metric.label}</div><div className="kpi-value">{metric.value ?? '—'}</div><div className="kpi-meta">{metric.note}</div></article>)}</div>
    {metrics && <section className="card"><div className="card-title"><span>单据流转分布</span><span className="card-title-extra">合计 {metrics.total} 张</span></div>
      <div className="dist-grid portal-stage-distribution">{Object.entries(metrics.by_stage).map(([stage, count]) => {
        const percent = metrics.total > 0 ? count / metrics.total : 0;
        const tone = stage === 'APPROVED' ? 'tone-green' : stage === 'REJECTED' || stage === 'SYSTEM_ERROR' ? 'tone-red' : stage === 'RETURNED' || stage === 'ERP_SYNC_PENDING' ? 'tone-orange' : 'tone-blue';
        return <div className={`dist-row${count === 0 ? ' is-zero' : ''}`} key={stage}><span className="dist-name">{stage === 'FINANCE_APPLYING' ? '财务决定处理中' : STAGE_LABEL[stage] ?? stage}</span><span className="dist-bar" aria-hidden="true"><span className={`dist-fill ${tone}`} style={{ width: `${percent * 100}%` }} /></span><span className="dist-count">{count}</span><span className="dist-pct">{fmtPercent(percent)}</span></div>;
      })}</div><p className="section-note">数量来自本企业报销状态统计，占比为该状态数量除以本企业报销单总量。</p>
    </section>}
    <section className="card portal-workbench-actions"><h2>工作入口</h2><Link className="btn btn-primary" to="/company/claims">{user?.role === 'FINANCE' ? '处理待审核报销' : '查看企业报销'}</Link>{user?.role === 'ADMIN' && <><Link className="btn" to="/policies">上传与发布制度</Link><Link className="btn" to="/accounts">管理企业账号</Link></>}<Link className="btn" to="/company/audit">查看审计记录</Link></section>
    <section className="card portal-capabilities"><h2>审核责任</h2><p>AI 提供风险分析与制度依据，财务负责通过、退回或不通过。硬性条件与额度阻断不能被批准。</p><p>当前已实现住宿按城市、日期和每晚额度校验。其他制度条款提供原文检索，须人工判断，尚未自动执行完整企业制度。</p></section>
  </Layout>;
}

function Accounts() {
  const { user } = useAuth();
  const [items, setItems] = useState<User[]>([]);
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [fields, setFields] = useState({ username: '', password: '', name: '', department: '', position: '', role: 'EMPLOYEE' as Role });
  const refresh = useCallback(async () => { const data = await portalHttp<{ items: User[] }>('/users'); setItems(data.items); }, []);
  useEffect(() => { void refresh().catch(e => setError(errorText(e))); }, [refresh]);
  async function submit(event: FormEvent) {
    event.preventDefault(); if (busy) return; setBusy(true); setError(''); setNotice('');
    try { await portalHttp('/users', fields); setFields({ username: '', password: '', name: '', department: '', position: '', role: 'EMPLOYEE' }); setShow(false); await refresh(); setNotice('账号已创建，请通过企业授权渠道交给该员工本人。'); }
    catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  async function toggle(item: User) {
    if (busy) return; setBusy(true); setError('');
    try { await portalHttp(`/users/${item.id}/enabled`, { enabled: !item.enabled }); await refresh(); }
    catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  return <Layout title="账号管理"><div className="page-header"><div><h1 className="page-title">企业账号管理</h1><p className="page-desc">每人独立账号，员工、财务和管理员各有权限。</p></div><button className="btn btn-primary" onClick={() => setShow(!show)}>{show ? '取消新建' : '+ 新建账号'}</button></div>
    {error && <div className="alert alert-danger" role="alert">{error}</div>}{notice && <div className="alert alert-success" role="status">{notice}</div>}
    <TravelProfiles />
    {show && <form onSubmit={submit} className="card portal-account-form"><h2>新建企业账号</h2><p>账号所属企业：{user?.company_id}</p><div className="submission-fields">{([['username', '登录账号'], ['name', '姓名'], ['department', '部门'], ['position', '职位'], ['password', '初始密码（至少 12 个字符）']] as const).map(([key, label]) => <div className="field" key={key}><label className="label" htmlFor={`user-${key}`}>{label}</label><input className="input" id={`user-${key}`} type={key === 'password' ? 'password' : 'text'} required minLength={key === 'password' ? 12 : key === 'username' ? 3 : 1} maxLength={key === 'username' || key === 'name' ? 100 : 128} autoComplete={key === 'password' ? 'new-password' : 'off'} value={fields[key]} disabled={busy} onChange={e => setFields(current => ({ ...current, [key]: e.target.value }))} /></div>)}
      <div className="field"><label className="label" htmlFor="user-role">账号角色</label><select id="user-role" className="select" value={fields.role} disabled={busy} onChange={e => setFields(current => ({ ...current, role: e.target.value as Role }))}>{(['EMPLOYEE', 'FINANCE', 'ADMIN'] as const).map(role => <option key={role} value={role}>{ROLE_LABEL[role]}</option>)}</select></div></div><button className="btn btn-primary" disabled={busy}>{busy ? '创建中…' : '创建账号'}</button></form>}
    <section className="card"><div className="table-wrap portal-table-wrap"><table className="table table-dense portal-table"><thead><tr><th>姓名 / 账号</th><th>部门 / 职位</th><th>角色</th><th>状态</th><th className="col-actions">操作</th></tr></thead><tbody>{items.map(item => <tr key={item.id}><td>{item.name}<small>{item.username}</small></td><td>{item.department}<small>{item.position}</small></td><td>{ROLE_LABEL[item.role]}</td><td>{item.enabled ? '已启用' : '已停用'}</td><td className="col-actions"><button className="btn btn-sm" disabled={busy || item.id === user?.id} onClick={() => void toggle(item)}>{item.enabled ? '停用账号' : '启用账号'}</button></td></tr>)}</tbody></table></div><p className="page-desc">停用后，已有登录会话立即失效；报销与审计记录保留。</p></section>
  </Layout>;
}

function CompanyAudit() {
  const { user } = useAuth();
  const [data, setData] = useState<{ events: PortalEvent[]; claims: { claim_id: string; events: { event_type: string; occurred_at: string; payload: { summary?: string } }[] }[] } | null>(null);
  const [error, setError] = useState('');
  const [summaryError, setSummaryError] = useState('');
  const [reload, setReload] = useState(0);
  const [summaries, setSummaries] = useState<Map<string, PortalClaim>>(new Map());
  useEffect(() => {
    let active = true;
    setData(null); setError(''); setSummaryError(''); setSummaries(new Map());
    Promise.allSettled([portalHttp<NonNullable<typeof data>>('/audit'), portalHttp<{ items: PortalClaim[] }>('/claims')])
      .then(([audit, claims]) => {
        if (!active) return;
        if (audit.status === 'fulfilled') setData(audit.value); else setError(errorText(audit.reason));
        if (claims.status === 'fulfilled') setSummaries(new Map(claims.value.items.map(item => [item.claim_id, item])));
        else setSummaryError('申请摘要暂未取得，仍可通过单号核对原始审计记录。');
      });
    return () => { active = false; };
  }, [user?.id, reload]);
  return <Layout title="审计记录">
    <div className="page-header"><div><h1 className="page-title">企业审计记录</h1><p className="page-desc">本企业账号、报销流程与制度管理记录。</p></div></div>
    {error && <div className="alert alert-danger" role="alert">{error}<button className="btn btn-sm" onClick={() => setReload(value => value + 1)}>重新读取</button></div>}
    {summaryError && <p role="status">{summaryError}<button className="link-btn" onClick={() => setReload(value => value + 1)}>重试</button></p>}
    {data && <div className="card summary-card">
      <div className="summary-head"><div><div className="summary-id">{user?.company_id} · 审计概览</div><div className="summary-meta">核对操作时间、人员和原始说明，查看每笔报销的工作流记录。</div></div></div>
      <div className="summary-metrics audit-metrics">
        <Metric label="人员与流程记录" value={data.events.length} note="当前接口返回的企业事件" />
        <Metric label="有关联记录的报销" value={data.claims.length} note="当前返回的单据数量" />
        <Metric label="工作流事件" value={data.claims.reduce((sum, claim) => sum + claim.events.length, 0)} note="当前单据事件条数之和" />
      </div>
    </div>}
    <section className="card">
      <div className="card-title"><span>人员与流程记录</span><span className="card-title-extra">{data ? `${data.events.length} 条` : '正在读取…'}</span></div>
      {!data ? !error && <p role="status">正在读取…</p> : data.events.length === 0 ? <EmptyState text="暂无企业操作记录" /> :
        <div className="table-wrap portal-table-wrap record-audit-events" role="region" aria-label="人员与流程操作记录" tabIndex={0}><table className="table table-dense portal-table">
          <thead><tr><th className="record-sequence" scope="col">序号</th><th>时间</th><th>操作</th><th>操作人</th><th>说明</th></tr></thead>
          <tbody>{[...data.events].reverse().map((event, index) => <tr key={index}>
            <td className="record-sequence">{index + 1}</td><td>{dateText(event.at)}</td><td>{EVENT_LABEL[event.kind] ?? '业务处理'}</td><td>{event.actor_name} · {EVENT_ROLE_LABEL[event.role] ?? '角色待确认'}</td><td>{event.reason || (event.claim_id ? summaries.get(event.claim_id)?.fields.purpose || '报销流程处理' : '账号或制度管理')}{event.claim_id && <RecordId value={event.claim_id} />}</td>
          </tr>)}</tbody>
        </table></div>}
    </section>
    {data?.claims.map((claim, index) => <details className="card portal-history" key={claim.claim_id}>
      <summary><span className="record-audit-summary"><strong>{index + 1}. {summaries.get(claim.claim_id)?.fields.purpose || '报销申请'}</strong><span className="record-audit-meta">{summaries.get(claim.claim_id)?.fields.applicant_name || '申请人待取得'} · {summaries.get(claim.claim_id)?.fields.city || '城市待取得'} · {recordMoney(summaries.get(claim.claim_id)?.fields.amount)}</span><span className="record-audit-meta">{shortRecordId(claim.claim_id)} · {claim.events.length} 条工作流记录</span></span></summary>
      <RecordId value={claim.claim_id} full copy />
      <div className="timeline timeline-compact">{claim.events.map((event, index) => <div className="tl-item" key={index}>
        <div className="tl-dot info" /><div className="tl-head"><span className="tl-step">{event.payload.summary ?? '工作流处理'}</span></div><div className="tl-time">{dateText(event.occurred_at)}</div>
      </div>)}</div>
    </details>)}
  </Layout>;
}

export default function EnterpriseRoutes() {
  return <Routes>{import.meta.env.VITE_P4A_ENABLED === 'true' && <><Route path="/hotel-matrix" element={<Guard><HotelMatrix /></Guard>} /><Route path="/hotel-records" element={<Guard><HotelAuditRecords /></Guard>} /></>}<Route path="/login" element={<Login />} /><Route path="/my-claims" element={<Guard roles={['EMPLOYEE']}><ClaimWorkspace employee /></Guard>} /><Route path="/my-claims/new" element={<Guard roles={['EMPLOYEE']}>{HOTEL_DEMO_MODE ? <Navigate to="/hotel-matrix" replace /> : <ClaimWorkspace employee initialNew />}</Guard>} />
    <Route path="/workbench" element={<Guard roles={['FINANCE', 'ADMIN']}><Workbench /></Guard>} />
    <Route path="/company/claims" element={<Guard roles={['FINANCE', 'ADMIN']}><ClaimWorkspace /></Guard>} />
    <Route path="/company/audit" element={<Guard roles={['FINANCE', 'ADMIN']}><CompanyAudit /></Guard>} />
    <Route path="/policies" element={<Guard roles={['ADMIN', 'FINANCE']}><Policies /></Guard>} />
    <Route path="/accounts" element={<Guard roles={['ADMIN']}><Accounts /></Guard>} />
    <Route path="*" element={<Home />} /></Routes>;
}
