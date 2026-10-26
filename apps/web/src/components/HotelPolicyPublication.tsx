import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { portalHttp } from '../lib/api/enterprise';
import { downloadPolicy, type PolicyUpload } from '../lib/api/policies';
import { ApiError } from '../lib/api/types';
import {
  canPublishHotelPreview, completeHotelMatrix, hotelDocumentIssues, HOTEL_PREVIEW_HELP,
  matchingHotelReview, type MatrixCell,
} from '../utils/hotelPolicyFlow';
import './HotelPolicyPublication.css';

type Cell = MatrixCell & { source_table: number; source_row: number; source_column: number };
type Extraction = { file_sha256: string; rules: Cell[];
  city_classification: { city_tier: string; scope: string }[] };
type Review = { revision: number; reason: string; recorded_at: string; file_sha256: string;
  confirmed_rule: { matrix?: unknown; metadata?: unknown } };
type Source = { source_state: string; metadata_confirmed: boolean; extraction_digest: string;
  extraction: Extraction; reviews: Review[] };
type Version = { policy_version_id: string; version: string; region: string;
  effective_from: string; effective_to: string | null; source_uri?: string };
type Preview = { blockers: string[]; message?: string; preview_digest?: string;
  new?: Version; old?: Version[]; impact?: { city_scope: { city_tier: string; scope: string }[] } };
type PublishRequest = { predecessor_ids: string[]; preview_digest: string; confirmed: true;
  idempotency_key: string; reason: string };

const api = <T,>(path: string, body?: unknown) => portalHttp<T>(`/p4a/v1${path}`, body);
const steps = ['核对住宿标准', '查看发布影响', '确认发布'];
const grades = ['A', 'B', 'C', 'D'];
const tiers = ['I', 'II', 'III', 'IV'];
const errorText = (error: unknown) => error instanceof Error ? error.message : '操作未完成，请重试。';
const period = (value: Version) => `${value.effective_from} 起，${value.effective_to ? `至 ${value.effective_to} 前` : '长期有效'}`;
const scope = (region: string) => region === '*' ? '各城市等级' : region;

