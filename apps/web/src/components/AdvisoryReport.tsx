import type { MouseEvent } from 'react';
import type { PortalClaim } from '../lib/api/enterprise';

const LABEL: Record<string, string> = {
  RECOMMEND_APPROVAL: '建议通过', RECOMMEND_REJECTION: '建议不通过', REQUEST_EVIDENCE: '建议退回补充资料',
  ESCALATE: '建议进一步复核', UNDETERMINED: '暂无法给出建议', LOW: '低风险', MEDIUM: '中风险', HIGH: '高风险',
  INFO: '一般提示', WARNING: '风险提示', BLOCKER: '阻断问题',
  VERIFIED: '已核实', DECLARED: '员工申报', MISSING: '缺少资料', CONFLICTED: '信息存在冲突',
  employee: '员工档案', grade: '员工职级', city_tier: '城市类别', stay: '住宿记录',
  expense_type: '费用类型', category_mapping: '费用归类', policy_version: '适用制度',
  original_invoice: '发票原件', invoice_verified: '发票真实性', business_related: '业务相关性',
  prior_approval: '事前批准', duplicate_clear: '重复报销', rule_results: '额度计算',
  customer_identified: '客户身份', exception_authorized: '例外授权', itinerary_verified: '行程记录',
  cancellation_notice: '航班取消证明', participants: '参与人数', full_days: '公务天数',
  lunches: '已提供午餐', dinners: '已提供晚餐', entertainment_category: '招待类别',
  applicant_name: '申请人', department: '部门', position: '岗位', category: '费用类型', nights: '住宿晚数', amount: '申报金额', city: '城市',
  incurred_on: '费用发生日期', trip_start: '入住 / 行程开始', trip_end: '退房 / 行程结束',
  purpose: '报销事项', notes: '补充说明', invoice_number: '发票号码', seller: '酒店 / 销售方',
  declared_purpose: '员工申报的报销事项', declared_notes: '员工补充说明',
  total_amount: '发票金额', seller_name: '发票销售方', buyer_name: '发票购买方', issue_date: '开票日期',
};

function textValue(value: unknown): string {
  if (value == null || value === '') return '未提供';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'object') return JSON.stringify(value, null, 2);
  return String(value);
}

function revealSource(event: MouseEvent<HTMLAnchorElement>) {
  let node = document.getElementById(event.currentTarget.getAttribute('href')?.slice(1) ?? '');
  while (node) {
    if (node instanceof HTMLDetailsElement) node.open = true;
    node = node.parentElement;
  }
}

