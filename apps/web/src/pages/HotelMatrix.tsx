import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import Layout from '../components/Layout';
import './HotelMatrix.css';
import InvoicePanel from '../components/InvoicePanel';
import HotelPolicyPublication from '../components/HotelPolicyPublication';
import { portalHttp, storageKey, STAGE_LABEL, type User, type PortalClaim } from '../lib/api/enterprise';
import { type InvoiceConfirmation } from '../lib/api/invoices';
import { ApiError } from '../lib/api/types';
import { type SubmissionFields } from '../lib/api/submission';
import { useAuth } from '../lib/session/AuthContext';
import { DECISION_LABEL } from '../utils/format';
import RecordId from '../components/RecordId';
import { lodgingCheck, recordMoney } from '../utils/recordPresentation';

const api = <T,>(path: string, body?: unknown) => portalHttp<T>(`/p4a/v1${path}`, body);
type Fact = { id: string; kind: 'EMPLOYEE' | 'CITY' | 'STAY'; key: string; value: Record<string, string | string[]>; effective_from: string; effective_to: string; source: string; version: string };
type Report = { employee_grade: string; city: string; city_tier: string; policy_version_id: string | null; limit_per_night: string | null; nights: number; total_limit: string | null; claimed_amount: string; exceeded_amount: string | null; decision: string; reason: string; source_reference: { title?: string; version?: string; source_section?: string; source_table?: number; source_row?: number; source_column?: number; file_sha256?: string; original_cell?: string } | null };
type Version = { policy_version_id: string; version: string; region: string; effective_from: string; effective_to: string | null; rules?: { rule_type?: string } };
const errorText = (e: unknown) => e instanceof Error ? e.message : '操作失败，请重试';

function Result({ cid, onClose }: { cid: string; onClose: () => void }) {
  const [report, setReport] = useState<Report | null>(null);
  const [claim, setClaim] = useState<PortalClaim | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const heading = useRef<HTMLHeadingElement>(null);
  async function refresh() {
    setLoading(true); setError('');
    try {
      const [r, detail] = await Promise.all([
        api<{ rule_result: Report | null }>(`/claims/${cid}`), portalHttp<PortalClaim>(`/claims/${cid}`),
      ]);
      setReport(r.rule_result); setClaim(detail);
    } catch (e) { setError(errorText(e)); }
    finally { setLoading(false); }
  }
  useEffect(() => { void refresh(); }, [cid]);
  useEffect(() => { heading.current?.focus(); }, [cid]);
  const source = report?.source_reference;
  const { user } = useAuth();
  return <section className="card hotel-record-detail" id="hotel-record-detail" aria-labelledby="hotel-detail-heading">
    <div className="hotel-detail-heading"><div><h2 id="hotel-detail-heading" ref={heading} tabIndex={-1}>住宿申请详情</h2><RecordId key={cid} value={cid} full copy /></div><button type="button" className="btn btn-sm" onClick={onClose}>收起详情</button></div>
    <button className="btn btn-sm" disabled={loading} onClick={() => void refresh()}>{loading ? '正在读取…' : '刷新审核进度'}</button>
    {error ? <p role="alert">{error}</p> : loading ? <p role="status">正在读取住宿申请详情…</p> : <>
    {claim && <p className="hotel-detail-summary"><strong>{claim.fields.purpose || '住宿申请'}</strong> · {claim.fields.trip_start || '—'} 入住 → {claim.fields.trip_end || '—'} 退房</p>}
    {!report ? <p>规则检查尚未完成，当前不能视为批准。</p> : <>
      <dl className="portal-facts">{Object.entries({ 员工: claim?.fields.applicant_name, 职级: report.employee_grade, 住宿城市: report.city, 城市等级: report.city_tier, 制度版本: source?.version ?? report.policy_version_id, 每晚限额: recordMoney(report.limit_per_night), 住宿晚数: `${report.nights} 晚`, 总限额: recordMoney(report.total_limit), 申请金额: recordMoney(report.claimed_amount), 超出金额: recordMoney(report.exceeded_amount), 住宿额度检查: lodgingCheck(report).label }).map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v === 'UNKNOWN' ? '待核实' : v ?? '—'}</dd></div>)}</dl>
      <p>{report.reason === 'STAY_UNVERIFIED' ? '住宿申报待财务核实，核实后完成金额检查。' : report.reason}</p>{source && <blockquote>{source.title} · {source.version} · {source.source_section}<br />表 {source.source_table ?? '历史条款'}，行 {source.source_row ?? '—'}，列 {source.source_column ?? '—'}；原始单元格 {source.original_cell ?? '见历史制度'}<br />文件 SHA-256：{source.file_sha256 ?? '历史制度快照'}</blockquote>}
    </>}
      <p>审批进度：{claim ? STAGE_LABEL[claim.stage] ?? '状态待确认' : '—'}。住宿额度检查通过后，仍须核对票据、硬性条件、风险及财务审批。</p>
      {claim?.reason && <p>财务说明：{claim.reason}</p>}
      {claim?.ai_result && <p>AI 建议：{DECISION_LABEL[claim.ai_result.decision]}；{claim.ai_result.reason}</p>}
      {claim && <Link to={`${user?.role === 'EMPLOYEE' ? '/my-claims' : '/company/claims'}?claim=${encodeURIComponent(cid)}`}>查看完整报销流程与审核记录 →</Link>}
    </>}
  </section>;
}

