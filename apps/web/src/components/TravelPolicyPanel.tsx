import { useEffect, useState } from 'react';
import TravelRuntime from './TravelRuntime';
import { portalHttp } from '../lib/api/enterprise';
import type { TravelPolicy, TravelRule, TravelType } from '../lib/api/travel';

const p4a = import.meta.env.VITE_P4A_ENABLED === 'true';
const names = { HOTEL: '住宿费', TRANSPORT: '交通费', ALLOWANCE: '差旅补助' };
const fresh = (type: TravelType = p4a ? 'TRANSPORT' : 'HOTEL'): TravelRule => ({ expense_type: type,
  cities: ['*'], departments: ['*'], positions: ['*'], grades: ['*'],
  unit: type === 'HOTEL' ? 'NIGHT' : type === 'ALLOWANCE' ? 'DAY' : 'TRIP', limit: '',
  transport_mode: '', seats: [], breakfast_rate: '0', lunch_rate: '0', dinner_rate: '0',
  source_start: 0, source_end: 0, review_required: false });
const list = (value: string) => value.trim() === '全部' ? ['*'] : value.split(/[,，、\n]/).map(v => v.trim()).filter(Boolean);
const scopeText = (values: string[], name = '') => values.includes('*') ? `全部${name}` : values.join('、');
export default function TravelPolicyPanel({ uploadId }: { uploadId: string }) {
  const [data, setData] = useState<TravelPolicy | null>(null);
  const [rules, setRules] = useState<TravelRule[]>([]);
  const [rule, setRule] = useState<TravelRule>(fresh());
  const [start, setStart] = useState(0); const [end, setEnd] = useState(0);
  const [reason, setReason] = useState(''); const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const [notice, setNotice] = useState(''); const [retireDate, setRetireDate] = useState('');
  const [requestKey, setRequestKey] = useState('');
  useEffect(() => {
    let active = true;
    portalHttp<TravelPolicy>(`/travel/policies/${uploadId}`).then(value => {
      if (active) { setData(value); setRules([]); setRule(fresh()); setStart(0); setEnd(0); setError(''); setNotice(''); setConfirmed(false); setReason(''); setRequestKey(''); }
    }).catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [uploadId]);
  const lines = data?.parsed.lines ?? [];
  const quote = lines[start] && lines[end] && end >= start ? data!.parsed.text.slice(lines[start].start, lines[end].end) : '';
  function edit(patch: Partial<TravelRule>) { setRule(current => ({ ...current, ...patch })); setConfirmed(false); setRequestKey(''); }
  function add() {
    setError('');
    if (!quote || quote.length > 6000 || (rule.expense_type !== 'TRANSPORT' && !rule.limit)) { setError('请选择原文依据并填写标准，引用最多 6000 字。'); return; }
    setRules(current => [...current, { ...rule, limit: rule.limit || null, source_start: lines[start].start, source_end: lines[end].end, quote }]);
    setConfirmed(false); setRequestKey(''); setRule(fresh(rule.expense_type));
  }
  async function publish() {
    if (!data || busy || !confirmed || !rules.length || !reason.trim()) return;
    setBusy(true); setError('');
    const key = requestKey || crypto.randomUUID(); setRequestKey(key);
    try {
      await portalHttp(`/travel/policies/${uploadId}/publish`, { rules: rules.map(({ quote: _quote, ...rest }) => rest), expected_revision: data.revision, confirmed: true, reason, idempotency_key: key });
      setData(await portalHttp<TravelPolicy>(`/travel/policies/${uploadId}`)); setRules([]); setNotice('差旅标准已发布，将按制度生效日期用于审核。'); setRequestKey('');
    } catch (e) { setError(e instanceof Error ? e.message : '发布失败'); } finally { setBusy(false); }
  }
  async function retire() {
    if (!data || busy || !retireDate || !reason.trim()) return;
    setBusy(true); setError('');
    const key = requestKey || crypto.randomUUID(); setRequestKey(key);
    try {
      await portalHttp(`/travel/policies/${uploadId}/retire`, { expected_revision: data.revision, effective_on: retireDate, reason, idempotency_key: key });
      setData(await portalHttp<TravelPolicy>(`/travel/policies/${uploadId}`)); setNotice('退休日期已记录，历史审核保留原标准。'); setRequestKey('');
    } catch (e) { setError(e instanceof Error ? e.message : '操作失败'); } finally { setBusy(false); }
  }
  const published = data?.versions[0];
  return <section className="card policy-governance" aria-label="差旅多维标准"><h2>{p4a ? '交通与差旅补助标准' : '交通、住宿与差旅补助标准'}</h2>
    <TravelRuntime /><p>按城市、部门、职位及职级配置标准。上传的原文由管理员核对并发布后，供规则引擎和 AI 共同分析。</p>
    {error && <div className="alert alert-danger" role="alert">{error}</div>}{notice && <div className="alert alert-success" role="status">{notice}</div>}
    {!data && !error && <p role="status">正在读取制度依据…</p>}
    {published ? <><p>已发布 {published.metadata.version} · 生效 {published.metadata.effective_from} · {published.rules.length} 条标准</p>
      {published.rules.map((r, i) => <article className="policy-reference-row" key={i}><strong>{names[r.expense_type]}</strong><p>城市：{scopeText(r.cities)} · 部门：{scopeText(r.departments)} · 职位：{scopeText(r.positions)} · 职级：{scopeText(r.grades)}</p><p>{r.limit ? `${r.limit} 元 / ${r.unit === 'NIGHT' ? '晚' : r.unit === 'DAY' ? '天' : '次'}` : '按交通席别审核'} {r.transport_mode} {r.seats.join('、')}</p><details><summary>制度依据</summary><pre className="travel-quote">{r.quote}</pre></details></article>)}
      {data.retirements.length ? <p>退休日期：{data.retirements[0].effective_on}</p> : <div className="submission-fields"><div className="field"><label className="label" htmlFor="travel-retire">退休日期</label><input className="input" type="date" id="travel-retire" value={retireDate} onChange={e => { setRetireDate(e.target.value); setRequestKey(''); }} /></div><div className="field"><label className="label" htmlFor="travel-retire-reason">退休原因</label><input className="input" id="travel-retire-reason" value={reason} onChange={e => { setReason(e.target.value); setRequestKey(''); }} /></div><button className="btn" disabled={busy || !retireDate || !reason.trim()} onClick={() => void retire()}>记录未来退休日期</button></div>}
    </> : data && <fieldset disabled={busy} className="travel-editor"><div className="submission-fields">
      <div className="field"><label className="label" htmlFor="travel-type">标准费用类型</label><select className="select" id="travel-type" value={rule.expense_type} onChange={e => { setRule(fresh(e.target.value as TravelType)); setConfirmed(false); setRequestKey(''); }}>{Object.entries(names).filter(([key]) => !p4a || key !== 'HOTEL').map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></div>
      {(['cities', 'departments', 'positions', 'grades'] as const).map((key, i) => <div className="field" key={key}><label className="label" htmlFor={`travel-${key}`}>{['适用城市', '适用部门', '适用职位', '适用职级'][i]}</label><input className="input" id={`travel-${key}`} value={scopeText(rule[key])} onChange={e => edit({ [key]: list(e.target.value) })} placeholder={key === 'grades' ? 'A、B、C、D 或全部' : '填全部，或用顿号分隔多个名称'} /></div>)}
      <div className="field"><label className="label" htmlFor="travel-limit">{rule.expense_type === 'ALLOWANCE' ? '每日补助（元）' : rule.expense_type === 'HOTEL' ? '每晚住宿限额（元）' : '交通金额限额（可选）'}</label><input className="input" id="travel-limit" inputMode="decimal" value={rule.limit ?? ''} onChange={e => edit({ limit: e.target.value })} /></div>
      {rule.expense_type === 'TRANSPORT' && <><div className="field"><label className="label" htmlFor="travel-mode">交通工具</label><input className="input" id="travel-mode" value={rule.transport_mode} onChange={e => edit({ transport_mode: e.target.value })} placeholder="如：高铁、飞机、市内交通" /></div><div className="field"><label className="label" htmlFor="travel-seats">允许席别／方式</label><input className="input" id="travel-seats" value={rule.seats.join('、')} onChange={e => edit({ seats: list(e.target.value) })} placeholder="如：二等座、硬卧；无席别填制度中的方式" /></div><div className="field"><label className="label" htmlFor="travel-unit">金额限额口径</label><select className="select" id="travel-unit" value={rule.unit} onChange={e => edit({ unit: e.target.value as TravelRule['unit'] })}><option value="TRIP">每次</option><option value="DAY">每天</option></select></div></>}
      {rule.expense_type === 'ALLOWANCE' && (['breakfast_rate', 'lunch_rate', 'dinner_rate'] as const).map((key, i) => <div className="field" key={key}><label className="label" htmlFor={key}>{['早餐', '午餐', '晚餐'][i]}供餐扣减比例（%）</label><input className="input" id={key} type="number" min="0" max="100" step="1" value={Number(rule[key]) * 100} onChange={e => edit({ [key]: String(Number(e.target.value) / 100) })} /></div>)}
      {(['start', 'end'] as const).map((key, i) => <div className="field" key={key}><label className="label" htmlFor={`travel-source-${key}`}>引用原文{ i ? '结束' : '开始' }段落</label><select className="select" id={`travel-source-${key}`} value={i ? end : start} onChange={e => { const n = Number(e.target.value); if (i) setEnd(n); else { setStart(n); setEnd(current => Math.max(current, n)); } setConfirmed(false); setRequestKey(''); }}>{lines.map((line, n) => <option key={n} value={n}>[{line.line}] {line.text}</option>)}</select></div>)}
    </div>
      {!!data.tables?.length && <details><summary>查看原文表格，选择标准依据</summary><div className="policy-reference-rows">{data.tables.map((row, i) => <article className="policy-reference-row" key={i}><p>{row.cells.join(' ｜ ')}</p><button type="button" className="btn btn-sm" onClick={() => { const first = lines.findIndex(line => line.start === row.table_start); const last = lines.findIndex(line => line.end === row.table_end); if (first >= 0 && last >= first) { setStart(Math.max(0, first - 1)); setEnd(Math.min(lines.length - 1, last + 1)); setConfirmed(false); setRequestKey(''); } }}>引用此表及相邻说明</button></article>)}</div></details>}
      <pre className="travel-quote">{quote || '请选择包含表头、条件及标准数值的原文段落'}</pre>
      <label className="policy-check"><input type="checkbox" checked={rule.review_required} onChange={e => edit({ review_required: e.target.checked })} />此条款还有未结构化的例外条件，必须人工核对</label>
      <button type="button" className="btn" onClick={add}>加入待发布标准</button>
      <p className="page-desc">职位、部门、城市名称须与账号和申请一致；职级请在住宿职级标准页面按有效期维护。复杂例外须勾选人工核对。金额、席别、扣减比例将校验原文依据。</p>
      {rules.map((r, i) => <article className="policy-reference-row" key={i}><strong>{names[r.expense_type]} · {r.limit ?? '席别标准'} {r.unit === 'NIGHT' ? '元/晚' : r.unit === 'DAY' ? '元/天' : r.limit ? '元/次' : ''}</strong><p>{scopeText(r.cities)} · {scopeText(r.departments)} · {scopeText(r.positions)} · 职级 {scopeText(r.grades)}</p><button type="button" className="btn btn-sm" onClick={() => { setRules(current => current.filter((_, n) => n !== i)); setConfirmed(false); setRequestKey(''); }}>移除此待发布标准</button></article>)}
      <div className="field"><label className="label" htmlFor="travel-publish-reason">标准发布依据</label><input className="input" id="travel-publish-reason" maxLength={1000} value={reason} onChange={e => { setReason(e.target.value); setConfirmed(false); setRequestKey(''); }} /></div>
      <label className="policy-check"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />我已逐条核对原文、城市、部门、职位／职级、金额、席别、扣减与例外条件</label>
      <button type="button" className="btn btn-primary" disabled={busy || !confirmed || !rules.length || !reason.trim()} onClick={() => void publish()}>{busy ? '正在发布…' : `发布 ${rules.length} 条差旅标准`}</button>
    </fieldset>}
  </section>;
}
