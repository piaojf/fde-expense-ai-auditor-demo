import { useEffect, useRef, useState, type ReactNode } from 'react';
import {
  DRAFT_STORAGE, INVOICE_FIELDS, checkInvoice, confirmationValues, invoiceHttp, previewInvoice, uploadInvoice,
  type Attachment, type ConfirmedFields, type Draft, type DraftState, type InvoiceConfirmation, type InvoiceKey, type Warning,
} from '../lib/api/invoices';
import type { SubmissionFields } from '../lib/api/submission';
import { ApiError } from '../lib/api/types';
import { storageKey } from '../lib/api/enterprise';
import type { InvoiceEvidence } from '../lib/api/invoices';

interface Props {
  fields: SubmissionFields; disabled: boolean; p4a?: boolean;
  suggest: (fields: Partial<SubmissionFields>) => void;
  onConfirmation: (value: InvoiceConfirmation | null) => void;
  onAttached: (value: boolean) => void; onBusy: (value: boolean) => void;
  retained?: { draft: Draft; evidence: InvoiceEvidence };
  children?: ReactNode;
}
function invoiceError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.code === 'INVALID_FILE') return '文件内容损坏或格式不支持，请上传有效 PDF、JPG 或 PNG。';
    if (e.code === 'OCR_UNAVAILABLE') return '本机 OCR 不可用，请检查 Tesseract 及所需语言包后重试。';
    if (e.code === 'ATTACHMENT_INTEGRITY') return '附件内容已改变或不可用，请删除后重新上传。';
    if (e.code === 'FILE_TOO_LARGE') return '单文件不能超过 10MB。';
  }
  return e instanceof Error ? e.message : String(e);
}
const hotelKeys: InvoiceKey[] = ['total_amount', 'seller_name', 'invoice_number', 'issue_date'];
const blank: ConfirmedFields = { invoice_number: '', invoice_code: '', issue_date: '', seller_name: '', buyer_name: '', total_amount: '', tax_amount: '' };
export default function InvoicePanel({ fields, disabled, suggest, onConfirmation, onAttached, onBusy, retained, p4a = false, children }: Props) {
  const storage = storageKey(DRAFT_STORAGE);
  const [bound, setBound] = useState(!!retained);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [attachment, setAttachment] = useState<Attachment | null>(null);
  const [values, setValues] = useState<ConfirmedFields>(blank);
  const [preview, setPreview] = useState('');
  const [phase, setPhase] = useState('');
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState('');
  const [reason, setReason] = useState('');
  const [warnings, setWarnings] = useState<Warning[]>([]);
  const [confirmed, setConfirmed] = useState(false);
  const gate = useRef(false);
  const blocked = disabled || !!phase;
  const visibleKeys = p4a ? hotelKeys : Object.keys(INVOICE_FIELDS) as InvoiceKey[];
  function recognizedValues(ocr: NonNullable<Attachment['ocr']>): ConfirmedFields {
    return { ...blank, ...Object.fromEntries((Object.keys(INVOICE_FIELDS) as InvoiceKey[]).map(key => [key, ocr.fields[key].value ?? ''])) };
  }
  function suggestValues(next: ConfirmedFields) {
    suggest({ ...(next.total_amount ? { amount: next.total_amount } : {}),
      ...(!p4a && next.issue_date ? { incurred_on: next.issue_date } : {}),
      ...(next.seller_name ? { seller: next.seller_name } : {}) });
  }
  useEffect(() => {
    if (retained) {
      setDraft(retained.draft); setAttachment(retained.evidence.attachment);
      setValues(retained.evidence.confirmed_fields); setReason(retained.evidence.discrepancy_reason);
      onAttached(true); return;
    }
    let saved: Draft | null = null;
    try { saved = JSON.parse(sessionStorage.getItem(storage) ?? 'null'); } catch { /* invalid local state */ }
    if (saved) {
      setDraft(saved); onBusy(true); setPhase('恢复草稿…');
      invoiceHttp<DraftState>(`/drafts/${saved.draft_id}/read`, { token: saved.token })
        .then(result => {
          if (result.claim_id) { sessionStorage.removeItem(storage); setDraft(null); return; }
          setAttachment(result.attachment); onAttached(!!result.attachment);
          if (result.attachment?.ocr) { const next = recognizedValues(result.attachment.ocr); setValues(next); suggestValues(next); }
        }).catch(e => {
          if (e instanceof ApiError && e.status === 404) {
            sessionStorage.removeItem(storage); setDraft(null);
            setError('原草稿已不可用，请重新选择附件上传。');
          } else setError(invoiceError(e));
        })
        .finally(() => { setPhase(''); onBusy(false); });
    }
    // Initial hydration only; editing form never rehydrates saved OCR over human corrections.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    setConfirmed(false); onConfirmation(null); setWarnings([]);
  }, [fields, values, reason, attachment, onConfirmation]);
  useEffect(() => {
    if (!attachment || !draft) { setPreview(''); return; }
    let cancelled = false; let blob = '';
    setPreview('');
    previewInvoice(draft, attachment).then(url => {
      blob = url; if (cancelled) URL.revokeObjectURL(url); else setPreview(url);
    }).catch(e => { if (!cancelled) setError(String(e.message)); });
    return () => { cancelled = true; if (blob) URL.revokeObjectURL(blob); };
  }, [attachment, draft]);
  async function ensureDraft() {
    const next = draft ?? { draft_id: crypto.randomUUID(), token: crypto.randomUUID() };
    sessionStorage.setItem(storage, JSON.stringify(next)); setDraft(next);
    await invoiceHttp('/drafts', next);
    return next;
  }
  async function recognize(next: Draft, uploaded: Attachment, retry = false) {
    setPhase('正在执行本地识别…');
    const result = await invoiceHttp<Attachment>(`/drafts/${next.draft_id}/attachments/${uploaded.attachment_id}/${retry ? 'ocr-retry' : 'ocr'}`, { token: next.token });
    setAttachment(result);
    if (result.ocr) {
      const recognized = recognizedValues(result.ocr);
      setValues(recognized);
      suggestValues(recognized);
    }
  }
  async function upload(file: File) {
    if (gate.current) return;
    if (file.size > 10 * 1024 * 1024) { setError('单文件不能超过 10MB。'); return; }
    gate.current = true; onBusy(true); setPhase('正在上传…'); setError(''); setProgress(0);
    try {
      const next = await ensureDraft();
      const uploaded = await uploadInvoice(next, file, setProgress);
      setAttachment(uploaded); onAttached(true);
      await recognize(next, uploaded);
    } catch (e) { setError(invoiceError(e)); }
    finally { gate.current = false; onBusy(false); setPhase(''); }
  }
  async function remove() {
    if (!draft || !attachment || gate.current) return;
    if (bound) {
      setBound(false); setDraft(null); setAttachment(null); onAttached(false);
      setValues(blank); setReason(''); setWarnings([]); sessionStorage.removeItem(storage);
      return;
    }
    gate.current = true; onBusy(true); setPhase('删除中…'); setError('');
    try {
      await invoiceHttp(`/drafts/${draft.draft_id}/attachments/${attachment.attachment_id}/delete`, { token: draft.token });
      setAttachment(null); onAttached(false); setValues(blank); setReason(''); setWarnings([]);
    } catch (e) { setError(invoiceError(e)); }
    finally { gate.current = false; onBusy(false); setPhase(''); }
  }
  async function confirm() {
    if (!draft || !attachment?.ocr || gate.current) return;
    gate.current = true; onBusy(true); setPhase('核对票据一致性…'); setError('');
    try {
      const confirmedValues = p4a ? { ...values, total_amount: fields.amount, seller_name: fields.seller } : values;
      const result = await checkInvoice(draft, fields, confirmedValues, p4a); setWarnings(result.warnings);
      if (result.warnings.length && !reason.trim()) { setError('存在核实提示，请填写票据差异或修正说明后再次确认。'); return; }
      setConfirmed(true);
      onConfirmation({ ...draft, attachment_id: attachment.attachment_id, confirmed: true,
        confirmed_fields: confirmationValues(confirmedValues), discrepancy_reason: reason });
    } catch (e) { setError(`确认失败：${invoiceError(e)}`); }
    finally { gate.current = false; onBusy(false); setPhase(''); }
  }
  const amount = attachment?.ocr?.fields.total_amount.value;
  const mismatch = amount && fields.amount && Number(amount) !== Number(fields.amount);
  const recognizedCount = attachment?.ocr ? visibleKeys.filter(key => attachment.ocr!.fields[key].value !== null).length : 0;
  /** 置信度分档：仅用于展示弱提示，不参与任何业务判断 */
  const confidenceClass = (confidence: number | null) =>
    confidence === null ? '' : confidence >= 0.85 ? 'is-strong' : confidence < 0.6 ? 'is-weak' : '';
  const invoiceFields = <div className="submission-fields invoice-fields">{visibleKeys.map(key => {
          const raw = attachment?.ocr?.fields[key];
          return <div className="field" key={key}><label className="label" htmlFor={`invoice-${key}`}>{p4a && key === 'seller_name' ? '酒店名称（发票销售方）' : p4a && key === 'total_amount' ? '本次住宿报销总额（元）' : INVOICE_FIELDS[key]}</label>
            <input className="input" id={`invoice-${key}`} disabled={blocked} type={key === 'issue_date' ? 'date' : p4a && key === 'total_amount' ? 'number' : 'text'} required={p4a && (key === 'total_amount' || key === 'seller_name')} min={p4a && key === 'total_amount' ? '0.01' : undefined} step={p4a && key === 'total_amount' ? '0.01' : undefined} maxLength={key.includes('amount') ? 13 : key.includes('name') ? 200 : 64}
              value={p4a && key === 'total_amount' ? fields.amount : p4a && key === 'seller_name' ? fields.seller : values[key] ?? ''} onChange={e => { setValues(current => ({ ...current, [key]: e.target.value })); if (p4a && key === 'seller_name') suggest({ seller: e.target.value }); if (p4a && key === 'total_amount') suggest({ amount: e.target.value }); }} />
            {raw && <div className="ocr-meta">
              <span className="ocr-chip">识别原值 <b>{raw.value ?? 'UNKNOWN'}</b></span>
              <span className="ocr-chip">
                来源 <b>{raw.source ?? '未识别'}</b>
                {raw.page ? <span>· 第 {raw.page} 页 / 第 {raw.line} 行</span> : null}
              </span>
              {raw.confidence !== null ? (
                <span className={`ocr-chip ${confidenceClass(raw.confidence)}`}>置信度 {(raw.confidence * 100).toFixed(0)}%</span>
              ) : null}
            </div>}</div>;
        })}</div>;
  const confirmationControls = <>
{mismatch && <div role="alert" className="result-tip err">金额差异：识别发票 {amount} 元 / 申报 {fields.amount} 元。需说明原因并人工核实，不能据此认定造假。</div>}
        {warnings.length > 0 && <ul className="invoice-warnings">{warnings.map(w => <li key={w.code}>{w.message}</li>)}</ul>}
        <label className="label" htmlFor="invoice-reason">票据差异或修正说明（有提示时必填）</label>
        <textarea className="textarea" id="invoice-reason" disabled={blocked} maxLength={2000} value={reason} onChange={e => setReason(e.target.value)} />
        <button type="button" className={`btn ${confirmed ? '' : 'btn-primary'}`} disabled={blocked || confirmed} onClick={confirm}>
          {confirmed ? '✓ 已人工确认票据' : '核对并确认票据信息'}</button>
        <p className={`invoice-confirm-status${confirmed ? ' is-confirmed' : ''}`}>{confirmed ? '✓ 票据已确认，可以提交审核。' : '请填写完整报销表单并确认票据；任何修改后需要重新确认。'}</p>
  </>;
  const invoicePreview = <figure className="invoice-preview-frame">
    <figcaption>发票第一页预览</figcaption>
    <img className="invoice-preview" src={preview} alt="发票第一页预览" />
  </figure>;
  return <section className="invoice-panel" aria-label="发票附件与识别">
    <div className="card-title">
      <span className="step-no">{p4a ? 1 : 4}</span>发票附件与识别
      <span className="card-title-extra"><span className="badge badge-tag-gray">本机 Demo</span></span>
    </div>
    <p className="page-desc">PDF / JPG / PNG · 单文件 10MB。识别结果需本人核对，不属于税务真伪验证。</p>
    {!attachment && <div className="invoice-upload"><label className="label" htmlFor="invoice-file">选择发票附件（选填）</label>
      <input id="invoice-file" type="file" accept="application/pdf,image/jpeg,image/png,.pdf,.jpg,.jpeg,.png" disabled={blocked} onChange={e => {
        const file = e.target.files?.[0]; e.target.value = ''; if (file) void upload(file);
      }} />
      <p className="upload-hint">{p4a ? '上传后仅回填报销金额、酒店名称（发票销售方）、发票号码和开票日期，均可手动修改。实际住宿城市／区县、地址、出差事由、备注及入住／退房日期请手动填写。' : '上传后在本机完成 OCR，识别到的金额、费用日期与销售方会回填到上方表单，必须人工核对后才能确认。'}</p></div>}
    {phase && <div role="status" className="invoice-progress"><progress max={100} value={progress} /><span>{phase}{phase.includes('上传') ? ` ${progress}%` : ''}</span></div>}
    {p4a && invoiceFields}
    {p4a && <p className="page-desc">金额填写本次申请的住宿合计，多晚合开发票无需拆分。以上四项可手动修改；实际住宿信息在下一步填写。</p>}
    {attachment && <>
      <div className="invoice-file-info"><strong className="file-name">{attachment.file_name}</strong>
        <span className="file-meta">{(attachment.size / 1024).toFixed(0)} KB · {attachment.pages} 页</span>
        <button type="button" className="btn btn-danger" disabled={blocked} onClick={remove}>{bound ? '更换发票（原件保留在历史中）' : '删除附件并重新上传'}</button></div>
      {bound && <p className="page-desc">此发票来自上次提交，原始识别与历史确认记录保留。请重新核对确认后提交。</p>}
      {preview && (p4a ? <details className="invoice-preview-details"><summary>查看发票原件预览</summary>{invoicePreview}</details> : invoicePreview)}
      {attachment.pages > 1 && <p className="page-desc">此处仅预览第一页；请用原文件核对全部页面。多页字段冲突会保留 UNKNOWN。</p>}
      <p className="page-desc">SHA-256：{attachment.sha256}</p>
      {!bound && <button className="btn" type="button" disabled={blocked} onClick={async () => {
        if (!draft || gate.current) return;
        gate.current = true; onBusy(true); setError('');
        try { await recognize(draft, attachment, !!attachment.ocr); } catch (e) { setError(invoiceError(e)); }
        finally { gate.current = false; onBusy(false); setPhase(''); }
      }}>{attachment.ocr ? '重新识别并回填' : '重试本地 OCR'}</button>}
      {attachment.ocr && <>
        <div className="invoice-source">识别路径：{attachment.ocr.methods.join(' + ')} · {attachment.ocr.engine_version}</div>
        {!p4a && invoiceFields}
        <p className="page-desc" role="status">{recognizedCount ? `已识别 ${recognizedCount} 项票据信息并填入票据输入框；未识别项请对照原票补充。` : '未能提取票据字段，请重新识别或对照原票手工填写。'}
          {p4a ? ' 仅处理报销金额、酒店名称（发票销售方）、发票号码和开票日期。其他住宿信息请手动填写。' : ' 识别到的金额、开票日期及销售方会建议回填上方表单，请核对是否适用。'}
          {!bound && ' 重新识别会更新票据输入框，请先保留需要的手工修正。'}</p>
        {!p4a && confirmationControls}
      </>}
    </>}
    {children}
    {p4a && attachment?.ocr && <div className="hotel-confirmation">
      <div className="card-title"><span className="step-no">3</span>确认票据并提交</div>
      {confirmationControls}
    </div>}
    {error && <div role="alert" className="result-tip err">{error}</div>}
  </section>;
}