export default function HotelPolicyPublication({ uploadId }: { uploadId: string }) {
  const [uid, setUid] = useState(uploadId);
  const [documents, setDocuments] = useState<PolicyUpload[]>([]);
  const [document, setDocument] = useState<PolicyUpload | null>(null);
  const [source, setSource] = useState<Source | null>(null);
  const [versions, setVersions] = useState<Version[]>([]);
  const [versionsReady, setVersionsReady] = useState(false);
  const [loading, setLoading] = useState(!!uploadId);
  const [listLoading, setListLoading] = useState(true);
  const [step, setStep] = useState(1);
  const [reason, setReason] = useState('');
  const [checked, setChecked] = useState(false);
  const [publishChecked, setPublishChecked] = useState(false);
  const [ids, setIds] = useState<string[]>([]);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [published, setPublished] = useState<Version | null>(null);
  const [pending, setPending] = useState<PublishRequest | null>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [cell, setCell] = useState<Cell | null>(null);
  const [refresh, setRefresh] = useState(0);
  const actionLock = useRef(false);
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    let active = true;
    setListLoading(true); setVersionsReady(false);
    Promise.all([
      portalHttp<{ items: PolicyUpload[] }>('/policies/list', {}),
      api<{ items: Version[] }>('/versions'),
    ]).then(([list, current]) => {
      if (!active) return;
      const eligible = list.items.filter(item => item.metadata
        && ['HOTEL', 'ALL'].includes(item.metadata.expense_type));
      setDocuments(eligible); setVersions(current.items); setVersionsReady(true);
      if (eligible.length === 1) setUid(value => value || eligible[0].upload_id);
    }).catch(error => { if (active) setError(errorText(error)); })
      .finally(() => { if (active) setListLoading(false); });
    return () => { active = false; };
  }, [refresh]);

  useEffect(() => {
    let active = true;
    setDocument(null); setSource(null); setStep(1); setIds([]); setPreview(null);
    setChecked(false); setPublishChecked(false); setPending(null); setPublished(null);
    setReason(''); setCell(null); setError('');
    if (!uid) { setLoading(false); return; }
    setLoading(true);
    Promise.all([
      portalHttp<PolicyUpload>(`/policies/${uid}/read`, {}),
      api<Source>(`/policies/${uid}/read`, {}),
    ]).then(([item, extracted]) => {
      if (!active) return;
      setDocument(item); setSource(extracted);
      const latest = extracted.reviews[extracted.reviews.length - 1];
      if (latest && matchingHotelReview(latest, extracted.extraction, item.metadata)) {
        setReason(latest.reason);
        if (extracted.source_state === 'INDEXED' && !hotelDocumentIssues(item).length) setStep(2);
      }
    }).catch(error => { if (active) setError(errorText(error)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [uid, refresh]);

  const issues = document ? hotelDocumentIssues(document) : [];
  const complete = !!source && completeHotelMatrix(source.extraction.rules);
  const latest = source?.reviews[source.reviews.length - 1];
  const reviewed = !!source && !!document
    && matchingHotelReview(latest, source.extraction, document.metadata);
  const prepared = source?.source_state === 'INDEXED';
  const previewReady = canPublishHotelPreview(preview);
  const currentPublication = published ?? versions.find(version =>
    version.source_uri === `upload://policies/${uid}`) ?? null;
  const locked = !!busy || !!pending;
  const documentReady = !!document?.metadata && !!source?.metadata_confirmed
    && ['HOTEL', 'ALL'].includes(document.metadata.expense_type);
  const canConfirm = documentReady && complete && !issues.length && checked && !!reason.trim();

  function goTo(value: number) {
    setStep(value); setError('');
    requestAnimationFrame(() => heading.current?.focus());
  }

  async function run(kind: 'confirm' | 'preview' | 'publish' | 'download') {
    if (actionLock.current || !source || !document) return;
    if (kind === 'confirm' && (!canConfirm || pending)) return;
    if (kind === 'preview' && (!reviewed || !prepared || !versionsReady || issues.length || pending)) return;
    if (kind === 'publish' && (!previewReady || !publishChecked || !reason.trim())) return;
    actionLock.current = true; setBusy(kind); setError('');
    const base = `/policies/${uid}`;
    try {
      if (kind === 'download') await downloadPolicy('', document);
      if (kind === 'confirm') {
        // Both writes follow the user's explicit confirmation, never page loading.
        if (!prepared) await portalHttp(`/policies/${uid}/ingest`, { remote_authorized: false });
        await api(base + '/confirm', {
          expected_revision: latest?.revision ?? 0, extraction_digest: source.extraction_digest,
          confirmed: true, reason: reason.trim(),
        });
        setSource(await api<Source>(base + '/read', {}));
        setPreview(null); setPublishChecked(false); goTo(2);
      }
      if (kind === 'preview') {
        const result = await api<Preview>(base + '/preview', { predecessor_ids: ids });
        setPreview(result); setPublishChecked(false);
        if (canPublishHotelPreview(result)) goTo(3);
      }
      if (kind === 'publish' && preview?.preview_digest) {
        const request: PublishRequest = pending ?? {
          predecessor_ids: [...ids], preview_digest: preview.preview_digest,
          confirmed: true, idempotency_key: crypto.randomUUID(), reason: reason.trim(),
        };
        setPending(request);
        const result = await api<{ policy: Version }>(base + '/publish', request);
        setPublished(result.policy); setPending(null); setPreview(null); setPublishChecked(false);
      }
    } catch (error) {
      setError(errorText(error));
      if (kind === 'publish' && error instanceof ApiError && error.status >= 400 && error.status < 500) {
        setPending(null); setPreview(null); setPublishChecked(false); setStep(2);
      }
    } finally { actionLock.current = false; setBusy(''); }
  }

  const nextHint = issues.length ? `可以勾选住宿限额核对；继续前仍需处理制度信息差异：${issues.map(issue =>
    `${issue.label}已确认为 ${issue.confirmed}，原文为 ${issue.original}`).join('；')}。请通过新的制度版本修订后继续。`
    : !documentReady ? '请先在企业制度页面确认这份文件的制度信息。'
    : !complete ? '尚未取得完整的住宿标准，请先核对原文件。'
    : !checked ? '核对表格后，请勾选确认并填写核对说明。'
    : !reason.trim() ? '请填写核对说明，以便日后查询确认依据。'
    : '下一步会检查生效日期，以及是否需要替换旧标准。';

  return <section className="hotel-publication" aria-labelledby="hotel-publication-title" aria-busy={loading || !!busy}>
    <header className="hotel-publish-header">
      <div><p className="hotel-publish-eyebrow">企业制度 · 住宿费用</p>
        <h1 id="hotel-publication-title">发布住宿标准</h1>
        <p>先核对每晚可报销的住宿限额，再检查生效日期，最后确认发布。</p></div>
      <span className={`hotel-publish-status${currentPublication ? ' is-complete' : ''}`}>
        {currentPublication ? '已有发布记录' : issues.length ? '制度信息待核对' : reviewed ? '标准已核对 · 尚未发布' : '标准待核对'}
      </span>
    </header>

    <section className="card hotel-document-card" aria-label="本次使用的制度">
      <div className="hotel-document-title"><span className="hotel-document-icon" aria-hidden="true">文</span>
        <div><label htmlFor="hotel-policy-document">本次使用的制度</label>
          <select id="hotel-policy-document" className="select" value={uid} disabled={locked || listLoading}
            onChange={event => setUid(event.target.value)}>
            {!documents.some(item => item.upload_id === uid) && <option value={uid}>
              {listLoading ? '正在读取制度文件…' : uid ? document?.filename ?? '正在读取所选制度…' : '请选择已确认的制度文件'}
            </option>}
            {documents.map(item => <option key={item.upload_id} value={item.upload_id}>
              {item.filename} · {item.metadata?.version}
            </option>)}
          </select></div></div>
      <button className="btn btn-sm" type="button" disabled={locked || loading || listLoading}
        onClick={() => setRefresh(value => value + 1)}>重新读取</button>
      {document?.metadata && <div className="hotel-document-meta">
        <span>版本 <strong>{document.metadata.version}</strong></span>
        <span>生效日 <strong>{document.metadata.effective_from}</strong></span>
        <span>截止日（不含） <strong>{document.metadata.effective_to ?? '未设置'}</strong></span>
      </div>}
      {!listLoading && !documents.length && !uid && <p className="hotel-publish-empty">
        暂无已确认的住宿制度。<Link to="/policies">去上传或确认制度文件</Link></p>}
    </section>

    {issues.length > 0 && <aside className="hotel-publish-warning" role="alert">
      <strong>先核对制度信息，才能发布</strong>
      <p>已确认的信息与上传原文有差异。下面的原值已保留，请核对后再继续。</p>
      {issues.map(issue => <dl className="hotel-date-comparison" key={issue.field}>
        <div><dt>已确认的{issue.label}</dt><dd>{issue.confirmed}</dd></div>
        <div><dt>原文中的{issue.label}</dt><dd>{issue.original}</dd></div>
      </dl>)}
      <Link to="/policies">返回企业制度查看原文与确认信息 →</Link>
      <small>已确认的信息保留在原记录中；若需修订，请通过新的制度版本处理。</small>
    </aside>}

    {!currentPublication && <ol className="hotel-publish-steps" aria-label="住宿标准发布步骤">
      {steps.map((label, index) => {
        const value = index + 1;
        const enabled = value === 1 || value === 2 && reviewed && prepared && !issues.length
          || value === 3 && previewReady;
        return <li key={label} className={step === value ? 'is-current' : step > value ? 'is-done' : ''}>
          <button type="button" disabled={!enabled || locked} aria-current={step === value ? 'step' : undefined}
            onClick={() => goTo(value)}><span>{step > value ? '✓' : value}</span><strong>{label}</strong></button>
        </li>;
      })}
    </ol>}

    {loading ? <section className="card hotel-publish-empty" role="status">正在从制度原文读取住宿标准…</section>
      : currentPublication ? <section className="card hotel-publish-success" role="status">
        <span aria-hidden="true">✓</span><h2>住宿标准已有发布记录</h2>
        <p>{currentPublication.version} · {period(currentPublication)}</p>
        <p>报销会按费用发生日期匹配制度；发布标准后，报销仍由财务作最终决定。</p>
        <Link className="btn" to="/policies">返回企业制度</Link>
      </section> : source && document ? <section className="card hotel-publish-stage">
        <header className="hotel-stage-heading"><span>第 {step} 步 / 共 3 步</span>
          <h2 ref={heading} tabIndex={-1}>{steps[step - 1]}</h2>
          <p>{step === 1 ? '请对照原文件，确认下表的每一项金额。'
            : step === 2 ? '检查这份标准将从何时使用，以及哪些旧标准需要替换。'
            : '再核对一次发布范围。只有点击最后的确认按钮，标准才会正式发布。'}</p>
        </header>

        {step === 1 && <>
          <div className="hotel-limit-table-wrap"><table className="hotel-limit-table">
            <caption>住宿报销上限 <span>单位：元 / 间 / 晚</span></caption>
            <thead><tr><th scope="col">员工职级</th>{tiers.map(tier => <th scope="col" key={tier}>{tier} 类城市</th>)}</tr></thead>
            <tbody>{grades.map(grade => <tr key={grade}><th scope="row">{grade} 级</th>{tiers.map(tier => {
              const value = source.extraction.rules.find(item => item.employee_grade === grade && item.city_tier === tier);
              return <td key={tier}>{value ? <button type="button" className="hotel-cell-source"
                aria-label={`查看${grade}级${tier}类城市${value.original_cell}元的原文位置`}
                onClick={() => setCell(value)}>{value.original_cell}</button> : '待核对'}</td>;
            })}</tr>)}</tbody>
          </table></div>
          <p className="hotel-matrix-explanation">行是员工职级，列是住宿城市的类别；交叉位置就是每间房每晚的报销上限。点击金额可查看原文位置。</p>
          <details className="hotel-source-details"><summary>查看城市分类与原文件</summary>
            {source.extraction.city_classification.map(item => <p key={item.city_tier}><strong>{item.city_tier} 类城市：</strong>{item.scope}</p>)}
            <button type="button" className="btn btn-sm" disabled={!!busy} onClick={() => void run('download')}>下载原文件核对</button>
            <details><summary>文件编号与完整性校验</summary><p>制度编号：{uid}</p><p>SHA-256：{source.extraction.file_sha256}</p></details>
          </details>
          {cell && <div className="hotel-cell-evidence" role="status"><strong>{cell.employee_grade} 级 · {cell.city_tier} 类城市 · {cell.original_cell} 元 / 间 / 晚</strong>
            <p>对应上传原文第 {cell.source_table} 个表格，第 {cell.source_row} 行、第 {cell.source_column} 列。</p>
            <button type="button" className="link-btn" onClick={() => setCell(null)}>收起来源</button></div>}
          {reviewed && <p className="hotel-saved-review" role="status">已有一次已保存的标准核对。重新确认会新增核对记录，原记录会保留。</p>}
          <label className="hotel-publish-check"><input type="checkbox" checked={checked} disabled={locked || !complete}
            onChange={event => setChecked(event.target.checked)} />
            <span>我已对照原文，核对全部 16 项住宿限额和城市分类。</span></label>
          <label className="label" htmlFor="hotel-matrix-review-reason">核对说明 <span>必填，记录本次确认的依据</span></label>
          <textarea id="hotel-matrix-review-reason" className="input" rows={3} maxLength={2000}
            placeholder="例如：已对照制度第五章，逐项核对职级、城市分类和每晚限额。"
            disabled={locked} value={reason} onChange={event => { setReason(event.target.value); setPreview(null); }} />
          <footer className="hotel-stage-actions"><p id="hotel-confirm-help">{nextHint}</p>
            {issues.length > 0 && <Link to="/policies">前往企业制度处理信息差异 →</Link>}
            <button type="button" className="btn btn-primary" aria-describedby="hotel-confirm-help"
              disabled={!!busy || !canConfirm} onClick={() => void run('confirm')}>
              {busy === 'confirm' ? '正在保存核对并准备标准…' : '确认标准并继续'}</button>
            <small>点击后会保存你的核对记录，并准备下一步所需的制度条款。</small>
          </footer>
        </>}

        {step === 2 && <>
          <div className="hotel-publish-impact"><strong>本次仅发布住宿标准</strong>
            <p>覆盖原文中的 A–D 职级及 I–IV 类城市；其他费用类型需另行核对发布。</p>
            <p>生效日期：{document.metadata?.effective_from}；截止日期（不含）：{document.metadata?.effective_to ?? '未设置'}。</p>
            <p>已有报销的审核快照会保留，历史记录不会被本次发布覆盖。</p></div>
          <h3>是否替换旧标准</h3>
          {!versionsReady ? <p role="status">正在读取现有标准。读取完成后才能检查发布影响。</p>
            : versions.length === 0 ? <div className="hotel-no-predecessor"><strong>这是第一次发布住宿标准</strong>
              <p>当前没有已发布的旧标准，无需选择替换版本，直接检查发布影响即可。</p></div>
              : <><p>若新旧制度的生效日期重叠，请勾选需要由本次新标准替换的所有旧版本；系统会再次检查。</p>
                <div className="hotel-predecessors">{versions.map(version => <label className="hotel-predecessor" key={version.policy_version_id}>
                  <input type="checkbox" checked={ids.includes(version.policy_version_id)} disabled={locked}
                    onChange={event => { setIds(previous => event.target.checked ? [...previous, version.policy_version_id]
                      : previous.filter(id => id !== version.policy_version_id)); setPreview(null); setPublishChecked(false); }} />
                  <span><strong>{version.version} · {scope(version.region)}</strong><small>{period(version)}</small></span>
                </label>)}</div></>}
          {preview && !previewReady && <aside className="hotel-publish-warning" role="alert"><strong>发布前还需要处理</strong>
            <p>{preview.message ?? preview.blockers.map(code => HOTEL_PREVIEW_HELP[code] ?? '暂时无法发布，请核对原文与版本信息后重试。').join(' ')}</p>
            <p>处理后可重新检查，当前尚未发布。</p></aside>}
          <footer className="hotel-stage-actions hotel-stage-actions-row">
            <button type="button" className="btn" disabled={locked} onClick={() => goTo(1)}>返回核对标准</button>
            <button type="button" className="btn btn-primary" disabled={!!busy || !versionsReady || !reviewed || !prepared}
              onClick={() => void run('preview')}>{busy === 'preview' ? '正在检查…' : '检查发布影响并继续'}</button>
          </footer>
        </>}

        {step === 3 && previewReady && preview?.new && <>
          <dl className="hotel-release-summary">
            <div><dt>发布版本</dt><dd>{preview.new.version}</dd></div>
            <div><dt>费用类型</dt><dd>酒店住宿</dd></div>
            <div><dt>标准范围</dt><dd>A–D 职级 × I–IV 类城市，共 16 项限额</dd></div>
            <div><dt>生效日期</dt><dd>{preview.new.effective_from}（含）</dd></div>
            <div><dt>截止日期</dt><dd>{preview.new.effective_to ? `${preview.new.effective_to}（不含）` : '长期有效'}</dd></div>
            <div><dt>替换旧版本</dt><dd>{preview.old?.length ? preview.old.map(version => version.version).join('、') : '无，首次发布'}</dd></div>
          </dl>
          <p className="hotel-saved-review">发布影响检查已通过。住宿标准将按费用发生日期使用，报销仍需财务审核。</p>
          <label className="hotel-publish-check"><input type="checkbox" checked={publishChecked} disabled={locked}
            onChange={event => setPublishChecked(event.target.checked)} />
            <span>我确认上述住宿标准、适用范围和生效日期，同意正式发布。</span></label>
          {pending && !busy && <p className="hotel-matrix-explanation">上次发布结果尚未确认。再次点击会复用同一请求，避免重复发布。</p>}
          <footer className="hotel-stage-actions hotel-stage-actions-row">
            <button type="button" className="btn" disabled={locked} onClick={() => goTo(2)}>返回查看发布影响</button>
            <button type="button" className="btn btn-primary" disabled={!!busy || !publishChecked}
              onClick={() => void run('publish')}>{busy === 'publish' ? '正在发布…' : pending ? '重试同一次发布' : '确认发布住宿标准'}</button>
          </footer>
        </>}
      </section> : !loading && uid && !error ? <p className="hotel-publish-empty">正在准备所选制度…</p> : null}
    {error && <div className="hotel-publish-error" role="alert"><strong>本次操作未完成</strong><p>{error}</p>
      {!source && <button type="button" className="btn" disabled={!!busy} onClick={() => setRefresh(value => value + 1)}>重新读取制度</button>}</div>}
  </section>;
}
