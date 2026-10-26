export type HotelDocument = {
  metadata: {
    legal_entity: string; region: string; expense_type: string; version: string;
    effective_from: string; effective_to: string | null;
  } | null;
  parsed: { extracted_metadata?: Record<string, { value: string }> };
};

export type DocumentIssue = { field: string; label: string; confirmed: string; original: string };
export type MatrixCell = { employee_grade: string; city_tier: string; original_cell: string };

const metadataLabels = {
  legal_entity: '公司', region: '适用地区', expense_type: '费用类型', version: '制度版本',
  effective_from: '生效日期', effective_to: '截止日期',
} as const;

/** Early guidance only; the backend independently checks the original on publication. */
export function hotelDocumentIssues(document: HotelDocument): DocumentIssue[] {
  if (!document.metadata) return [];
  const metadata = document.metadata;
  return Object.entries(metadataLabels).flatMap(([field, label]) => {
    const original = document.parsed.extracted_metadata?.[field]?.value;
    const confirmed = metadata[field as keyof typeof metadata];
    if (field === 'region' && confirmed === '*' && original && original !== 'UNKNOWN') return [];
    if (field === 'expense_type' && ['ALL', 'HOTEL'].includes(confirmed ?? '')
      && ['ALL', 'HOTEL'].includes(original ?? '')) return [];
    if (original !== undefined && original === confirmed) return [];
    return [{ field, label, confirmed: confirmed ?? '未设置',
      original: !original || original === 'UNKNOWN' ? '原文未能明确识别' : original }];
  });
}

export function completeHotelMatrix(cells: MatrixCell[]): boolean {
  if (cells.length !== 16) return false;
  return ['A', 'B', 'C', 'D'].every(grade => ['I', 'II', 'III', 'IV'].every(tier =>
    cells.filter(cell => cell.employee_grade === grade && cell.city_tier === tier
      && cell.original_cell.trim()).length === 1));
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(key =>
    JSON.stringify(key) + ':' + canonical((value as Record<string, unknown>)[key])).join(',') + '}';
  return JSON.stringify(value) ?? 'undefined';
}

export function matchingHotelReview(
  review: { file_sha256: string; confirmed_rule: { matrix?: unknown; metadata?: unknown } } | undefined,
  extraction: { file_sha256: string }, metadata: unknown,
): boolean {
  return !!review && review.file_sha256 === extraction.file_sha256
    && canonical(review.confirmed_rule.matrix) === canonical(extraction)
    && canonical(review.confirmed_rule.metadata) === canonical(metadata);
}

export function canPublishHotelPreview(preview: {
  preview_digest?: string; blockers: string[]; new?: unknown;
} | null): boolean {
  return !!preview?.preview_digest && /^[a-f0-9]{64}$/.test(preview.preview_digest)
    && preview.blockers.length === 0 && !!preview.new;
}

export const HOTEL_PREVIEW_HELP: Record<string, string> = {
  INDEX_REQUIRED: '住宿标准尚未准备完成，请返回第一步确认标准。',
  MATRIX_CONFIRM_REQUIRED: '请先核对并确认全部住宿限额。',
  PREDECESSOR_REQUIRED: '这份标准与现有制度日期重叠，请明确选择需要替换的旧版本。',
  POLICY_CONFLICT: '所选旧版本与实际冲突不一致，请重新核对需要替换的版本。',
  METADATA_SOURCE_MISMATCH: '制度信息与原文不一致，请先回到企业制度核对。',
  FUTURE_EFFECTIVE_REQUIRED: '新标准的生效日期必须晚于当前日期，请核对制度原文。',
  PREVIEW_STALE: '标准或版本已变化，请重新检查发布影响。',
  ALREADY_PUBLISHED: '这份制度已有发布记录，请刷新页面查看。',
};
