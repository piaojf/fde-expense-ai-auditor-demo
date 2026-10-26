import { API_BASE } from "./client";
import { ApiError } from "./types";
import type { PolicyLine } from "./policies";
import { ENTERPRISE_MODE, portalHttp, storageKey } from './enterprise';

export interface RuleCandidate {
  amount: string; currency: "CNY"; quote: string; start: number; end: number; source: PolicyLine;
}
export interface RuleReview {
  review_id: string; revision: number; actor: string; reason: string; recorded_at: string;
  extraction: { candidates: RuleCandidate[]; blockers: string[] };
  confirmed_rule: { amount: string; currency: string; clause: RuleCandidate; metadata_changes: Record<string, unknown>; rule_scope?: { region: string; expense_type: 'HOTEL' }; source_mode?: 'AUTOMATIC' | 'MANUAL'; conditions?: string; execution_status?: 'READY' | 'REFERENCE_ONLY'; execution_blockers?: string[] };
}
export interface PublishedPolicy {
  policy_version_id: string; version: string; amount: string | null;
  effective_from: string; effective_to: string | null;
  legal_entity: string; region: string; expense_type: string;
}
export interface PublicationEvent {
  event_id: string; kind: string; policy: PublishedPolicy | null; predecessor_id: string | null;
  actor: string; recorded_at: string; effective_on?: string;
}
export interface RuleData {
  extraction: { status: string; candidates: RuleCandidate[]; blockers: string[] };
  reviews: RuleReview[]; publications: PublicationEvent[];
  source_lines?: PolicyLine[];
  reference_tables?: { cells: string[]; quote: string; start: number; end: number; line: number; end_line: number }[];
  condition_warning?: boolean;
}

const ruleMessages: Record<string, string> = {
  HOTEL_LIMIT_MISSING: '未识别到明确的每晚住宿限额，可引用原文补录。',
  EXTRACTION_UNCERTAIN: '部分条款未能可靠识别，请对照原文核对。',
  CONFLICTING_HOTEL_LIMITS: '原文有多个住宿金额，需要区分适用条件，不能任选一个统一发布。',
  SOURCE_SPAN_INVALID: '原文位置校验失败，请重新上传文件。',
  RULE_CONDITIONS_UNSUPPORTED: '原文涉及职级、城市分级或例外等条件，当前引擎尚不能完整执行，可保存为参考草稿。',
  RULE_HOTEL_SOURCE_REQUIRED: '引用中缺少明确的住宿费用说明。',
  RULE_NIGHT_UNIT_REQUIRED: '引用中缺少“每晚”单位，请同时引用表头或相关说明。',
  RULE_LIMIT_SOURCE_REQUIRED: '引用中缺少明确的上限或标准说明。',
  RULE_REGION_SOURCE_REQUIRED: '引用中未明确具体城市，请补充相关原文范围说明。',
  RULE_CURRENCY_SOURCE_REQUIRED: '引用中未能确认人民币币种。',
  RULE_MULTIPLE_AMOUNTS: '引用包含多个金额或金额单位不完整，须补充各金额的适用条件。',
  RULE_OTHER_STANDARDS: '文档另有未核清的住宿标准，需要进一步区分条件。',
  RULE_TABLE_REVIEW_REQUIRED: '文档包含住宿表格标准，目前可逐行引用保存；尚不能将表格条件自动用于审批。',
  RULE_DATE_CONDITIONS: '引用包含额外日期条件，需要核对其与制度生效日期的关系，暂存为参考草稿。',
  RULE_REFERENCE_ONLY: '此记录为参考草稿，尚不能作为自动审核规则发布。',
};
export function ruleMessage(code: string) {
  return ruleMessages[code] ?? '规则校验未通过，请核对原文、适用范围和生效日期。';
}
export interface PublicationHistory { events: PublicationEvent[]; versions: PublishedPolicy[] }
export interface ReleasePreview {
  blockers: string[]; message?: string; preview_digest?: string;
  current?: PublishedPolicy | null; old?: PublishedPolicy | null;
  new?: PublishedPolicy; target?: PublishedPolicy; restore?: PublishedPolicy | null;
  authority_at_start?: PublishedPolicy | null; effective_on?: string;
  clause?: RuleCandidate; embedding_model?: string; real_semantic_validation?: string;
  impact?: { from_date: string; to_date: string | null; legal_entity: string; region: string; expense_type: string };
}
export async function publicationHttp<T>(token: string, path: string, body = {}): Promise<T> {
  if (ENTERPRISE_MODE) return portalHttp<T>(`/publication/policies${path}`, body);
  const response = await fetch(`${API_BASE}/api/v1.4/policies${path}`, {
    credentials: 'same-origin',
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token, ...body }),
  });
  const data = await response.json();
  if (!response.ok) throw new ApiError(response.status, data.error?.code ?? "PUBLICATION_ERROR", data.error?.message ?? "制度发布失败");
  return data as T;
}
export function operationKey(digest: string) {
  const name = storageKey(`ui-p3b-operation-${digest}`);
  let key = sessionStorage.getItem(name);
  if (!key) { key = crypto.randomUUID(); sessionStorage.setItem(name, key); }
  return key;
}
