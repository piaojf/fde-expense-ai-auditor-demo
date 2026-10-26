import { useEffect, useState } from "react";
import { ENTERPRISE_MODE } from '../lib/api/enterprise';
import { useAuth } from '../lib/session/AuthContext';
import type { PolicyUpload } from "../lib/api/policies";
import {
  operationKey, publicationHttp, ruleMessage,
  type PublicationHistory, type ReleasePreview, type RuleData, type PublicationEvent,
} from "../lib/api/publication";

export default function PolicyPublicationPanel({ token, policy, prepareForPublication, canPrepare }: {
  token: string; policy: PolicyUpload; prepareForPublication: () => Promise<void>; canPrepare: boolean;
}) {
  const [data, setData] = useState<RuleData | null>(null);
  const [history, setHistory] = useState<PublicationHistory>({ events: [], versions: [] });
  const { user } = useAuth();
  const [actor, setActor] = useState(ENTERPRISE_MODE ? user?.name ?? '' : "本机管理员");
  const [reason, setReason] = useState("");
  const [amount, setAmount] = useState("");
  const [candidateIndex, setCandidateIndex] = useState(0);
  const [sourceMode, setSourceMode] = useState<'AUTOMATIC' | 'MANUAL'>('AUTOMATIC');
  const [sourceStart, setSourceStart] = useState(0);
  const [sourceEnd, setSourceEnd] = useState(0);
  const [conditions, setConditions] = useState('');
  const [ruleChecked, setRuleChecked] = useState(false);
  const [ruleEdited, setRuleEdited] = useState(false);
  const [publishChecked, setPublishChecked] = useState(false);
  const [predecessor, setPredecessor] = useState("");
  const [preview, setPreview] = useState<ReleasePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [kind, setKind] = useState("ROLLBACK");
  const [target, setTarget] = useState("");
  const [restore, setRestore] = useState("");
  const [effective, setEffective] = useState("");
  const [lifecyclePreview, setLifecyclePreview] = useState<ReleasePreview | null>(null);
  const [lifecycleChecked, setLifecycleChecked] = useState(false);
  const published = !!data?.publications.length;
  const sourceLines = policy.parsed.lines ?? [];
  const manual = ENTERPRISE_MODE && sourceMode === 'MANUAL';
  const firstLine = sourceLines[sourceStart];
  const lastLine = sourceLines[sourceEnd];
  const manualQuote = firstLine && lastLine && sourceEnd >= sourceStart ? (policy.parsed.text ?? '').slice(firstLine.start, lastLine.end) : '';
  const candidate = manual && firstLine && lastLine ? { start: firstLine.start, end: lastLine.end, quote: manualQuote, amount, currency: 'CNY', source: firstLine } : data?.extraction.candidates[candidateIndex];
  const latest = data?.reviews[data.reviews.length - 1];
  const referenceOnly = latest?.confirmed_rule.execution_status === 'REFERENCE_ONLY';
  const originalRegion = policy.parsed.extracted_metadata?.region?.value;
  const ruleRegion = ENTERPRISE_MODE ? latest?.confirmed_rule.rule_scope?.region ?? originalRegion : policy.metadata?.region;
  const knownRuleScope = !ENTERPRISE_MODE || (!!ruleRegion && !['UNKNOWN', '*', 'ALL', '全部', '全国', '全部地区'].includes(ruleRegion) && policy.parsed.extracted_metadata?.expense_type?.value === 'HOTEL');
  const inScope = history.versions.filter(p => p.legal_entity === policy.metadata?.legal_entity && p.region === ruleRegion && p.expense_type === "HOTEL");

  async function refresh() {
    const [rules, h] = await Promise.all([
      publicationHttp<RuleData>(token, `/${policy.upload_id}/rules`),
      publicationHttp<PublicationHistory>(token, "/history"),
    ]);
    setData(rules); setHistory(h);
    setAmount(rules.reviews[rules.reviews.length - 1]?.confirmed_rule.amount ?? rules.extraction.candidates[candidateIndex]?.amount ?? "");
    const own = rules.publications[rules.publications.length - 1]?.policy?.policy_version_id;
    if (own) setTarget(own);
  }
  useEffect(() => { let alive = true;
    Promise.all([publicationHttp<RuleData>(token, `/${policy.upload_id}/rules`), publicationHttp<PublicationHistory>(token, "/history")])
      .then(([d, h]) => { if (alive) {
        setData(d); setHistory(h);
        const review = d.reviews[d.reviews.length - 1];
        setAmount(review?.confirmed_rule.amount ?? d.extraction.candidates[0]?.amount ?? "");
        setTarget(d.publications[d.publications.length - 1]?.policy?.policy_version_id ?? "");
        if (ENTERPRISE_MODE) {
          setSourceMode(review?.confirmed_rule.source_mode ?? (d.extraction.candidates.length ? 'AUTOMATIC' : 'MANUAL'));
          setConditions(review?.confirmed_rule.conditions ?? '');
          const start = review?.confirmed_rule.clause.start;
          const end = review?.confirmed_rule.clause.end;
          const index = start !== undefined ? sourceLines.findIndex(l => l.start === start) : sourceLines.findIndex(l => /住宿|酒店/.test(l.text));
          const from = Math.max(0, index);
          setSourceStart(from);
          setSourceEnd(end !== undefined ? Math.max(from, sourceLines.findIndex(l => l.end === end)) : from);
        }
      } })
      .catch(e => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [token, policy.upload_id, policy.state]);
  useEffect(() => { setPreview(null); setPublishChecked(false); setRuleChecked(false); }, [predecessor, amount, candidateIndex, sourceMode, sourceStart, sourceEnd, conditions]);
  useEffect(() => { setLifecyclePreview(null); setLifecycleChecked(false); }, [target, restore, kind, effective]);
  async function act(work: () => Promise<void>) {
    setBusy(true); setError(""); setNotice("");
    try { await work(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  async function confirmRule() {
    if (!candidate || !ruleChecked || !actor.trim() || !reason.trim()) { setError("请填写操作者、修改说明并确认原文来源"); return; }
    await act(async () => {
      const review = await publicationHttp<RuleData['reviews'][number]>(token, `/${policy.upload_id}/confirm-rule`, { actor, reason, confirmed: true, amount, currency: "CNY", clause_start: candidate.start, clause_end: candidate.end, expected_revision: latest?.revision ?? 0, ...(ENTERPRISE_MODE ? { source_mode: sourceMode, conditions } : {}) });
      await refresh(); setPreview(null); setPublishChecked(false); setRuleChecked(false); setRuleEdited(false);
      setNotice(review.confirmed_rule.execution_status === 'REFERENCE_ONLY' ? '原文规则草稿已保存，尚有待支持或核对的条件；未生效，不影响报销审核。' : '结构化规则已人工确认；尚未发布，不影响审批。');
    });
  }
  async function publicationPreview() {
    await act(async () => {
      if (policy.state !== "INDEXED") await prepareForPublication();
      setPreview(await publicationHttp<ReleasePreview>(token, `/${policy.upload_id}/publication-preview`, { predecessor_id: predecessor || null }));
      setPublishChecked(false);
    });
  }
  async function publish() {
    if (!preview?.preview_digest || !publishChecked || !actor.trim()) { setError("请先查看差异并再次确认发布"); return; }
    await act(async () => {
      const r = await publicationHttp<PublicationEvent>(token, `/${policy.upload_id}/publish`, { predecessor_id: predecessor || null, actor, confirmed: true, preview_digest: preview.preview_digest, idempotency_key: operationKey(preview.preview_digest!) });
      await refresh(); setNotice(`制度 ${r.policy?.version} 已正式发布，自 ${r.policy?.effective_from} 起按费用发生日期适用，报销仍需满足其他硬性条件。`);
      setPublishChecked(false); setPreview(null);
    });
  }
  const lifecycleBody = { kind, effective_on: effective, restore_id: kind === "ROLLBACK" ? restore : null };
  function editSource(start: number, end: number) {
    setSourceStart(start); setSourceEnd(end); setAmount(''); setRuleEdited(true);
  }
  async function previewLifecycle() {
    await act(async () => { setLifecyclePreview(await publicationHttp<ReleasePreview>(token, `/${target}/lifecycle-preview`, lifecycleBody)); setLifecycleChecked(false); });
  }
  async function changeLifecycle() {
    if (!lifecyclePreview?.preview_digest || !lifecycleChecked || !reason.trim()) { setError("请填写变更原因并再次确认未来范围"); return; }
    await act(async () => {
      await publicationHttp(token, `/${target}/lifecycle`, { ...lifecycleBody, actor, reason, confirmed: true, preview_digest: lifecyclePreview.preview_digest, idempotency_key: operationKey(lifecyclePreview.preview_digest!) });
      await refresh(); setLifecycleChecked(false); setLifecyclePreview(null); setNotice("未来退休或回退已记录；原制度和历史审批保留。");
    });
  }
  return <section className="card publication-panel" aria-label="规则审核与制度发布">
    <div className="publication-eyebrow">第二步</div>
    <h2>人工确认审核规则</h2>
    <p>规则来自本次上传的制度，请核对原文再确认。当前支持执行具体城市的单一每晚住宿限额；职级、城市分级等复杂条件可先保存为参考草稿。</p>
    {error && <div role="alert" className="alert alert-error">{error}</div>}
    {notice && <div role="status" className="alert alert-success">{notice}</div>}
    <div className="policy-fields">
      <label>{ENTERPRISE_MODE ? '确认与发布人员' : '确认与发布人员（演示身份）'}<input aria-label="发布操作者" value={actor} onChange={e => setActor(e.target.value)} disabled={busy || ENTERPRISE_MODE} maxLength={128} /></label>
      <label>规则修正 / 版本变更原因<input aria-label="规则变更原因" value={reason} onChange={e => setReason(e.target.value)} disabled={busy} maxLength={1000} /></label>
    </div>
    {data && <>
      {ENTERPRISE_MODE && <div role="status" className="alert alert-info">
        <strong>文档已保存 · {published ? '规则已发布，按发布范围及费用发生日期适用' : referenceOnly ? '规则草稿待处理' : latest ? '规则已确认，尚未发布' : '规则待核对'}</strong>
        <p>上传文件、保存规则草稿和正式发布分别记录。只有正式发布且在适用日期内的规则才参与审核。</p>
      </div>}
      <details><summary>查看规则来源校验信息</summary><p>来源文件校验值：<code className="publication-hash">{policy.sha256}</code></p></details>
      <p>审核规则：每晚住宿限额 · 人民币</p>
      {ENTERPRISE_MODE && <p>本次审核规则适用范围：{knownRuleScope ? `${ruleRegion} · 酒店住宿` : '待原文明确具体地区及酒店住宿类型'}。文档选择“全部”不会将此住宿限额用于其他地区或费用。</p>}
      {!knownRuleScope && <div role="alert" className="alert alert-danger">原文缺少可核验的具体规则范围，可保存和检索制度文档；请补充规则适用地区、费用类型后再发布审核规则。</div>}
      {!!data.extraction.blockers.length && <div role="alert" className="alert alert-warning"><strong>自动识别待核对</strong><ul>{data.extraction.blockers.map(code => <li key={code}>{ruleMessage(code)}</li>)}</ul>{ENTERPRISE_MODE && <p>可在下方引用原文补录；识别警告会保留在核对记录中。</p>}</div>}
      {ENTERPRISE_MODE && data.condition_warning && <p className="alert alert-warning">{ruleMessage('RULE_CONDITIONS_UNSUPPORTED')}</p>}
      {ENTERPRISE_MODE && <label className="policy-query">规则录入方式<select aria-label="规则录入方式" value={sourceMode} disabled={busy || published} onChange={e => { setSourceMode(e.target.value as typeof sourceMode); setAmount(e.target.value === 'AUTOMATIC' ? data.extraction.candidates[candidateIndex]?.amount ?? '' : ''); setRuleEdited(true); }}>
        <option value="AUTOMATIC">核对自动识别结果</option><option value="MANUAL">引用原文补录</option>
      </select></label>}
      {manual && <>
        {!!data.reference_tables?.length && <details open><summary>住宿表格原文（{data.reference_tables.length} 行）</summary><p>这些是原文参考行，包含表头和不同标准，尚未成为自动审核规则。</p><div className="policy-reference-rows">{data.reference_tables.map(row => <div className="policy-reference-row" key={row.start}><div><small>段落 {row.line}–{row.end_line}</small><p>{row.cells.join(' · ')}</p></div><button type="button" className="btn btn-secondary" disabled={busy || published} onClick={() => editSource(Math.max(0, sourceLines.findIndex(l => l.start === row.start)), Math.max(0, sourceLines.findIndex(l => l.end === row.end)))}>引用此行</button></div>)}</div></details>}
        <div className="policy-fields">
          <label>原文起始段落<select aria-label="原文起始段落" value={sourceStart} disabled={busy || published} onChange={e => { const start = Number(e.target.value); editSource(start, Math.max(start, sourceEnd)); }}>{sourceLines.map((line, i) => <option value={i} key={line.start}>段落 {line.line} · {line.text.slice(0, 45)}</option>)}</select></label>
          <label>原文结束段落<select aria-label="原文结束段落" value={sourceEnd} disabled={busy || published} onChange={e => editSource(sourceStart, Number(e.target.value))}>{sourceLines.map((line, i) => i >= sourceStart && <option value={i} key={line.start}>段落 {line.line} · {line.text.slice(0, 45)}</option>)}</select></label>
        </div>
        <p>选择连续原文段落，可包含表头及单位说明，最多 2000 字。金额须出现在引用中。</p>
      </>}
      {!manual && <label className="policy-query">原文规则条款<select aria-label="原文规则条款" value={candidateIndex} disabled={busy || published} onChange={e => { const i = Number(e.target.value); setCandidateIndex(i); setAmount(data.extraction.candidates[i]?.amount ?? ""); setRuleChecked(false); setRuleEdited(true); }}>
        {!data.extraction.candidates.length && <option value="">暂无可核对的自动识别结果</option>}
        {data.extraction.candidates.map((c, i) => <option key={i} value={i}>段落 {c.source.line} · {c.amount} 元/晚</option>)}
      </select></label>}
      {candidate && <div className="policy-clause"><pre>{candidate.quote.slice(0, 2000)}</pre><small>字符 {candidate.start}–{candidate.end} · {candidate.source.method} · {manual ? `原文引用 ${manualQuote.length} 字` : `原始提取 ${candidate.amount} CNY`}</small></div>}
      <label className="policy-query">{manual ? '原文住宿标准金额（元，单位须对照原文）' : '每晚住宿限额（元）'}<input aria-label="确认每晚住宿限额" inputMode="decimal" value={amount} disabled={busy || published} onChange={e => { setAmount(e.target.value); setRuleChecked(false); setRuleEdited(true); }} /></label>
      {ENTERPRISE_MODE && <label className="policy-query">适用条件 / 待处理说明<input aria-label="规则适用条件" value={conditions} maxLength={1000} disabled={busy || published} placeholder="例如：D 级员工；有条件的标准将保存为参考草稿" onChange={e => { setConditions(e.target.value); setRuleEdited(true); }} /></label>}
      <p>修正金额必须有上述原文条款作为依据。无法可靠确认的规则不能发布。</p>
      <label className="policy-check"><input type="checkbox" checked={ruleChecked} disabled={busy || published} onChange={e => setRuleChecked(e.target.checked)} />我已对照原文核对范围、日期、金额、单位及适用条件，确认保存本次核对记录</label>
      <button className="btn btn-primary" disabled={busy || published || !policy.metadata || !candidate || !ruleChecked || !actor.trim() || !reason.trim() || !knownRuleScope || !amount.trim() || (manual ? !manualQuote || manualQuote.length > 2000 : !!data.extraction.blockers.length)} onClick={confirmRule}>{manual || (ENTERPRISE_MODE && data.condition_warning) ? '保存原文规则草稿' : '确认审核规则'}</button>
      {referenceOnly && <div className="alert alert-warning"><strong>已保存参考草稿，暂不能发布</strong><ul>{latest?.confirmed_rule.execution_blockers?.map(code => <li key={code}>{ruleMessage(code)}</li>)}</ul><p>原文件和核对记录已保留。待补齐依据或系统支持相应条件后，再重新核对。</p></div>}
      {!policy.metadata && <p>请先确认上方的制度信息，再确认审核规则。</p>}
      {!!data.reviews.length && <details><summary>规则确认与修改记录（{data.reviews.length}）</summary>{data.reviews.map(r => <p key={r.review_id}>修订 {r.revision} · {r.confirmed_rule.amount} 元 · {r.confirmed_rule.execution_status === 'REFERENCE_ONLY' ? '参考草稿' : '审核规则已确认'} · {r.actor} · {r.recorded_at}<br />{r.reason}{r.confirmed_rule.conditions && <><br />适用条件：{r.confirmed_rule.conditions}</>}<br />原始提取及元数据修正：{JSON.stringify(r.confirmed_rule.metadata_changes)}</p>)}</details>}
      {!published && <>
        <div className="publication-stage"><div className="publication-eyebrow">第三步</div><h2>正式发布制度</h2><p>先核对新旧版本与生效范围，再确认发布。未发布的草稿不参与报销审核。</p></div>
        <label className="policy-query">明确替代的旧制度<select aria-label="明确替代的旧制度" value={predecessor} disabled={busy} onChange={e => setPredecessor(e.target.value)}>
          <option value="">无旧制度（范围内不得重叠）</option>{inScope.map(p => <option key={p.policy_version_id} value={p.policy_version_id}>{p.version} · {p.amount} 元/晚 · {p.policy_version_id}</option>)}
        </select></label>
        <button className="btn btn-secondary" disabled={busy || !latest || referenceOnly || ruleEdited || (policy.state !== "INDEXED" && !canPrepare)} onClick={publicationPreview}>查看发布前差异</button>
        {ruleEdited && <p>规则内容已修改，请重新确认审核规则后再查看发布差异。</p>}
        {preview && <div className="publication-preview" data-testid="publication-preview">
          <h3>发布前预览</h3>
          {!!preview.blockers.length ? <div role="alert" className="alert alert-danger">拒绝发布：{preview.message} · {preview.blockers.join(" / ")}</div> : <>
            <div className="publication-diff"><div><small>当前有效制度</small><strong>{preview.current?.amount ?? "UNKNOWN"} 元/晚</strong><span>旧版本 {preview.old?.version ?? "无"}</span></div><div><small>待发布制度</small><strong>{preview.new?.amount} 元/晚</strong><span>新版本 {preview.new?.version}</span></div></div>
            <p>生效日期 {preview.new?.effective_from} · 失效日期 {preview.new?.effective_to ?? "无"}</p>
            <p>影响范围：{preview.impact?.legal_entity} / {preview.impact?.region} / {preview.impact?.expense_type}；住宿规则 {preview.old?.amount ?? "UNKNOWN"} → {preview.new?.amount} CNY/晚。</p>
            <p>历史审批与正在复核的制度依据保留。金额合规的报销仍需满足其他硬性条件。</p>
            <p>发布校验：无冲突或缺失项。当前检索模式见下方条款检索设置。</p>
            <label className="policy-check"><input type="checkbox" checked={publishChecked} disabled={busy} onChange={e => setPublishChecked(e.target.checked)} />我已核对差异、替代版本及生效范围，确认正式发布</label>
            <button className="btn btn-primary" disabled={busy || !publishChecked} onClick={publish}>正式发布制度</button>
          </>}
        </div>}
      </>}
      {published && <div className="policy-confirmed">此制度已正式发布，按生效日期适用。原始文件、规则确认记录与历史版本均保留。</div>}
    </>}
    <details className="publication-history"><summary>版本发布审计与未来退休 / 回退</summary>
      {history.events.map(e => <article className="policy-clause" key={e.event_id}><strong>{e.kind} · {e.policy?.version ?? "退休"}</strong><p>{e.policy?.amount && `${e.policy.amount} 元/晚 · `}生效 {e.policy?.effective_from ?? e.effective_on}<br />{e.actor} · 发布于 {e.recorded_at}<br />版本 {e.policy?.policy_version_id ?? e.predecessor_id} · 审计 {e.event_id}</p></article>)}
      {!history.events.length && <p>尚无发布审计。</p>}
      <div className="policy-fields">
        <label>目标版本<select aria-label="退休回退目标版本" value={target} onChange={e => setTarget(e.target.value)} disabled={busy}><option value="">请选择</option>{inScope.map(p => <option key={p.policy_version_id} value={p.policy_version_id}>{p.version} · {p.amount} 元/晚</option>)}</select></label>
        <label>变更类型<select aria-label="版本变更类型" value={kind} onChange={e => setKind(e.target.value)} disabled={busy}><option value="ROLLBACK">受控回退（新建未来版本）</option><option value="RETIRE">退休（未来安全转 UNKNOWN）</option></select></label>
        {kind === "ROLLBACK" && <label>历史来源<select aria-label="回退来源版本" value={restore} onChange={e => setRestore(e.target.value)} disabled={busy}>{inScope.map(p => <option key={p.policy_version_id} value={p.policy_version_id}>{p.version} · {p.amount} 元/晚</option>)}</select></label>}
        <label>未来生效日期<input aria-label="退休回退生效日期" type="date" value={effective} onChange={e => setEffective(e.target.value)} disabled={busy} /></label>
      </div>
      <button className="btn btn-secondary" disabled={busy || !target || !effective} onClick={previewLifecycle}>预览退休或回退</button>
      {lifecyclePreview && <div className="publication-preview">{!!lifecyclePreview.blockers.length ? <div role="alert" className="alert alert-danger">{lifecyclePreview.message} · {lifecyclePreview.blockers.join(" / ")}</div> : <>
        <p>从 {lifecyclePreview.effective_on} 起，{lifecyclePreview.target?.amount} 元/晚 → {lifecyclePreview.restore?.amount ?? "UNKNOWN"}。不改写历史审批，不删除原始制度。</p>
        <label className="policy-check"><input type="checkbox" checked={lifecycleChecked} onChange={e => setLifecycleChecked(e.target.checked)} disabled={busy} />我确认未来退休或回退范围并保留历史</label>
        <button className="btn btn-primary" onClick={changeLifecycle} disabled={busy || !lifecycleChecked}>确认退休或回退</button>
      </>}</div>}
    </details>
  </section>;
}
