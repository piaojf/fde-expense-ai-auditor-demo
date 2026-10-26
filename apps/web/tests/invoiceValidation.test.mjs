import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const root = fileURLToPath(new URL('../', import.meta.url));
const output = mkdtempSync(join(tmpdir(), 'invoice-validation-'));
after(() => rmSync(output, { recursive: true, force: true }));
const compiled = spawnSync(process.execPath, [join(root, 'node_modules/typescript/bin/tsc'),
  join(root, 'src/utils/invoiceValidation.ts'), '--target', 'ES2020', '--module', 'commonjs',
  '--strict', '--skipLibCheck', '--outDir', output], { encoding: 'utf8' });
assert.equal(compiled.status, 0, compiled.stdout + compiled.stderr);
const { hotelDateError, invoiceValidationMessage } = createRequire(import.meta.url)(join(output, 'invoiceValidation.js'));

test('错误年份明确指出实际日期、366晚和365晚限制', () => {
  const text = hotelDateError('2024-05-12', '2025-05-13');
  for (const fragment of ['2024-05-12', '2025-05-13', '366 晚', '365 晚', '年份']) assert.ok(text.includes(fragment));
});

test('正常跨年、闰日和365晚不误报', () => {
  for (const [start, end] of [['2024-05-12', '2024-05-13'], ['2024-12-31', '2025-01-01'],
    ['2024-02-28', '2024-03-01'], ['2024-05-12', '2025-05-12']]) {
    assert.equal(hotelDateError(start, end), null);
  }
});

test('同日和反向日期说明退房须晚于入住', () => {
  for (const end of ['2024-05-12', '2024-05-11']) {
    assert.match(hotelDateError('2024-05-12', end), /退房日期必须晚于入住日期/);
  }
});

test('缺日期和不存在的日期指向具体输入框', () => {
  assert.match(hotelDateError('', '2024-05-13'), /请填写入住日期/);
  assert.match(hotelDateError('2024-05-12', ''), /请填写退房日期/);
  assert.match(hotelDateError('2025-02-29', '2025-03-01'), /入住日期无效/);
  assert.match(hotelDateError('2024-02-28', '2024-02-30'), /退房日期无效/);
});

test('服务端空字段、金额和开票日期错误转成中文并合并去重', () => {
  const issue = (field, type) => ({ location: ['body', 'fields', field], type, message: 'English internals' });
  const message = invoiceValidationMessage({ issues: [issue('city', 'string_too_short'),
    issue('amount', 'greater_than'), issue('issue_date', 'date_from_datetime_parsing'),
    issue('city', 'string_too_short')] });
  assert.match(message, /实际住宿城市／区县不能为空/);
  assert.match(message, /报销金额必须大于 0/);
  assert.match(message, /开票日期无效/);
  assert.equal(message.split('实际住宿城市／区县').length, 2);
  assert.ok(!message.includes('English internals'));
});

test('模型级中文校验原因保留，未知错误提供中文兜底', () => {
  assert.match(invoiceValidationMessage({ issues: [{ location: ['body', 'fields'], type: 'value_error',
    message: 'Value error, 退房日期减入住日期必须等于住宿晚数' }] }), /退房日期减入住日期必须等于住宿晚数/);
  for (const details of [{}, { issues: [] }, { issues: [null, 'unexpected'] }]) {
    assert.match(invoiceValidationMessage(details), /表单校验未通过/);
  }
});