function Apply({ user, facts, saved, existing, originalStay, originalAddress }: {
  user: User; facts: Fact[]; saved: (cid: string) => void; existing?: PortalClaim;
  originalStay?: Fact | null; originalAddress?: string;
}) {
  const [cityName, setCityName] = useState(existing?.fields.city ?? '');
  const [address, setAddress] = useState(originalAddress ?? String(originalStay?.value.address ?? ''));
  const [start, setStart] = useState(existing?.fields.trip_start ?? '');
  const [end, setEnd] = useState(existing?.fields.trip_end ?? '');
  const [amount, setAmount] = useState(existing?.fields.amount ?? '');
  const [purpose, setPurpose] = useState(existing?.fields.purpose ?? '');
  const [notes, setNotes] = useState(existing?.fields.notes ?? '');
  const [seller, setSeller] = useState(existing?.fields.seller ?? '');
  const [invoice, setInvoice] = useState<InvoiceConfirmation | null>(null);
  const [attached, setAttached] = useState(false);
  const [invoiceBusy, setInvoiceBusy] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const nights = start && end ? Math.round((Date.parse(end) - Date.parse(start)) / 86400000) : 0;
  const validDates = Number.isFinite(nights) && nights >= 1 && nights <= 365;
  const cities = facts.filter(f => f.kind === 'CITY' &&
    [String(f.value.name), ...(Array.isArray(f.value.aliases) ? f.value.aliases : [])].includes(cityName.trim()) &&
    f.effective_from <= start && f.effective_to > start);
  const city = cities.length === 1 ? cities[0] : undefined;
  const grade = facts.find(f => f.kind === 'EMPLOYEE' && f.key === user.id && f.effective_from <= start && f.effective_to > start);
  const fields = useMemo<SubmissionFields>(() => ({ applicant_name: user.name, department: user.department,
    position: user.position, category: 'HOTEL', city: cityName.trim(), trip_start: start, trip_end: end,
    incurred_on: start, amount, nights: validDates ? nights : 1, purpose, seller, notes,
  }), [user, cityName, start, end, amount, nights, validDates, purpose, seller, notes]);
  const confirmed = useCallback((value: InvoiceConfirmation | null) => setInvoice(value), []);
  async function submit(event: FormEvent) {
    event.preventDefault(); if (busy || invoiceBusy) return;
    if (!validDates) { setError('退房日期须晚于入住日期，最多支持 365 晚。'); return; }
    if (attached && !invoice) { setError('请先核对并确认发票。'); return; }
    setBusy(true); setError('');
    const key = storageKey(`employee-hotel-pending-${existing?.claim_id ?? 'new'}-${existing?.revision ?? 0}`);
    try {
      const pending = sessionStorage.getItem(key);
      const request = pending ? JSON.parse(pending) : { city: cityName.trim(), address: address.trim(),
        check_in: start, check_out: end, amount, purpose: purpose.trim(), seller: seller.trim(), notes,
        invoice: invoice ? { draft_id: invoice.draft_id, attachment_id: invoice.attachment_id, confirmed: true,
          confirmed_fields: invoice.confirmed_fields, discrepancy_reason: invoice.discrepancy_reason } : null,
        idempotency_key: crypto.randomUUID(), ...(existing ? { expected_revision: existing.revision } : {}) };
      sessionStorage.setItem(key, JSON.stringify(request));
      const r = await portalHttp<{ claim_id: string }>(existing ? `/p4a/v2/claims/${existing.claim_id}/resubmit` : '/p4a/v2/claims', request);
      sessionStorage.removeItem(key); saved(r.claim_id);
    } catch (e) { if (e instanceof ApiError && e.status >= 400 && e.status < 500) sessionStorage.removeItem(key); setError(errorText(e)); }
    finally { setBusy(false); }
  }
  return <section className="card"><h2>{existing ? '修改并重新提交住宿费' : '提交住宿费'}</h2>
    {existing && <div className="portal-reason" role="note"><strong>退回理由</strong><p>{existing.reason || '请按财务要求核对并补充材料。'}</p><small>原申请 {existing.claim_id} · 修改后保留原编号，新增提交版本并重新审核。</small></div>}
    <form className="submission-form" onSubmit={submit}>
      <p>员工：{user.name} · 部门：{user.department} · 职位：{user.position}</p>
      <p>可先上传发票回填金额、酒店名称、发票号码和开票日期，再手动填写实际住宿城市／区县、地址、出差事由、备注及入住／退房日期。提交后由财务核实。</p>
      <InvoicePanel p4a retained={existing?.invoice_draft && existing.invoice_evidence ? { draft: existing.invoice_draft, evidence: existing.invoice_evidence } : undefined} fields={fields} disabled={busy} suggest={s => { if (s.amount !== undefined) setAmount(s.amount); if (s.seller !== undefined) setSeller(s.seller); }} onConfirmation={confirmed} onAttached={setAttached} onBusy={setInvoiceBusy}>
      <section className="hotel-manual-fields" aria-label="手动填写实际住宿信息">
        <div className="card-title"><span className="step-no">2</span>手动填写实际住宿信息</div>
      <div className="submission-fields">
        <div className="field"><label className="label" htmlFor="hotel-city">实际住宿城市／区县</label><input className="input" id="hotel-city" required maxLength={128} placeholder="例如：深圳" value={cityName} onChange={e => setCityName(e.target.value)} disabled={busy || invoiceBusy} /></div>
        <div className="field"><label className="label" htmlFor="hotel-address">实际住宿地址</label><input className="input" id="hotel-address" required maxLength={2000} placeholder="填写酒店实际所在地址" value={address} onChange={e => setAddress(e.target.value)} disabled={busy || invoiceBusy} /></div>
        <div className="field"><label className="label" htmlFor="hotel-check-in">入住日期</label><input className="input" id="hotel-check-in" type="date" required value={start} onChange={e => setStart(e.target.value)} disabled={busy || invoiceBusy} /></div>
        <div className="field"><label className="label" htmlFor="hotel-check-out">退房日期</label><input className="input" id="hotel-check-out" type="date" required value={end} onChange={e => setEnd(e.target.value)} disabled={busy || invoiceBusy} /></div>
      </div>
      <p role="status">住宿晚数：{validDates ? `${nights} 晚` : '请填写有效的入住和退房日期'}（按退房日期减入住日期计算）</p>
      {start && end && !validDates && <p role="alert">退房须晚于入住，住宿不得超过 365 晚。</p>}
      <p>适用职级：{String(grade?.value.grade ?? '待匹配')}；城市等级：{String(city?.value.tier ?? '待匹配')}。标准由系统匹配；住宿申报尚待财务核实。</p>
      <div className="submission-fields">{([['purpose', '出差事由／业务用途', purpose, setPurpose], ['notes', '备注及特殊情况', notes, setNotes]] as const).map(([name, label, value, setter]) => <div className="field" key={name}><label className="label" htmlFor={`hotel-${name}`}>{label}</label><input className="input" id={`hotel-${name}`} type="text" required={name !== 'notes'} value={value} onChange={e => setter(e.target.value)} disabled={busy || invoiceBusy} /></div>)}</div>
      </section>
      </InvoicePanel>
      {error && <p role="alert">{error}</p>}<button className="btn btn-primary" disabled={busy || invoiceBusy || !validDates}>{busy ? '正在提交' : existing ? '修改后重新提交住宿审核' : '提交住宿审核'}</button>
    </form></section>;
}

