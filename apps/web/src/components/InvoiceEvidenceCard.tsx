import { API_BASE } from '../lib/api/client';
import { INVOICE_FIELDS, type InvoiceEvidence, type InvoiceKey } from '../lib/api/invoices';

/**
 * 已确认的发票证据（展示组件）。
 * 只呈现接口返回的「识别原值」与「人工确认值」，前端不做任何真伪判断。
 */
export default function InvoiceEvidenceCard({ claimId, evidence }: { claimId: string; evidence: InvoiceEvidence }) {
  return <div className="card invoice-evidence">
    <div className="card-title">
      已确认的发票证据
      <span className="card-title-extra">
        <span className="badge badge-no-dot badge-tag-green">已人工确认</span>
      </span>
    </div>

    <p className="page-desc">
      {evidence.attachment.file_name} · {evidence.attachment.content_type} · {evidence.attachment.pages} 页
    </p>

    <figure className="invoice-preview-frame">
      <figcaption>发票第一页预览</figcaption>
      <img
        className="invoice-preview"
        src={`${API_BASE}/api/v1.2/claims/${encodeURIComponent(claimId)}/invoice-preview`}
        alt="已提交发票第一页预览"
      />
    </figure>

    <div className="kv-list">
      {(Object.keys(INVOICE_FIELDS) as InvoiceKey[]).map((key) => {
        const confirmed = evidence.confirmed_fields[key];
        const original = evidence.attachment.ocr?.fields[key].value ?? null;
        const changed = !!confirmed && !!original && confirmed !== original;
        return (
          <div className="kv" key={key}>
            <span className="k">{INVOICE_FIELDS[key]}</span>
            <span className="v">
              <span className="cell-strong">{confirmed || 'UNKNOWN'}</span>
              <span className="ocr-meta">
                <span className={`ocr-chip${changed ? ' is-weak' : ''}`}>
                  识别原值 <b>{original ?? 'UNKNOWN'}</b>
                </span>
                {changed ? <span className="ocr-chip is-weak">已人工修正</span> : null}
              </span>
            </span>
          </div>
        );
      })}
    </div>

    <p className="invoice-source">
      识别路径：{evidence.attachment.ocr?.methods.join(' + ')} · {evidence.attachment.ocr?.engine_version}
    </p>

    {evidence.warnings.length > 0 && (
      <ul className="invoice-warnings">
        {evidence.warnings.map((w) => <li key={w.code}>{w.message}</li>)}
      </ul>
    )}

    {evidence.discrepancy_reason && <p className="page-desc">人工说明：{evidence.discrepancy_reason}</p>}

    <p className="page-desc">SHA-256：{evidence.attachment.sha256}</p>
    <p className="page-desc">确认时间：{evidence.confirmed_at}。识别及确认均非税务真伪验证；风险提示不代表欺诈认定。</p>
  </div>;
}
