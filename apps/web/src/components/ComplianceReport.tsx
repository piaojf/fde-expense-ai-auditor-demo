import AdvisoryReport from './AdvisoryReport';
import { STAGE_LABEL } from '../lib/api/enterprise';
import { ERP_STATUS_LABEL } from '../utils/format';
import { RULE_CHECK_LABEL } from '../utils/recordPresentation';
import EmployeeStayVerification from './EmployeeStayVerification';
import { useEffect, useState, type FormEvent } from 'react';
import { portalHttp, type PortalClaim } from '../lib/api/enterprise';
import type { ComplianceEvidence, ComplianceVerification } from '../lib/api/compliance';

const STATUS: Record<string, string> = {
  CHECK_PASSED: '合规检查通过', NON_COMPLIANT: '存在不合规项', INSUFFICIENT_EVIDENCE: '证据不足',
  POLICY_CONFLICT: '制度冲突', UNKNOWN: '尚无法判断', COMPLIANT: '符合已核实条件',
  UNSUPPORTED: '语义能力未验证', HUMAN_REVIEW: '人工复核', REQUEST_EVIDENCE: '要求补证', REJECT: '硬性阻断',
};
const TOPICS: Record<string, string> = { BUSINESS_PURPOSE: '业务目的', POLICY_APPLICABILITY: '制度适用', EXCEPTION_REASON: '例外事由', FACT_POLICY_CONFLICT: '事实与制度矛盾', EVIDENCE_SUFFICIENCY: '证据充分性', DUPLICATE_SPLIT_CATEGORY: '重复、拆分与类别', AUTHORIZATION: '授权', UNRESOLVED_QUESTIONS: '未决事项' };
const CHECKS: Record<string, string> = { CLEAR: '已检查', ISSUE: '存在异常', UNKNOWN: '尚待核实' };
const BOOLS = [
  ['business_related', '业务必要性'], ['invoice_verified', '票据已核实'],
  ['prior_approval', '有效事前批准'], ['duplicate_clear', '重复报销已排查'],
  ['customer_identified', '客户身份'], ['cancellation_notice', '航班取消证明'],
  ['itinerary_verified', '实际行程'], ['exception_authorized', '有效例外授权'],
] as const;

export default function ComplianceReport({ item, finance, refresh }: {
  item: PortalClaim; finance: boolean; refresh: () => Promise<void>;
}) {
  if (item.advisory_analysis) return <section className="p4b-report portal-advice-report" aria-label="AI 审核建议">
    <div className="portal-advice-header"><h3>AI 审核建议</h3></div>
    <AdvisoryReport item={item} />
    {finance && item.stage === 'FINANCE_REVIEW'
      && ['FAILED', 'PARTIAL'].includes(item.advisory_analysis.ai_advisory_status)
      && <RetryAdvisory key={`${item.claim_id}:${item.version}`} item={item} refresh={refresh} />}
  </section>;
  return <LegacyComplianceReport item={item} finance={finance} refresh={refresh} />;
}

function RetryAdvisory({ item, refresh }: { item: PortalClaim; refresh: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [request, setRequest] = useState<{ expected_revision: number; idempotency_key: string } | null>(null);
  async function retry() {
    if (busy) return;
    const body = request ?? { expected_revision: item.revision, idempotency_key: crypto.randomUUID() };
    setRequest(body); setBusy(true); setError('');
    try {
      await portalHttp(`/p4b/v1/claims/${item.claim_id}/reaudit`, body);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : '重新生成失败，请刷新后重试。');
    } finally { setBusy(false); }
  }
  return <div className="portal-reason">
    <p>可重新调用当前配置的 AI 生成建议，将使用现有调用额度。历史结果保留，最终由财务决定。</p>
    <button className="btn" type="button" disabled={busy} onClick={() => void retry()}>
      {busy ? '正在重新生成…' : request ? '重试同一次建议请求' : '重新生成 AI 建议'}
    </button>
    {error && <p className="alert alert-danger" role="alert">{error}</p>}
  </div>;
}