function Master({ user, reload }: { user: User; reload: () => Promise<void> }) {
  const [kind, setKind] = useState('STAY'); const [key, setKey] = useState('');
  const [v, setV] = useState<Record<string, string>>({ grade: 'D', tier: 'I', level: 'PREFECTURE', effective_from: '', effective_to: '', version: '', source: '', reason: '' });
  const [users, setUsers] = useState<{ id: string; name: string }[]>([]); const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  useEffect(() => { api<{ items: { id: string; name: string }[] }>('/people').then(r => setUsers(r.items)).catch(e => setError(errorText(e))); }, [user.role]);
  function field(name: string, label: string, type = 'text', required = true) { return <div className="field" key={name}><label className="label" htmlFor={`master-${name}`}>{label}</label><input className="input" id={`master-${name}`} type={type} required={required} value={v[name] ?? ''} onChange={e => setV(s => ({ ...s, [name]: e.target.value }))} /></div>; }
  function selectField(name: string, label: string, choices: [string, string][]) { return <div className="field"><label className="label" htmlFor={`master-${name}`}>{label}</label><select id={`master-${name}`} className="select" value={v[name] ?? ''} onChange={e => setV(s => ({ ...s, [name]: e.target.value }))}><option value="">请选择</option>{choices.map(([value, text]) => <option key={value} value={value}>{text}</option>)}</select></div>; }
  async function submit(e: FormEvent) {
    e.preventDefault(); if (busy) return; setBusy(true); setError('');
    const value = kind === 'EMPLOYEE' ? { grade: v.grade } : kind === 'CITY' ? { name: v.name, tier: v.tier, level: v.level, aliases: (v.aliases ?? '').split('、').filter(Boolean) } : { employee_id: v.employee_id, city_key: v.city_key, address: v.address, check_in: v.check_in, check_out: v.check_out, evidence_reference: v.evidence_reference };
    try { await api('/facts', { kind, key, value, effective_from: v.effective_from, effective_to: v.effective_to, source: v.source, version: v.version, reason: v.reason }); await reload(); setKey(''); }
    catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  }
  return <details className="card"><summary>维护职级、城市及住宿核验记录</summary><p>记录追加保存，生效日期不得重叠。城市来源须能证明行政层级；住宿地址须来自实际订单，不能使用开票方注册地。</p><form className="submission-form" onSubmit={submit}>
    <label className="label" htmlFor="master-kind">记录类型</label><select id="master-kind" className="select" value={kind} onChange={e => setKind(e.target.value)}><option value="STAY">住宿事实核验</option>{user.role === 'ADMIN' && <><option value="EMPLOYEE">员工有效职级</option><option value="CITY">受控城市映射</option></>}</select>
    <label className="label" htmlFor="master-key">{kind === 'EMPLOYEE' ? '员工' : kind === 'CITY' ? '城市记录编号' : '住宿记录编号'}</label>{kind === 'EMPLOYEE' ? <select id="master-key" className="select" required value={key} onChange={e => setKey(e.target.value)}><option value="">请选择员工</option>{users.map(u => <option key={u.id} value={u.id}>{u.name}</option>)}</select> : <input id="master-key" className="input" required value={key} onChange={e => setKey(e.target.value)} />}
    {kind === 'EMPLOYEE' ? selectField('grade', '核实职级', [['A', 'A 高管'], ['B', 'B 部门负责人'], ['C', 'C 主管／项目经理'], ['D', 'D 普通员工']]) : kind === 'CITY' ? <>{field('name', '城市或县域标准名称')}{field('aliases', '明确别名，以顿号分隔（可选）', 'text', false)}{selectField('tier', '制度城市等级', [['I', 'Ⅰ类'], ['II', 'Ⅱ类'], ['III', 'Ⅲ类'], ['IV', 'Ⅳ类']])}{selectField('level', '核实的行政层级', [['MUNICIPALITY', '直辖市城区'], ['PREFECTURE', '地级市城区'], ['COUNTY', '县或县级市'], ['TOWN', '乡镇']])}</> : <>{selectField('employee_id', '住宿员工', users.map(u => [u.id, u.name]))}{field('city_key', '受控城市记录编号')}{field('address', '实际住宿地址')}{field('check_in', '入住日期', 'date')}{field('check_out', '退房日期', 'date')}{field('evidence_reference', '订单或住宿证明的证据引用')}</>}
    {field('effective_from', '有效起始日（含）', 'date')}{field('effective_to', '有效结束日（不含）', 'date')}{field('version', '主数据来源版本')}{field('source', '可信数据来源')}{field('reason', '核实及变更说明')}
    {error && <p role="alert">{error}</p>}<button className="btn" disabled={busy}>保存核验记录</button></form></details>;
}