export default function AdvisoryReport({ item }: { item: PortalClaim }) {
  const report = item.advisory_analysis;
  if (!report) return null;
  const currentData = report.analysis_basis === 'CURRENT_DATA_WITH_RAG';
  const entries = Object.entries(report.positive_evidence.entries ?? {})
    .filter(([key, value]) => !currentData || (key !== 'rule_results'
      && ['VERIFIED', 'DECLARED'].includes(value.status)));
  const candidate = report.input_facts?.candidate ?? item.fields;
  const fields = ['applicant_name', 'department', 'position', 'category', 'amount', 'seller', 'city', 'incurred_on', 'trip_start', 'trip_end', 'nights', 'purpose', 'notes'];
  const sourceId = (id: string) => `advice-source-${item.claim_id}-${id}`;
  const factId = (ref: string) => sourceId(ref.startsWith('positive.') ? ref : ref.startsWith('account.')
    ? ref.replace('account.', 'positive.') : ref.startsWith('invoice.') ? 'invoice'
    : ref.startsWith('candidate.') ? 'candidate' : 'rules');
  const factLabel = (ref: string) => LABEL[ref.replace(/^(positive|candidate|account|invoice)\./, '')]
    ?? (ref === 'declared_amount_check' ? '按申报计算的额度' : ref);
  const risk = ['LOW', 'MEDIUM', 'HIGH'].includes(report.risk_level) ? LABEL[report.risk_level] : '风险暂无法判断';
  return <>
    <section aria-label="详细审核建议">
      <h4>1 · 详细审核建议</h4>
      {report.model.startsWith('mock-') && <p className="portal-advice-meta">Mock AI · 合成审核建议</p>}
      {report.model.startsWith('deepseek-') && <p className="portal-advice-meta">AI 模型：DeepSeek · {report.model}</p>}
      <div className={`portal-ai-conclusion risk-${report.risk_level.toLowerCase()}`}>
        <div className="portal-ai-conclusion-heading"><span>AI 对本单的结论</span><strong>{risk} · {LABEL[report.recommendation] ?? '暂无法给出建议'}</strong></div>
        <p>{report.ai_advisory_status === 'SUCCEEDED' ? currentData ? report.summary : '以下为 AI 审核建议，最终处理由财务决定。'
          : report.ai_advisory_status === 'PARTIAL' ? '部分建议未通过校验，以下仅展示可核验的内容，暂无法形成完整结论。'
          : report.ai_advisory_status === 'NOT_RUN' ? '本次 AI 尚未运行，暂没有可用的详细审核建议。'
          : '本次 AI 分析失败或依据不足，暂没有可用的完整审核建议。'}</p>
        {currentData && <p>依据现有申报、发票信息与制度分析；是否核实、通过或退回，由财务决定。</p>}
      </div>
      {report.issues.map((issue, index) => <article className="portal-advice-issue" key={index}>
        <div className="portal-advice-issue-title"><strong>{index + 1}. {LABEL[issue.classification ?? 'WARNING']}</strong><span>{LABEL[issue.severity] ?? issue.severity}</span></div>
        <p>{issue.explanation}</p>
        {issue.finance_check && <p className="portal-advice-followup"><b>建议：</b>{issue.finance_check}</p>}
        {issue.unverified_question && <p>待明确的问题：{issue.unverified_question}{issue.source_quote && <>（员工原文：“{issue.source_quote}”）</>}</p>}
        <div className="portal-advice-references" aria-label={`第 ${index + 1} 条建议的数据出处`}>
          {issue.fact_refs.map(ref => <a href={`#${factId(ref)}`} onClick={revealSource} key={ref}>{factLabel(ref)}</a>)}
          {(issue.rule_refs ?? []).length > 0 && <a href={`#${sourceId('rules')}`} onClick={revealSource}>额度计算依据</a>}
          {issue.policy_ref_ids.map(id => {
            const source = report.supporting_evidence.find(e => e.chunk_id === id);
            return source && <a href={`#${sourceId(id)}`} onClick={revealSource} key={id}>{source.section} · {source.clause_id}</a>;
          })}
        </div>
      </article>)}
      {report.ai_advisory_status === 'SUCCEEDED' && report.issues.length === 0 && <p className="portal-advice-meta">AI 未列出其他详细审核事项。</p>}
      {report.missing_evidence.length > 0 && <p className="portal-advice-followup">AI 建议补充：{report.missing_evidence.map(key => LABEL[key] ?? key).join('、')}</p>}
    </section>
    <section className="portal-advice-sources" aria-label="原始数据与出处">
      <h4>2 · 原始数据与出处</h4>
      <article><details id={sourceId('candidate')} className="portal-advice-source-group"><summary><strong>员工原始申报</strong><span>第 {item.version} 次提交</span></summary>
        <dl className="portal-advice-data">{fields.map(key => <div key={key}><dt>{LABEL[key]}</dt><dd>{textValue((candidate as Record<string, unknown>)[key])}</dd></div>)}</dl>
        <small>来源：本单提交记录；员工申报内容保留原始状态。</small>
      </details></article>
      <article id={sourceId('invoice')}><h5>发票原件</h5>{item.invoice_evidence ? <>
        <p>{item.invoice_evidence.attachment.file_name}</p>
        <a href="#portal-invoice-source" onClick={() => {
          const details = document.getElementById('portal-invoice-source');
          if (details instanceof HTMLDetailsElement) details.open = true;
        }}>查看发票原件与确认数据 →</a>
      </> : <p>本次提交未附发票原件。</p>}</article>
      {entries.length > 0 && <article><details className="portal-advice-source-group"><summary><strong>本次审核采用的事实与来源</strong><span>{entries.length} 项</span></summary>
        <div className="portal-advice-facts">{entries.map(([key, value]) => <details id={sourceId('positive.' + key)} key={key}>
          <summary>{LABEL[key] ?? key}<span>{LABEL[value.status] ?? value.status}</span></summary>
          <p className="portal-advice-value">{textValue(value.value)}</p><small>原始记录：{value.source_reference ?? '没有可用的独立来源'}</small>
        </details>)}</div>
      </details></article>}
      <article id={sourceId('rules')}><h5>额度计算采用的数据</h5>
        {currentData && report.declared_amount_check ? <>
          <p>申报 {report.declared_amount_check.claimed_amount} 元 · 适用限额 {report.declared_amount_check.total_limit ?? '未确定'} 元</p>
          <p>{report.declared_amount_check.reason}</p>
          <small>晚数和日期来自员工申报；此项为金额比较，住宿真实性由财务判断。</small>
        </> : <>{(report.rule_results ?? item.compliance_analysis?.rule_results ?? []).map(rule => <p key={rule.rule_id}>
          申报 {rule.actual ?? '未确定'} 元 · 适用限额 {rule.limit ?? '未确定'} 元
        </p>)}
        <small>来源：本次审核绑定的制度额度与申报金额。</small></>}
      </article>
      <article><details className="portal-advice-source-group"><summary><strong>本次提供给 AI 的制度原文</strong><span>{report.supporting_evidence.length} 段</span></summary>
        {report.supporting_evidence.length === 0 && <p>本次没有可用的制度原文。</p>}
        <div className="portal-advice-policy-list">{report.supporting_evidence.map(e => <details id={sourceId(e.chunk_id)} key={e.chunk_id}>
          <summary>{e.section} · {e.clause_id}</summary>
          <blockquote>{e.source_text}</blockquote>
          <p className="portal-advice-position">原文位置：{item.compliance_analysis?.policy_title ?? '制度文档'} · {e.source_position.part} · 段落 {e.source_position.body_index}
            {e.source_position.table_index != null && <> · 表 {e.source_position.table_index}</>}
            {e.source_position.row_index != null && <> · 行 {e.source_position.row_index}</>}</p>
          <small>制度生效：{e.effective_from} 至 {e.effective_to ?? '未注明'}<br />制度版本：{e.policy_version_id}<br />文档 SHA-256：{e.document_sha256}</small>
        </details>)}</div>
      </details></article>
    </section>
  </>;
}