function LegacyComplianceReport({ item, finance, refresh }: {
  item: PortalClaim; finance: boolean; refresh: () => Promise<void>;
}) {
  const report = item.compliance_analysis;
  const advisory = item.advisory_analysis;
  const [verification, setVerification] = useState<ComplianceVerification | null>(null);
  const [facts, setFacts] = useState<Record<string, string>>({});
  const [reference, setReference] = useState('');
  const [basis, setBasis] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [source, setSource] = useState<ComplianceEvidence | null>(null);
  const [pending, setPending] = useState<{ body: unknown; auditKey: string } | null>(null);
  useEffect(() => {
    if (!report) return;
    let active = true;
    portalHttp<ComplianceVerification>(`/p4b/v1/claims/${item.claim_id}`).then(value => {
      if (active) { setVerification(value); setFacts({}); }
    }).catch(e => { if (active) setError(String(e)); });
    return () => { active = false; };
  }, [item.claim_id, item.version, !!report]);
  if (!report) return null;
  async function openSource(chunk: ComplianceEvidence) {
    try { setSource(await portalHttp<ComplianceEvidence>(`/p4b/v1/claims/${item.claim_id}/evidence/${encodeURIComponent(chunk.chunk_id)}`)); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }
  async function verify(event: FormEvent) {
    event.preventDefault(); if (busy || !verification) return;
    setBusy(true); setError('');
    const values = Object.fromEntries(Object.entries(facts).filter(([, v]) => v !== '').map(([k, v]) =>
      [k, v === 'true' ? true : v === 'false' ? false : /^\d+$/.test(v) ? Number(v) : v]));
    const request = pending ?? { body: { facts: values, expected_revision: verification.fact_revision,
      source_reference: reference.trim(), basis: basis.trim(), confirmed: true, idempotency_key: crypto.randomUUID() }, auditKey: crypto.randomUUID() };
    setPending(request);
    try {
      await portalHttp(`/p4b/v1/claims/${item.claim_id}/verify`, request.body);
      await portalHttp(`/p4b/v1/claims/${item.claim_id}/reaudit`, { expected_revision: item.revision, idempotency_key: request.auditKey });
      setPending(null); await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  return <section className="p4b-report" aria-label="混合合规审核报告">
    <div className="p4b-report-header"><div><h3>{advisory ? '财务审核工作台' : '混合合规审核报告'}</h3><p>AI 建议 ≠ 财务批准 · ERP 使用模拟服务</p></div>
      <strong>{advisory ? `确定性审核 ${RULE_CHECK_LABEL[advisory.deterministic_status] ?? '待核实'}` : STATUS[report.compliance_status] ?? report.compliance_status}</strong></div>
    {!advisory && report.analysis_status !== 'SUCCEEDED' && <p className="alert alert-danger">{report.provider === 'mock' ? '当前为离线 Mock，真实语义分析未验收。' : '语义分析或制度核验尚未成功。'}不能据此批准。</p>}
    <div className="p4b-report-grid"><aside>
      <EmployeeStayVerification key={`${item.claim_id}:${item.revision}`} item={item} finance={finance} refresh={refresh} />
      <h4>申请与证据</h4><p>{item.fields.applicant_name} · 职级 {report.trusted_facts.grade} · {item.fields.city}（{report.trusted_facts.city_tier} 类）</p>
      <p>{item.fields.purpose}</p><p>{item.fields.notes || '无补充说明'}</p><small>员工陈述是待核实信息。</small>
      <p>发票：{item.invoice_evidence ? item.invoice_evidence.attachment.file_name : '待提供或人工核对'}</p>
      {verification?.verification && <details><summary>最近一次人工核实 · 第 {verification.fact_revision} 版</summary>
        <p>{verification.verification.source_reference}</p><p>{verification.verification.basis}</p>
        <p>核实人 {verification.verification.actor_id} · {verification.verification.at}</p>
        {Object.entries(verification.verification.facts).map(([k, v]) => <p key={k}>{BOOLS.find(([key]) => key === k)?.[1] ?? k}：{String(v)}</p>)}</details>}
      {report.missing_evidence.length > 0 && <><h4>待补充证据</h4><ul>{report.missing_evidence.map((v, i) => <li key={i}>{v === 'invoice_verified' ? '缺少票据独立核验（invoice_verified）' : v}</li>)}</ul></>}
    </aside><div>
      <h4>1 · 规则计算</h4>{report.rule_results.map(r => <div key={r.rule_id} className="portal-reason">
        <strong>{RULE_CHECK_LABEL[r.outcome] ?? '待核实'} · {r.reason}</strong><p>申报 {r.actual ?? '未知'} 元 · 限额 {r.limit ?? '待核实'} 元
          {r.actual !== null && r.limit !== null && ` · 超标 ${Math.max(0, Number(r.actual) - Number(r.limit)).toFixed(2)} 元`}</p></div>)}
      {advisory ? <AdvisoryReport item={item} /> : <>
      <h4>2 · AI 语义审核</h4><small>{report.provider} · {report.model} · {report.analysis_status}</small><p>{report.explanation}</p>
      {report.semantic_assessment && <p>AI 建议风险：{report.semantic_assessment.suggested_risk} · 建议处理：{STATUS[report.semantic_assessment.recommended_action] ?? report.semantic_assessment.recommended_action}</p>}
      {report.semantic_coverage?.protocol === 'p4b.semantic.2' && <details><summary>查看逐项检查覆盖</summary>
        {Object.entries(report.semantic_coverage.model).map(([topic, state]) => <p key={topic}>{TOPICS[topic] ?? topic}：{CHECKS[state] ?? state}</p>)}
        <p>程序另行核对必要证据、确定性规则和硬性门禁；检查通过仍须财务最终批准。</p></details>}
      {report.citation_bindings?.filter(e => e.finding_id === 'REVIEW').map(e => <details key={e.chunk_id}><summary>{e.section} · {e.clause_id}</summary><p>{e.source_text}</p></details>)}
      {report.semantic_findings.map(f => <article key={f.finding_id}><strong>{STATUS[f.conclusion] ?? f.conclusion} · {f.issue_type}</strong>
        <p>{f.explanation}</p>{f.uncertainty && <p>待核实：{f.uncertainty}</p>}<small>依据：{f.evidence_refs.map(id => report.supporting_evidence.find(e => e.chunk_id === id)?.clause_id ?? id).join('、')}</small>
        {f.evidence_refs.map(id => { const e = report.supporting_evidence.find(item => item.chunk_id === id); return e && <details key={id}><summary>{e.section} · {e.clause_id}</summary><p>{e.source_text}</p></details>; })}</article>)}
      {report.exceptions.map((e, i) => <p key={i}>例外事项：{e}</p>)}{report.conflicts.map((e, i) => <p className="alert alert-danger" key={i}>冲突：{e}</p>)}
      </>}
      <details className="p4b-policy-details"><summary><strong>3 · 制度依据</strong><span>{report.supporting_evidence.length} 段原文 · 222.docx</span></summary>
        <p>{report.policy_title ?? '222.docx'} · 版本 {item.ai_result?.policy_evidence.find(e => e.policy_version_id === report.policy_version_id)?.version ?? '待核对'} · {report.policy_version_id ?? '有效版本未确定'}</p>
        <details><summary>查看实际检索原文（{report.supporting_evidence.length} 段）</summary>{report.supporting_evidence.map(e => <blockquote key={e.chunk_id}>
          <strong>{e.section} · {e.clause_id}</strong><p>{e.source_text}</p><small>生效 {e.effective_from} 至 {e.effective_to ?? '未注明'} · XML 段落 {e.source_position.body_index}
            {e.source_position.table_index && ` · 表 ${e.source_position.table_index} / 行 ${e.source_position.row_index}`}</small><br />
          <button type="button" className="btn btn-sm" onClick={() => void openSource(e)}>核对原文来源</button></blockquote>)}</details>
        {source && <div className="portal-reason"><strong>已重新核对原文 · {source.clause_id}</strong><p>{source.source_text}</p><small>文档 SHA-256 {source.document_sha256}</small></div>}
      </details>
      <h4>4 · 最终处理</h4>{!advisory && <p>合规检查：{STATUS[report.compliance_status]} · 建议：{STATUS[report.recommended_action]}</p>}
      <p>财务状态：{STAGE_LABEL[item.stage] ?? '状态待确认'} · ERP Mock：{ERP_STATUS_LABEL[item.erp_sync_status as keyof typeof ERP_STATUS_LABEL] ?? '状态待确认'}</p><p>所有申请均须财务最终审核；补证后生成新的审核记录，历史报告保留。</p>
    </div></div>
    {finance && ['FINANCE_REVIEW', 'RETURNED'].includes(item.stage) && <details><summary>记录独立核实依据并重新审核</summary><form onSubmit={verify}>
      <fieldset disabled={busy || !!pending}><div className="submission-fields">{BOOLS.map(([key, label]) => <label className="field" key={key}>{label}<select className="select" value={facts[key] ?? ''} onChange={e => setFacts(v => ({ ...v, [key]: e.target.value }))}><option value="">未核实 / 保留原记录</option><option value="true">已核实符合</option><option value="false">已核实不符合</option></select></label>)}
        {[['participants', '参与人数'], ['full_days', '经核实完整公务日'], ['lunches', '客户等提供午餐次数'], ['dinners', '客户等提供晚餐次数']].map(([key, label]) => <label key={key} className="field">{label}<input className="input" type="number" min={key === 'participants' || key === 'full_days' ? 1 : 0} max={key === 'participants' ? 1000 : 366} step={1} value={facts[key] ?? ''} onChange={e => setFacts(v => ({ ...v, [key]: e.target.value }))} /></label>)}
        <label className="field">招待类别<select className="select" value={facts.entertainment_category ?? ''} onChange={e => setFacts(v => ({ ...v, entertainment_category: e.target.value }))}><option value="">未核实</option><option value="GENERAL">一般客户 / 供应商工作餐</option><option value="MAJOR">重大项目 / 战略客户</option><option value="REFRESHMENT">会议茶歇</option><option value="GIFT">礼品</option></select></label></div>
        <label className="label">原始记录或独立证据编号<input className="input" required maxLength={1000} value={reference} onChange={e => setReference(e.target.value)} /></label>
        <label className="label">核实依据与结论<textarea className="textarea" required maxLength={2000} value={basis} onChange={e => setBasis(e.target.value)} /></label>
      </fieldset><button className="btn" disabled={busy || !verification}>{busy ? '正在记录与重审…' : pending ? '重试同一核实与重审' : '确认核实并重新审核'}</button></form></details>}
    {error && <p className="alert alert-danger" role="alert">{error}</p>}
  </section>;
}
