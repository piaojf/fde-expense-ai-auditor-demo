import { useCallback, useRef, useState, type FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import Layout from '../components/Layout';
import TravelRuntime from '../components/TravelRuntime';
import InvoicePanel from '../components/InvoicePanel';
import { DRAFT_STORAGE, invoiceHttp, type InvoiceConfirmation, type InvoiceResponse } from '../lib/api/invoices';
import { USE_MOCK_API } from '../lib/api/client';
import { ApiError } from '../lib/api/types';
import { CITIES, submissionHttp, type SubmissionFields } from '../lib/api/submission';
import { saveRunContext } from '../lib/session/runContext';
import { portalHttp, storageKey, type PortalClaim, type User } from '../lib/api/enterprise';

import { travelDetails, type TravelDetails, type TravelType } from '../lib/api/travel';

const STORAGE = 'ui-p1-pending-submission';
type Pending = SubmissionFields & { idempotency_key: string; travel_details?: TravelDetails;
  p4b_kind?: 'ALLOWANCE' | 'ENTERTAINMENT' } & Partial<InvoiceConfirmation>;
const empty: SubmissionFields = {
  applicant_name: '', department: '', position: '', category: 'HOTEL', amount: '',
  incurred_on: '', purpose: '', city: '', trip_start: '', trip_end: '', nights: 1, seller: '', notes: '',
};
function readPending(storage = STORAGE): Pending | null {
  try { return JSON.parse(sessionStorage.getItem(storage) ?? 'null'); } catch { return null; }
}
export default function NewClaim({ portalUser, existing, onSaved }: {
  portalUser?: User; existing?: PortalClaim; onSaved?: (claim: PortalClaim) => void;
} = {}) {
  const navigate = useNavigate();
  const p4a = !!portalUser && import.meta.env.VITE_P4A_ENABLED === 'true';
  const p4b = p4a && import.meta.env.VITE_P4B_ENABLED === 'true';
  const storage = portalUser ? storageKey(`${STORAGE}-${existing?.claim_id ?? 'new'}-${existing?.revision ?? 0}`) : STORAGE;
  const [pending, setPending] = useState<Pending | null>(() => readPending(storage));
  const [entertainment, setEntertainment] = useState(() => readPending(storage)?.p4b_kind === 'ENTERTAINMENT');
  const [fields, setFields] = useState<SubmissionFields>(() => {
    const saved = readPending(storage);
    if (!saved) return existing?.fields ?? (portalUser ? { ...empty, applicant_name: portalUser.name,
      department: portalUser.department, position: portalUser.position,
      ...(p4a ? { category: 'TRANSPORT', nights: null } : {}) } : empty);
    return Object.fromEntries(Object.keys(empty).map(key => [key, saved[key as keyof SubmissionFields]])) as unknown as SubmissionFields;
  });
  const [travel, setTravel] = useState<TravelDetails>(() => readPending(storage)?.travel_details ?? existing?.travel_details ?? travelDetails(existing?.fields.category === 'TRANSPORT' || (p4a && !existing) ? 'TRANSPORT' : 'HOTEL'));
  const [busy, setBusy] = useState(false);
  const [invoice, setInvoice] = useState<InvoiceConfirmation | null>(null);
  const [invoiceAttached, setInvoiceAttached] = useState(false);
  const [invoiceBusy, setInvoiceBusy] = useState(false);
  const [sources, setSources] = useState<Partial<Record<keyof SubmissionFields, string>>>({});
  const gate = useRef(false);
  const [error, setError] = useState('');
  const confirmInvoice = useCallback((value: InvoiceConfirmation | null) => {
    setInvoice(value);
    if (value) setError('');
  }, []);
  const today = import.meta.env.VITE_M6_DEMO === 'true'
    ? (import.meta.env.VITE_M6_BUSINESS_DATE ?? '2026-11-03')
    : new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date());
  const tripDays = fields.trip_start && fields.trip_end ?
    Math.round((Date.parse(fields.trip_end) - Date.parse(fields.trip_start)) / 86400000) + 1 : 0;
  function set<K extends keyof SubmissionFields>(key: K, value: SubmissionFields[K]) {
    setFields(current => ({ ...current, [key]: value }));
    setSources(current => current[key] ? { ...current, [key]: "人工编辑（初值来自发票识别）" } : current);
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (gate.current || USE_MOCK_API || invoiceBusy) return;
    if (existing?.compliance_analysis) { setError('混合审核申请请由财务核实补充事实并启动重审；此表单不修改已留痕的申请。'); return; }
    if (!pending && invoiceAttached && !invoice) { setError("请先核对并确认票据信息，再提交审核。"); return; }
    if (!pending && (!/^\d{1,10}(\.\d{1,2})?$/.test(fields.amount) || !/[1-9]/.test(fields.amount))) {
      setError('金额必须大于 0，最多两位小数。'); return;
    }
    if (!pending && (tripDays < 1 || tripDays > 366)) {
      setError('出差结束日期不能早于开始日期，行程最多 366 天。'); return;
    }
    gate.current = true; setBusy(true); setError('');
    const request: Pending = pending ?? { ...fields, ...(invoice ?? {}), ...(portalUser ? { travel_details: (fields.category === 'MEAL' || (fields.category === 'OTHER' && travel.expense_type !== 'ALLOWANCE')) ? undefined : travel } : {}),
      ...(p4b && fields.category === 'OTHER' && (entertainment || travel.expense_type === 'ALLOWANCE')
        ? { p4b_kind: entertainment ? 'ENTERTAINMENT' as const : 'ALLOWANCE' as const } : {}), idempotency_key: crypto.randomUUID() };
    try {
      sessionStorage.setItem(storage, JSON.stringify(request));
      setPending(request);
      if (portalUser) {
        const submissionFields = Object.fromEntries(Object.keys(empty).map(key => [key, request[key as keyof SubmissionFields]]));
        const invoiceBody = request.attachment_id ? { draft_id: request.draft_id, attachment_id: request.attachment_id,
          confirmed: true, confirmed_fields: request.confirmed_fields, discrepancy_reason: request.discrepancy_reason } : null;
        if (request.p4b_kind) {
          const created = await portalHttp<{ claim_id: string }>('/p4b/v1/claims', {
            expense_type: request.p4b_kind, city: request.city, amount: request.amount,
            incurred_on: request.incurred_on, trip_start: request.trip_start, trip_end: request.trip_end,
            purpose: request.purpose, notes: request.notes, seller: request.seller,
            invoice: invoiceBody, idempotency_key: request.idempotency_key,
            ...(request.p4b_kind === 'ALLOWANCE' ? {
              candidate_full_days: request.travel_details?.allowance_days,
              candidate_lunches: request.travel_details?.lunches, candidate_dinners: request.travel_details?.dinners,
            } : {}),
          });
          const result = await portalHttp<PortalClaim>(`/claims/${created.claim_id}`);
          sessionStorage.removeItem(storage); sessionStorage.removeItem(storageKey(DRAFT_STORAGE));
          onSaved?.(result); return;
        }
        const result = await portalHttp<PortalClaim>(existing ? `/claims/${existing.claim_id}/resubmit` : '/claims', {
          fields: submissionFields, travel_details: request.travel_details, invoice: invoiceBody, idempotency_key: request.idempotency_key,
          ...(existing ? { expected_revision: existing.revision } : {}),
        });
        sessionStorage.removeItem(storage); sessionStorage.removeItem(storageKey(DRAFT_STORAGE));
        onSaved?.(result); return;
      }
      const result = request.attachment_id ? await invoiceHttp<InvoiceResponse>('/claims', request) : await submissionHttp('', request);
      if (result.operation) saveRunContext(result.claim.claim_id, {
        run_id: result.operation.run_id, revision: result.operation.revision,
      });
      sessionStorage.removeItem(storage);
      sessionStorage.removeItem(DRAFT_STORAGE);
      navigate(`/claims/${result.claim.claim_id}`, { replace: true });
    } catch (e) {
      if (e instanceof ApiError && e.status === 422) {
        sessionStorage.removeItem(storage); setPending(null);
        setError('字段校验失败，请检查必填项、金额、日期和住宿晚数。');
      } else {
        setError(e instanceof ApiError && e.status === 409
          ? '提交键与已有申请冲突，请检查原申请；没有创建另一张单据。'
          : `${e instanceof Error ? e.message : String(e)} 原申请内容及提交键已保留，重试会返回同一单据。`);
      }
    } finally { gate.current = false; setBusy(false); }
  }
  function input(key: keyof SubmissionFields, label: string, options: { type?: string; maxLength?: number; max?: string; min?: string } = {}) {
    return <div className="field"><label className="label" htmlFor={key}>{label} <span className="req" aria-hidden="true">*</span></label>
      <input className="input" id={key} name={key} required value={String(fields[key] ?? '')}
        readOnly={!!portalUser && ['applicant_name', 'department', 'position'].includes(key)}
        {...options} onChange={e => set(key, e.target.value as never)} />{sources[key] && <small className="invoice-provenance">{sources[key]}</small>}</div>;
  }
  const content = <>
    <div className="page-header"><div><div className="page-title">{existing ? '修改并重新提交' : '新建报销'}</div>
      <div className="page-desc">填写费用与行程信息，提交后进入制度与 AI 审核。</div></div>
      {!portalUser && <Link className="btn" to="/claims">返回报销列表</Link>}</div>
    {portalUser && <TravelRuntime />}
    {existing && <div className="portal-reason" role="note"><strong>退回理由</strong><p>{existing.reason}</p><small>原编号保留，修改后重新进行 AI 和财务审核。</small></div>}
    <div className="submission-layout"><form onSubmit={submit} className="card submission-form">
      {USE_MOCK_API && <div role="alert" className="result-tip err">当前为离线演示模式，暂不支持新建报销。请切换到已连接服务的环境。</div>}
      {pending && !busy && <div className="result-tip">有一笔待确认的提交。原内容已锁定，重试会查询并继续同一申请。</div>}
      <fieldset disabled={busy || invoiceBusy || !!pending || USE_MOCK_API}>
        <div className="form-step">
          <div className="card-title"><span className="step-no">1</span>申请信息</div>
          <p className="form-step-desc">申请人身份由登录账号带出，费用类型决定后续适用的制度额度口径。</p>
          <div className="submission-fields">
            {input('applicant_name', '申请人姓名', { maxLength: 200 })}
            {input('department', '所属部门', { maxLength: 128 })}
            {input('position', '职位', { maxLength: 128 })}
            <div className="field"><label className="label" htmlFor="category">费用类型 <span className="req" aria-hidden="true">*</span></label>
              <select className="select" id="category" value={entertainment ? 'ENTERTAINMENT' : portalUser && travel.expense_type === 'ALLOWANCE' ? 'ALLOWANCE' : fields.category} onChange={e => {
                if (p4a && e.target.value === 'HOTEL') {
                  navigate(existing?.p4a ? `/hotel-matrix?edit=${encodeURIComponent(existing.claim_id)}` : '/hotel-matrix');
                  return;
                }
                setEntertainment(e.target.value === 'ENTERTAINMENT');
                if (portalUser) setTravel(travelDetails(['HOTEL', 'TRANSPORT', 'ALLOWANCE'].includes(e.target.value) ? e.target.value as TravelType : 'HOTEL'));
                const category = (['ALLOWANCE', 'ENTERTAINMENT'].includes(e.target.value) ? 'OTHER' : e.target.value) as SubmissionFields['category'];
                setFields(current => ({ ...current, category, nights: category === 'HOTEL' ? 1 : null }));
              }}><option value="HOTEL">住宿费</option><option value="TRANSPORT">交通费</option>
                {portalUser && <option value="ALLOWANCE">差旅补助</option>}{p4b && <option value="ENTERTAINMENT">业务招待费</option>}<option value="MEAL">餐饮费</option><option value="OTHER">其他费用</option></select></div>
            {input('amount', '报销金额（元 / CNY）', { maxLength: 13 })}
            {input('incurred_on', '费用发生日期', { type: 'date', max: today })}
          </div>
          <div className="field field-full"><label className="label" htmlFor="purpose">报销事项 <span className="req" aria-hidden="true">*</span></label>
            <textarea className="textarea" id="purpose" required maxLength={2000} value={fields.purpose} onChange={e => set('purpose', e.target.value)} placeholder="简述本次费用的业务事由，便于审核人判断必要性" /></div>
        </div>

        <div className="form-step">
          <div className="card-title"><span className="step-no">2</span>行程与费用</div>
          <p className="form-step-desc">住宿按晚数校验额度，出差天数按起止日期计算，二者独立计算。</p>
          <div className="submission-fields">
            <div className="field"><label className="label" htmlFor="city">出差城市 <span className="req" aria-hidden="true">*</span></label>
              <select className="select" id="city" required value={fields.city} onChange={e => set('city', e.target.value)}>
                <option value="">请选择标准城市</option>{CITIES.map(city => <option key={city}>{city}</option>)}</select></div>
            {input('trip_start', '出差开始日期', { type: 'date' })}
            {input('trip_end', '出差结束日期', { type: 'date', min: fields.trip_start })}
            {fields.category === 'HOTEL' && <div className="field"><label className="label" htmlFor="nights">住宿晚数 <span className="req" aria-hidden="true">*</span></label>
              <input className="input" id="nights" type="number" required min={1} max={366} step={1} value={fields.nights ?? ''} onChange={e => set('nights', e.target.value === '' ? null : Number(e.target.value))} /></div>}
            {input('seller', portalUser && travel.expense_type === 'ALLOWANCE' && fields.category === 'OTHER' ? '补助发放单位' : '开票单位（销售方）', { maxLength: 200 })}
          </div>
          {portalUser && fields.category === 'TRANSPORT' && <div className="submission-fields">
            <div className="field"><label className="label" htmlFor="claim-transport-mode">交通工具</label><input className="input" id="claim-transport-mode" required maxLength={50} value={travel.transport_mode} onChange={e => setTravel(current => ({ ...current, transport_mode: e.target.value }))} placeholder="与制度一致，如高铁、飞机" /></div>
            <div className="field"><label className="label" htmlFor="claim-seat">席别／出行方式</label><input className="input" id="claim-seat" required maxLength={50} value={travel.seat} onChange={e => setTravel(current => ({ ...current, seat: e.target.value }))} placeholder="如二等座、经济舱、出租车" /></div>
            <div className="field"><label className="label" htmlFor="claim-transport-count">交通次数／发生天数（按制度口径）</label><input className="input" id="claim-transport-count" type="number" required min={1} max={366} value={travel.transport_count} onChange={e => setTravel(current => ({ ...current, transport_count: Number(e.target.value) }))} /></div>
          </div>}
          {portalUser && travel.expense_type === 'ALLOWANCE' && fields.category === 'OTHER' && <div className="submission-fields">
            {(['allowance_days', 'breakfasts', 'lunches', 'dinners'] as const).map((key, i) => <div className="field" key={key}><label className="label" htmlFor={`claim-${key}`}>{['申报补助天数', '已提供早餐次数', '已提供午餐次数', '已提供晚餐次数'][i]}</label><input className="input" id={`claim-${key}`} type="number" required min={i ? 0 : 1} max={Math.max(tripDays, 1)} value={travel[key] ?? ''} onChange={e => setTravel(current => ({ ...current, [key]: e.target.value === '' ? null : Number(e.target.value) }))} /></div>)}
          </div>}
          {p4b && fields.category === 'OTHER' && (entertainment || travel.expense_type === 'ALLOWANCE') && <p className="form-hint">员工填写的信息为待核实声明。补助仅支持完整出差日；招待人数、客户与审批凭据由财务独立核实，缺失时进入补证。</p>}
          <p className="form-hint">
            <span>出差天数</span>
            <b>{tripDays > 0 ? `${tripDays} 天` : '待填写起止日期'}</b>
            <span>·</span>
            <span>住宿 {fields.category === 'HOTEL' ? `${fields.nights ?? 0} 晚` : '不适用'}</span>
          </p>
        </div>

        <div className="form-step">
          <div className="card-title"><span className="step-no">3</span>备注说明</div>
          <div className="field"><label className="label" htmlFor="notes">备注或特殊情况说明（选填）</label>
            <textarea className="textarea" id="notes" maxLength={2000} value={fields.notes} onChange={e => set('notes', e.target.value)} placeholder="如行程变更、航班取消等，请说明核实事项" /></div>
        </div>
      </fieldset>

      <InvoicePanel fields={fields} disabled={busy || !!pending || USE_MOCK_API} onConfirmation={confirmInvoice} onAttached={setInvoiceAttached} onBusy={setInvoiceBusy}
        retained={existing?.invoice_draft && existing.invoice_evidence ? { draft: existing.invoice_draft, evidence: existing.invoice_evidence } : undefined} suggest={suggested => {
        setFields(current => ({ ...current, ...suggested }));
        setSources(Object.fromEntries(Object.keys(suggested).map(key => [key, "来源：发票识别，请人工核对"])));
      }} />

      {error && <div role="alert" className="result-tip err">{error}</div>}

      <div className="submit-bar">
        <span className="submit-note">
          提交后系统会保存申请、校验硬性条件与制度额度，并启动 AI 审核。AI 结论不构成最终审批。
        </span>
        <button className="btn btn-primary btn-lg" type="submit" disabled={busy || invoiceBusy || USE_MOCK_API}>
          {busy ? '提交中，正在创建并审核…' : pending ? '使用原提交键重试' : '提交报销并启动审核'}</button>
      </div>
    </form><aside className="card submission-guide"><div className="card-title">提交后会发生什么</div>
      <ol><li>{existing ? '保留报销编号，保存新的提交版本' : '保存申请，生成唯一报销单编号'}</li><li>校验硬性条件与制度额度</li><li>检索适用制度并分析风险</li><li>{portalUser ? '财务审核，通过后同步入账' : '查看结果，必要时进入人工复核'}</li></ol>
      <p>发票在本机识别，经人工确认后作为辅助证据提交。尚未进行税务官方真伪核验。ERP 同步使用模拟服务；AI 分析没有最终审批权。</p>
      <p>{portalUser ? '交通费、住宿费和差旅补助按职位／职级、部门、城市匹配已发布标准；缺少标准或信息时需人工核对。' : '住宿以现有城市制度审核。其他费用或缺少制度的城市需人工处理。'}</p>
    </aside></div>
  </>;
  return portalUser ? content : <Layout title="新建报销">{content}</Layout>;
}
