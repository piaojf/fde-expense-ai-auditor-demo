/** Actual browser + React + HTTP FastAPI legacy Mock flow; no API interception. */
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright-core';

const WEB = process.env.WEB_BASE ?? 'http://127.0.0.1:5198';
const API = process.env.API_BASE ?? 'http://127.0.0.1:8000';
const CLAIM = process.env.CLAIM_ID ?? 'CLM-2026-00004';
const OUT = resolve(process.env.E2E_OUTPUT ?? resolve(dirname(fileURLToPath(import.meta.url)),
  '../../../output/ci/public-e2e'));
for (const base of [WEB, API]) {
  assert.equal(new URL(base).hostname, '127.0.0.1', 'Synthetic E2E must use loopback');
}
assert.ok(process.env.CHROME_PATH, 'Set CHROME_PATH to an installed Chrome executable');
mkdirSync(OUT, { recursive: true });
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on('pageerror', e => errors.push(e.message));
page.on('console', e => { if (e.type() === 'error') errors.push(e.text()); });
const detail = async () => {
  const response = await fetch(`${API}/claims/${CLAIM}`);
  assert.equal(response.status, 200);
  return response.json();
};
async function waitStatus(status) {
  for (let i = 0; i < 80; i++) {
    const claim = await detail();
    if (claim.status === status) return claim;
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error(`Timed out waiting for actual backend status ${status}`);
}
try {
  await page.goto(WEB, { waitUntil: 'networkidle' });
  assert.equal(await page.title(), '智能ai财务审批系统');
  assert.equal(await page.locator('.logo-title').innerText(), '智能ai财务审批系统');
  assert.equal(await page.locator('.sidebar').evaluate(el => el.scrollWidth <= el.clientWidth), true,
    'Project branding must fit the sidebar');
  await page.screenshot({ path: `${OUT}/01-dashboard.png`, fullPage: true });
  const before = await detail();
  assert.equal(before.status, 'NEW', 'Start a fresh public Mock generation');
  assert.equal(before.erp_status, 'NOT_STARTED');
  await page.goto(`${WEB}/claims/${CLAIM}`, { waitUntil: 'networkidle' });
  await page.locator('.summary-card').waitFor();
  await page.getByRole('button', { name: /执行 AI 审核/ }).click();
  await waitStatus('PENDING_HUMAN');
  await page.reload({ waitUntil: 'networkidle' });
  assert.match(await page.locator('.summary-card').innerText(), /待人工复核/);
  await page.screenshot({ path: `${OUT}/02-ai-human-review.png`, fullPage: true });
  await page.goto(`${WEB}/claims/${CLAIM}/review`, { waitUntil: 'networkidle' });
  await page.getByLabel('复核人（演示身份）').fill('synthetic-public-finance');
  await page.getByLabel(/复核意见/).fill('合成演示：已独立核查事由和证据，批准本次人工复核。');
  await page.getByRole('button', { name: '通过', exact: true }).click();
  await page.getByText(/复核意见已保存/).waitFor();
  await page.screenshot({ path: `${OUT}/03-human-approval.png`, fullPage: true });
  await page.goto(`${WEB}/claims/${CLAIM}`, { waitUntil: 'networkidle' });
  await page.getByRole('button', { name: /恢复处理/ }).click();
  const complete = await waitStatus('COMPLETED');
  assert.equal(complete.decision_result.decision, 'APPROVE');
  assert.equal(complete.erp_status, 'SUCCEEDED');
  await page.reload({ waitUntil: 'networkidle' });
  assert.match(await page.locator('.summary-card').innerText(), /已完结/);
  await page.screenshot({ path: `${OUT}/04-completed.png`, fullPage: true });
  const refreshed = await detail();
  assert.equal(refreshed.status, 'COMPLETED');
  assert.equal(refreshed.erp_status, 'SUCCEEDED');
  await page.goto(`${WEB}/claims/${CLAIM}/audit`, { waitUntil: 'networkidle' });
  await page.locator('.tl-item').first().waitFor();
  const response = await fetch(`${API}/claims/${CLAIM}/audit-log?limit=100`);
  const log = await response.json();
  assert.ok(log.items.some(e => e.event_type.includes('HUMAN')));
  assert.ok(log.items.some(e => e.event_type.includes('ERP')));
  assert.equal(errors.length, 0, JSON.stringify(errors));
  await page.screenshot({ path: `${OUT}/05-audit-log.png`, fullPage: true });
  writeFileSync(`${OUT}/result.json`, JSON.stringify({ status: 'PASS_LEGACY_MOCK',
    full_enterprise: 'BLOCKED_PRIVATE_POLICY_PIN', actual_browser: true, api_interception: false,
    claim_id: CLAIM, final_status: refreshed.status, erp_status: refreshed.erp_status,
    audit_events: log.total, console_errors: errors }, null, 2));
  console.log('PASS: actual browser Mock -> human review -> approval -> ERP -> audit log');
} catch (error) {
  await page.screenshot({ path: `${OUT}/99-error.png`, fullPage: true });
  throw error;
} finally {
  await browser.close();
}
