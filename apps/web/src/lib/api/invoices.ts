import { API_BASE } from './client';
import { ApiError } from './types';
import type { SubmissionFields, SubmissionResponse } from './submission';
import { ENTERPRISE_MODE, portalHttp } from './enterprise';
import { hotelDateError, invoiceValidationMessage } from '../../utils/invoiceValidation';

export const INVOICE_FIELDS = {
  invoice_number: '发票号码', invoice_code: '发票代码（如有）', issue_date: '开票日期',
  seller_name: '销售方名称', buyer_name: '购买方名称', total_amount: '价税合计', tax_amount: '税额（如有）',
};
export type InvoiceKey = keyof typeof INVOICE_FIELDS;
export type ConfirmedFields = Record<InvoiceKey, string | null>;
export interface OCRResult {
  provider: string; engine_version: string; methods: string[];
  fields: Record<InvoiceKey, { value: string | null; status: string; confidence: number | null; source: string | null; page: number | null; line: number | null }>;
}
export interface Attachment {
  attachment_id: string; file_name: string; content_type: string; size: number;
  sha256: string; pages: number; state: string; ocr: OCRResult | null;
}
export interface Draft { draft_id: string; token: string }
export interface DraftState { draft_id: string; claim_id: string | null; attachment: Attachment | null }
export interface InvoiceConfirmation extends Draft {
  attachment_id: string; confirmed: true; confirmed_fields: ConfirmedFields; discrepancy_reason: string;
}
export interface InvoiceEvidence {
  attachment: Attachment; confirmed_fields: ConfirmedFields;
  discrepancy_reason: string; warnings: Warning[]; confirmed_at: string; invoice_verified: false;
}
export interface Warning { code: string; message: string }
export interface InvoiceResponse extends SubmissionResponse { invoice_evidence: InvoiceEvidence | null }
export const DRAFT_STORAGE = 'ui-p2-invoice-draft';
const INVOICE_BASE = `${API_BASE}${ENTERPRISE_MODE ? '/api/v1.5/invoices' : '/api/v1.2'}`;

export async function invoiceHttp<T>(path: string, body?: unknown): Promise<T> {
  try {
    const response = await fetch(`${INVOICE_BASE}${path}`, {
      credentials: ENTERPRISE_MODE ? 'include' : 'same-origin',
      method: body === undefined ? 'GET' : 'POST',
      headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(120000),
    });
    const data = await response.json();
    if (!response.ok) throw new ApiError(response.status, data.error?.code ?? 'HTTP_ERROR',
      data.error?.message ?? '票据请求失败', data.request_id, data.error?.details);
    return data;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError(0, 'NETWORK_ERROR', '连接失败或请求超时，请重试同一草稿。');
  }
}
export async function previewInvoice(draft: Draft, attachment: Attachment): Promise<string> {
  const response = await fetch(`${INVOICE_BASE}/drafts/${draft.draft_id}/attachments/${attachment.attachment_id}/preview`, {
    credentials: ENTERPRISE_MODE ? 'include' : 'same-origin',
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: draft.token }), signal: AbortSignal.timeout(30000),
  });
  if (!response.ok) throw new Error('发票预览不可用，请检查附件后重试');
  return URL.createObjectURL(await response.blob());
}
export function uploadInvoice(draft: Draft, file: File, progress: (value: number) => void): Promise<Attachment> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${INVOICE_BASE}/drafts/${draft.draft_id}/attachments`);
    xhr.withCredentials = ENTERPRISE_MODE;
    xhr.timeout = 120000;
    xhr.upload.onprogress = e => { if (e.lengthComputable) progress(Math.round(e.loaded / e.total * 100)); };
    xhr.onload = () => {
      let data: { error?: { code: string; message: string } } & Attachment;
      try { data = JSON.parse(xhr.responseText); } catch { reject(new Error('文件超过上限或服务器拒绝上传')); return; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new ApiError(xhr.status, data.error?.code ?? 'UPLOAD_ERROR', data.error?.message ?? '上传失败'));
    };
    xhr.onerror = xhr.ontimeout = () => reject(new Error('上传连接失败或超时；重试相同文件会复用同一附件'));
    const body = new FormData(); body.append('token', draft.token); body.append('file', file);
    xhr.send(body);
  });
}
export function confirmationValues(values: ConfirmedFields): ConfirmedFields {
  return { ...values, issue_date: values.issue_date || null, total_amount: values.total_amount || null, tax_amount: values.tax_amount || null };
}
export async function checkInvoice(draft: Draft, fields: SubmissionFields, values: ConfirmedFields, p4a = false) {
  if (p4a) {
    const error = hotelDateError(fields.trip_start, fields.trip_end);
    if (error) throw new ApiError(422, 'VALIDATION_ERROR', error);
  }
  try {
    return p4a ? await portalHttp<{ warnings: Warning[] }>(`/p4a/v1/invoices/drafts/${draft.draft_id}/checks`, {
      fields, confirmed_fields: confirmationValues(values),
    }) : await invoiceHttp<{ warnings: Warning[] }>(`/drafts/${draft.draft_id}/checks`, {
      token: draft.token, fields, confirmed_fields: confirmationValues(values),
    });
  } catch (e) {
    if (e instanceof ApiError && e.code === 'VALIDATION_ERROR') {
      throw new ApiError(e.status, e.code, invoiceValidationMessage(e.details), e.requestId, e.details);
    }
    throw e;
  }
}