type LifecyclePreview = { target: Version; restore: Version | null; kind: string; effective_on: string; preview_digest: string };
function Lifecycle() {
  const [versions, setVersions] = useState<Version[]>([]);
  const [loading, setLoading] = useState(true);
  const [pid, setPid] = useState(''); const [kind, setKind] = useState('RETIRE');
  const [date, setDate] = useState(''); const [restoreId, setRestoreId] = useState(''); const [reason, setReason] = useState('');
  const [preview, setPreview] = useState<LifecyclePreview | null>(null);
  const [pending, setPending] = useState<unknown>(null); const [busy, setBusy] = useState(false); const [message, setMessage] = useState('');
  async function load() { setLoading(true); try { setVersions((await api<{ items: Version[] }>('/versions')).items); } catch (e) { setMessage(errorText(e)); } finally { setLoading(false); } }
  useEffect(() => { void load(); }, []);
  function invalidate() { setPreview(null); setPending(null); setMessage(''); }
  async function change(execute: boolean) {
    if (busy) return; setBusy(true); setMessage('');
    try {
      const base = { kind, effective_on: date, restore_id: kind === 'ROLLBACK' ? restoreId : null };
      if (!execute) { setPending(null); setPreview(await api<LifecyclePreview>(`/versions/${pid}/lifecycle-preview`, base)); }
      else if (preview) {
        const body = pending ?? { ...base, preview_digest: preview.preview_digest, confirmed: true, idempotency_key: crypto.randomUUID(), reason };
        setPending(body); await api(`/versions/${pid}/lifecycle`, body); setPending(null); setPreview(null);
        setMessage('变更已记录，历史规则与审核快照保留。'); await load();
      }
    } catch (e) { setMessage(errorText(e)); } finally { setBusy(false); }
  }
  const matrices = versions.filter(version => version.rules?.rule_type === 'HOTEL_LIMIT_MATRIX');
  const scopeName = (value: string) => value === '*' ? '各城市等级' : value;
  return <details className="card hotel-lifecycle"><summary>管理已发布标准：停用或恢复旧版本</summary>
    <p>这部分用于调整已经发布的住宿标准，日常发布无需填写。所有变更保留历史记录，并从指定的未来日期开始。</p>
    <button type="button" className="btn btn-sm" disabled={busy || loading} onClick={() => void load()}>{loading ? '正在读取…' : '刷新已发布标准'}</button>
    {!loading && !matrices.length ? <div className="hotel-management-empty"><strong>还没有已发布的住宿标准</strong><p>先完成上方的发布流程；需要停用或恢复旧版本时，再来这里操作。</p></div> : !loading && <>
      <div className="hotel-lifecycle-fields">
        <div><label className="label" htmlFor="lifecycle-target">选择要调整的住宿标准</label><select id="lifecycle-target" className="select" disabled={busy} value={pid} onChange={e => { setPid(e.target.value); invalidate(); }}><option value="">请选择已发布的版本</option>{matrices.map(v => <option key={v.policy_version_id} value={v.policy_version_id}>{v.version} · {scopeName(v.region)}</option>)}</select></div>
        <div><label className="label" htmlFor="lifecycle-kind">需要做什么</label><select id="lifecycle-kind" className="select" disabled={busy} value={kind} onChange={e => { setKind(e.target.value); invalidate(); }}><option value="RETIRE">停用这份标准</option><option value="ROLLBACK">恢复使用某个旧版本</option></select></div>
        <div><label className="label" htmlFor="lifecycle-date">从哪一天开始变更</label><input id="lifecycle-date" className="input" type="date" disabled={busy} value={date} onChange={e => { setDate(e.target.value); invalidate(); }} /></div>
        {kind === 'ROLLBACK' && <div><label className="label" htmlFor="lifecycle-restore">选择要恢复的旧版本</label><select id="lifecycle-restore" className="select" disabled={busy} value={restoreId} onChange={e => { setRestoreId(e.target.value); invalidate(); }}><option value="">请选择旧版本</option>{versions.filter(v => v.policy_version_id !== pid).map(v => <option key={v.policy_version_id} value={v.policy_version_id}>{v.version} · {scopeName(v.region)}</option>)}</select></div>}
      </div>
      {kind === 'ROLLBACK' && <p className="hotel-management-note">恢复旧版本会建立一份新记录。若旧版本只覆盖一个城市，其余城市不会自动沿用这份标准。</p>}
      <label className="label" htmlFor="lifecycle-reason">变更原因</label><textarea id="lifecycle-reason" className="input" rows={2} placeholder="说明为什么停用或恢复该版本，记录将被保留。" disabled={busy} value={reason} onChange={e => { setReason(e.target.value); setPending(null); }} />
      <button type="button" className="btn" disabled={busy || !pid || !date || (kind === 'ROLLBACK' && !restoreId)} onClick={() => void change(false)}>先查看变更影响</button>
      {preview && <div className="hotel-lifecycle-preview"><strong>请再次核对变更影响</strong><p>{preview.target.version} 将从 {preview.effective_on} 起停止适用。</p><p>{preview.restore ? `恢复来源：${preview.restore.version} · ${scopeName(preview.restore.region)}，仅恢复其适用范围。` : '停用后，未被其他有效制度覆盖的申请将需要人工核实。'}</p><button type="button" className="btn btn-primary" disabled={busy || !reason.trim()} onClick={() => void change(true)}>确认{kind === 'RETIRE' ? '停用标准' : '恢复旧版本'}</button></div>}
    </>}
    {message && <p role="status" className="hotel-management-note">{message}</p>}
  </details>;
}

