import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const root = fileURLToPath(new URL('../', import.meta.url));
const output = mkdtempSync(join(tmpdir(), 'hotel-policy-flow-'));
after(() => rmSync(output, { recursive: true, force: true }));
const compiled = spawnSync(process.execPath, [join(root, 'node_modules/typescript/bin/tsc'),
  join(root, 'src/utils/hotelPolicyFlow.ts'), '--target', 'ES2020', '--module', 'commonjs',
  '--strict', '--skipLibCheck', '--outDir', output], { encoding: 'utf8' });
assert.equal(compiled.status, 0, compiled.stdout + compiled.stderr);
const { hotelDocumentIssues, completeHotelMatrix, matchingHotelReview,
  canPublishHotelPreview } = createRequire(import.meta.url)(join(output, 'hotelPolicyFlow.js'));

const original = { legal_entity: 'DEMO-CN', region: '深圳', expense_type: 'HOTEL',
  version: '2026-11-V2', effective_from: '2026-11-01', effective_to: '2027-01-01' };
function document(changes = {}) {
  return { metadata: { ...original, region: '*', expense_type: 'ALL', ...changes },
    parsed: { extracted_metadata: Object.fromEntries(Object.entries(original).map(([key, value]) =>
      [key, { value }])) } };
}
const cells = ['A', 'B', 'C', 'D'].flatMap(employee_grade => ['I', 'II', 'III', 'IV'].map(city_tier =>
  ({ employee_grade, city_tier, original_cell: '800' })));

test('综合文档仍可发布住宿规则，全部分类本身不是错误', () => {
  assert.deepEqual(hotelDocumentIssues(document()), []);
  assert.deepEqual(hotelDocumentIssues(document({ expense_type: 'HOTEL', region: '深圳' })), []);
});

test('提前说明当前用户截止日期的具体差异，不改写确认值', () => {
  const value = document({ effective_to: '2027-10-01' });
  const snapshot = JSON.stringify(value);
  assert.deepEqual(hotelDocumentIssues(value), [{ field: 'effective_to', label: '截止日期',
    confirmed: '2027-10-01', original: '2027-01-01' }]);
  assert.equal(JSON.stringify(value), snapshot);
});

test('公司、版本、开始日期及特定地区差异均需处理', () => {
  assert.deepEqual(hotelDocumentIssues(document({ legal_entity: 'DEMO-B', version: 'other',
    effective_from: '2026-11-02', region: '杭州' })).map(issue => issue.field),
  ['legal_entity', 'region', 'version', 'effective_from']);
});

test('无法识别的来源和不包含住宿的分类不会被全部选项掩盖', () => {
  const value = document();
  value.parsed.extracted_metadata.region.value = 'UNKNOWN';
  assert.equal(hotelDocumentIssues(value)[0].field, 'region');
  assert.equal(hotelDocumentIssues(document({ expense_type: 'MEAL' }))[0].field, 'expense_type');
});

test('矩阵必须包含16个互不重复且有原始金额的单元格', () => {
  assert.equal(completeHotelMatrix(cells), true);
  assert.equal(completeHotelMatrix(cells.slice(1)), false);
  assert.equal(completeHotelMatrix([...cells.slice(1), cells[1]]), false);
  assert.equal(completeHotelMatrix([...cells, cells[0]]), false);
  assert.equal(completeHotelMatrix(cells.map((cell, i) => i ? cell : { ...cell, original_cell: '' })), false);
});

test('恢复已保存的核对时，原件、完整矩阵和确认信息必须都匹配', () => {
  const extraction = { file_sha256: 'original-sha', rules: cells };
  const metadata = document().metadata;
  const review = { file_sha256: 'original-sha', confirmed_rule: { matrix: extraction, metadata } };
  assert.equal(matchingHotelReview(review, extraction, metadata), true);
  assert.equal(matchingHotelReview({ ...review, file_sha256: 'different' }, extraction, metadata), false);
  assert.equal(matchingHotelReview(review, { ...extraction, rules: cells.slice(1) }, metadata), false);
  assert.equal(matchingHotelReview(review, extraction, { ...metadata, version: 'other' }), false);
  assert.equal(matchingHotelReview(undefined, extraction, metadata), false);
});

test('JSONB字段顺序变化不会丢失已有的有效核对状态', () => {
  const extraction = { file_sha256: 'source', rules: cells };
  const review = { file_sha256: 'source', confirmed_rule: {
    matrix: { rules: cells.map(cell => ({ original_cell: cell.original_cell,
      city_tier: cell.city_tier, employee_grade: cell.employee_grade })), file_sha256: 'source' },
    metadata: { region: '*', expense_type: 'ALL' },
  } };
  assert.equal(matchingHotelReview(review, extraction, { expense_type: 'ALL', region: '*' }), true);
});

test('只有完整且无阻断的正式预览才能进入最终发布', () => {
  const valid = { preview_digest: 'a'.repeat(64), blockers: [], new: { version: 'v2' } };
  assert.equal(canPublishHotelPreview(valid), true);
  assert.equal(canPublishHotelPreview({ ...valid, blockers: ['METADATA_SOURCE_MISMATCH'] }), false);
  assert.equal(canPublishHotelPreview({ ...valid, new: undefined }), false);
  assert.equal(canPublishHotelPreview({ ...valid, preview_digest: '' }), false);
  assert.equal(canPublishHotelPreview({ ...valid, preview_digest: 'invalid' }), false);
  assert.equal(canPublishHotelPreview(null), false);
});