type HotelRecord = { claim_id: string; rule_result: Report | null; summary?: PortalClaim };

export function HotelAuditRecords() {
  const { user } = useAuth();
  const [params, setParams] = useSearchParams();
  const cid = params.get('claim');
  const [claims, setClaims] = useState<HotelRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [checkFilter, setCheckFilter] = useState('ALL');
  const [stageFilter, setStageFilter] = useState('ALL');
  const [reload, setReload] = useState(0);
  const employee = user?.role === 'EMPLOYEE';
  useEffect(() => {
    let active = true;
    setLoading(true); setError(''); setClaims([]);
    Promise.all([
      api<{ items: { claim_id: string; rule_result: Report | null }[] }>('/claims'),
      portalHttp<{ items: PortalClaim[] }>('/claims'),
    ]).then(([records, details]) => {
      if (!active) return;
      const summaries = new Map(details.items.map(item => [item.claim_id, item]));
      const items = records.items.map(item => ({ ...item, summary: summaries.get(item.claim_id) }));
      const submittedAt = (item: HotelRecord) => {
        const time = item.summary?.history[item.summary.history.length - 1]?.submitted_at ?? item.summary?.created_at;
        return time ? Date.parse(time) || 0 : 0;
      };
      setClaims(items.sort((a, b) => submittedAt(b) - submittedAt(a) || a.claim_id.localeCompare(b.claim_id)));
    }).catch(e => { if (active) setError(errorText(e)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [user?.id, reload]);
  const filtered = claims.filter(item => {
    const fields = item.summary?.fields;
    const search = [item.claim_id, fields?.purpose, fields?.city, fields?.applicant_name, fields?.department, item.rule_result?.city].join(' ').toLocaleLowerCase();
    const check = item.rule_result ? ['PASS', 'FAIL'].includes(item.rule_result.decision) ? item.rule_result.decision : 'UNKNOWN' : 'PENDING';
    return search.includes(query.trim().toLocaleLowerCase()) && (checkFilter === 'ALL' || checkFilter === check) &&
      (stageFilter === 'ALL' || stageFilter === item.summary?.stage || stageFilter === 'APPROVED' && item.summary?.stage === 'ERP_SYNC_PENDING');
  });
  const hasFilter = !!query.trim() || checkFilter !== 'ALL' || stageFilter !== 'ALL';
  function clearFilters() { setQuery(''); setCheckFilter('ALL'); setStageFilter('ALL'); }
  function selectClaim(value: string | null) {
    setParams(previous => { const next = new URLSearchParams(previous); if (value) next.set('claim', value); else next.delete('claim'); return next; });
    if (!value) requestAnimationFrame(() => document.getElementById(`hotel-record-${cid}`)?.focus());
  }
  return <Layout title="住宿审核记录"><div className="hotel-matrix-page">
    <div className="page-header"><div><h1 className="page-title">住宿审核记录</h1><p className="page-desc">按住宿事项查看申请、额度检查与审批进度。</p></div></div>
    <section className="card hotel-records" aria-label="住宿申请记录" aria-busy={loading}>
      <div className="hotel-record-toolbar">
        <div className="hotel-record-search"><label htmlFor="hotel-record-search">搜索记录</label><input type="search" id="hotel-record-search" className="input" placeholder="城市、事由或单号" value={query} onChange={e => setQuery(e.target.value)} /></div>
        <div><label htmlFor="hotel-check-filter">住宿额度检查</label><select id="hotel-check-filter" className="select" value={checkFilter} onChange={e => setCheckFilter(e.target.value)}><option value="ALL">全部检查结果</option><option value="PASS">未超住宿限额</option><option value="FAIL">不符合住宿标准</option><option value="UNKNOWN">待核实</option><option value="PENDING">尚未完成检查</option></select></div>
        <div><label htmlFor="hotel-stage-filter">审批进度</label><select id="hotel-stage-filter" className="select" value={stageFilter} onChange={e => setStageFilter(e.target.value)}><option value="ALL">全部审批进度</option>{['AI_AUDITING', 'FINANCE_REVIEW', 'FINANCE_APPLYING', 'RETURNED', 'APPROVED', 'REJECTED', 'SYSTEM_ERROR'].map(stage => <option key={stage} value={stage}>{stage === 'FINANCE_APPLYING' ? '财务决定处理中' : STAGE_LABEL[stage]}</option>)}</select></div>
        <button type="button" className="btn btn-sm" disabled={loading} onClick={() => setReload(value => value + 1)}>刷新记录</button>
      </div>
      {!loading && !error && <div className="hotel-record-meta"><span role="status">共 {claims.length} 条{hasFilter ? `，筛选后 ${filtered.length} 条` : ''} · 按最近提交排序</span>{hasFilter && <button type="button" className="link-btn" onClick={clearFilters}>清除筛选</button>}</div>}
      {loading ? <p role="status" className="hotel-record-empty">正在读取住宿审核记录…</p> : error ? <div className="hotel-record-empty"><p role="alert">{error}</p><button type="button" className="btn btn-sm" onClick={() => setReload(value => value + 1)}>重新读取</button></div> : filtered.length === 0 ? <div className="hotel-record-empty"><strong>{hasFilter ? '没有符合条件的住宿申请' : '暂无住宿申请记录'}</strong><p>{hasFilter ? '试试其他关键词，或清除筛选条件。' : '提交住宿申请后，可以在这里查看审核进度。'}</p></div> :
        <div className="table-wrap hotel-record-table-wrap"><table className="table hotel-record-table"><thead><tr><th scope="col" className="record-sequence">序号</th><th scope="col">住宿事项</th>{!employee && <th scope="col">申请人</th>}<th scope="col" className="cell-num">申请金额</th><th scope="col">住宿额度检查</th><th scope="col">审批进度</th><th scope="col">操作</th></tr></thead><tbody>{filtered.map((item, index) => {
          const fields = item.summary?.fields;
          const check = lodgingCheck(item.rule_result);
          const nights = fields?.nights ?? item.rule_result?.nights;
          return <tr key={item.claim_id} className={cid === item.claim_id ? 'selected' : ''}>
            <td data-label="序号" className="record-sequence">{index + 1}</td>
            <td data-label="住宿事项" className="hotel-record-subject"><strong>{fields?.city || item.rule_result?.city || '城市待核实'} · {fields?.purpose || '住宿申请'}{nights != null ? ` · ${nights} 晚` : ''}</strong><small>入住 {fields?.trip_start || '—'} → 退房 {fields?.trip_end || '—'}</small><RecordId value={item.claim_id} /></td>
            {!employee && <td data-label="申请人">{fields?.applicant_name || '—'}<small>{fields?.department || '—'}</small></td>}
            <td data-label="申请金额" className="cell-money cell-num">{recordMoney(fields?.amount ?? item.rule_result?.claimed_amount)}</td>
            <td data-label="住宿额度检查"><span className={`badge badge-no-dot badge-tag-${check.tone}`}>{check.label}</span></td>
            <td data-label="审批进度"><span className={`badge badge-no-dot portal-status status-${item.summary?.stage.toLowerCase() ?? 'unknown'}`}>{item.summary ? STAGE_LABEL[item.summary.stage] ?? '状态待确认' : '状态未取得'}</span></td>
            <td data-label="操作"><button type="button" id={`hotel-record-${item.claim_id}`} className="btn btn-sm" aria-controls="hotel-record-detail" aria-expanded={cid === item.claim_id} aria-label={`查看第 ${index + 1} 条住宿申请详情`} onClick={() => selectClaim(item.claim_id)}>查看详情</button></td>
          </tr>;
        })}</tbody></table></div>}
      <p className="hotel-record-note">序号按当前列表排序；住宿额度检查结果与财务审批结论分别展示。</p>
    </section>
    {cid && <Result key={cid} cid={cid} onClose={() => selectClaim(null)} />}
  </div></Layout>;
}

export default function HotelMatrix() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const editId = params.get('edit');
  const [existing, setExisting] = useState<PortalClaim | undefined>();
  const [originalStay, setOriginalStay] = useState<Fact | null>(null);
  const [originalAddress, setOriginalAddress] = useState('');
  const [editLoading, setEditLoading] = useState(!!editId);
  const { user } = useAuth(); const [facts, setFacts] = useState<Fact[]>([]); const [error, setError] = useState('');
  async function reload() { try { setFacts((await api<{ items: Fact[] }>('/facts')).items); } catch (e) { setError(errorText(e)); } }
  useEffect(() => { void reload(); }, [user?.id]);
  useEffect(() => {
    let active = true;
    setExisting(undefined); setOriginalStay(null); setOriginalAddress(''); setError('');
    if (!editId) { setEditLoading(false); return; }
    setEditLoading(true);
    Promise.all([
      portalHttp<PortalClaim>(`/claims/${encodeURIComponent(editId)}`),
      api<{ facts: { stay: Fact | null; declaration: { address?: string } } }>(`/claims/${encodeURIComponent(editId)}`),
    ]).then(([item, snapshot]) => {
      if (!active) return;
      if (!item.p4a || item.stage !== 'RETURNED') throw new Error('仅能修改已退回的住宿单据');
      setOriginalStay(snapshot.facts.stay); setOriginalAddress(snapshot.facts.declaration.address ?? String(snapshot.facts.stay?.value.address ?? '')); setExisting(item);
    }).catch(e => { if (active) setError(errorText(e)); })
      .finally(() => { if (active) setEditLoading(false); });
    return () => { active = false; };
  }, [editId]);
  if (!user) return null;
  return <Layout title={user.role === 'EMPLOYEE' ? editId ? '修改住宿申请' : '住宿费申请' : user.role === 'ADMIN' ? '住宿标准管理' : '住宿核验与职级信息'}><div className="hotel-matrix-page">
    {user.role === 'EMPLOYEE' && <p><Link to="/my-claims">← 返回我的报销</Link></p>}
    {user.role !== 'ADMIN' && <p>员工填写住宿地点、日期和总金额；系统匹配职级及城市标准，财务核实后审核。</p>}
    {error && <p role="alert">{error}</p>}
    {user.role === 'EMPLOYEE' ? editId && (editLoading || !existing) ?
      editLoading && <p role="status">正在读取原住宿申请…</p> :
      <Apply key={`${existing?.claim_id ?? 'new'}:${existing?.revision ?? 0}`} existing={existing} originalStay={originalStay} originalAddress={originalAddress} user={user} facts={facts} saved={id => navigate(`/hotel-records?claim=${encodeURIComponent(id)}`)} /> :
      user.role === 'ADMIN' ? <>
        <HotelPolicyPublication key={params.get('upload') ?? 'manual'} uploadId={params.get('upload') ?? ''} />
        <div className="hotel-secondary-management"><h2>其他管理操作</h2><p>首次发布无需填写；仅在维护人员档案或调整已发布标准时使用。</p>
          <Master user={user} reload={reload} /><Lifecycle />
        </div>
      </> : <Master user={user} reload={reload} />}
  </div></Layout>;
}
