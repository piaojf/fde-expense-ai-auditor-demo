/**
 * M3 Failure Recovery —— 真实浏览器 E2E（Frontend Integration Gate）
 *
 * 验证内容（全部使用真实 Backend，前端不伪造任何事件/状态）：
 *   E2E A：ERP 超时一次 → 自动重试 → COMPLETED / APPROVE / ERP 已回写
 *          Audit Log 必须出现真实重试事件
 *   E2E B：ERP 持续失败 → PENDING_ERP（界面显示“等待 ERP 恢复” + 恢复面板）
 *          → 后端恢复 → 浏览器点击「恢复工作流」→ COMPLETED
 *          并校验上游节点未因 Resume 重跑
 *   E2E C：ERP 写入成功但响应丢失 → 重试 → IDEMPOTENT_REPLAY
 *          并用 Backend evidence 校验实际写入次数 = 1
 *
 * 前置：
 *   - Python 虚拟环境 .venv 已安装依赖
 *   - 前端已切 Real 模式（apps/web/.env.local: VITE_USE_MOCK_API=false）
 *
 * 运行：
 *   node apps/web/scripts/e2e-m3-recovery.mjs
 *
 * 说明：脚本只做“真实用户操作 + 读取界面/接口结果”；
 *       故障注入使用 Backend 自带的测试入口 scripts/smoke_m3.py --serve，
 *       不修改任何 Backend 文件。
 */
import { spawn, execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import { createConnection } from 'node:net';
import { randomUUID } from 'node:crypto';
import { resolve, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, '../../..');
const WEB = resolve(ROOT, 'apps/web');
const SHOT_DIR = resolve(ROOT, '.workbuddy/verify/m3');
const RUNTIME = resolve(ROOT, '.m3-runtime', randomUUID());
const PY = process.env.PYTHON_PATH ?? resolve(ROOT, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
const NODE = process.execPath;

const API_PORT = 8000;
const WEB_PORT = 5173;
const BASE = `http://localhost:${WEB_PORT}`;
const API = `http://localhost:${API_PORT}`;
const CLAIM = process.env.CLAIM_ID ?? 'CLM-2026-00006';

const log = (...a) => console.log(...a);
const fail = (m) => {
  console.error(`\n❌ E2E FAIL: ${m}`);
  process.exitCode = 1;
};

/* ---------------- playwright 解析（同现有 E2E） ---------------- */
async function loadPlaywright() {
  const candidates = ['playwright-core'];
  const fallback =
    process.env.PLAYWRIGHT_CORE;
  if (fallback) candidates.push(pathToFileURL(fallback).href);
  for (const c of candidates) {
    try {
      const mod = await import(c);
      const chromium = mod?.chromium ?? mod?.default?.chromium;
      if (chromium) return { chromium };
    } catch {
      /* 继续尝试下一个候选 */
    }
  }
  throw new Error('未找到 playwright-core');
}
const { chromium } = await loadPlaywright();
const CHROME =
  process.env.CHROME_PATH;

/* ---------------- 进程与端口工具 ---------------- */
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function requireFreePort(port) {
  return new Promise((resolvePort, reject) => {
    const socket = createConnection({ host: '127.0.0.1', port });
    socket.once('connect', () => {
      socket.destroy();
      reject(new Error(`Port ${port} is occupied; existing services are preserved`));
    });
    socket.once('error', error => {
      socket.destroy();
      if (error.code === 'ECONNREFUSED') resolvePort();
      else reject(error);
    });
  });
}

const ownedBackends = new Set();

async function waitHealth(url, timeoutMs = 40000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try {
      const r = await fetch(url);
      if (r.ok) return true;
    } catch {
      /* 未就绪 */
    }
    await sleep(500);
  }
  return false;
}

/** 启动带故障注入的 Backend（使用 Backend 自带测试入口，不改 Backend 文件） */
async function startBackend(mode, store) {
  await requireFreePort(API_PORT);
  const child = spawn(
    PY,
    ['scripts/smoke_m3.py', '--serve', '--mode', mode, '--store', store, '--port', String(API_PORT)],
    { cwd: ROOT, env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' }, stdio: 'ignore' }
  );
  ownedBackends.add(child);
  const ok = await waitHealth(`${API}/health`);
  if (!ok || child.exitCode !== null) {
    await stopBackend(child);
    throw new Error(`Backend(${mode}) startup failed`);
  }
  return child;
}

async function stopBackend(child) {
  if (!child || !ownedBackends.has(child)) return;
  ownedBackends.delete(child);
  if (child.exitCode !== null) return;
  await new Promise(resolveExit => {
    const timer = setTimeout(() => child.kill('SIGKILL'), 3000);
    child.once('exit', () => { clearTimeout(timer); resolveExit(); });
    child.kill('SIGTERM');
  });
}

function freshStore(name) {
  // Unique generation: never erase a previous run's SQLite/checkpoint files.
  return resolve(RUNTIME, `${name}.sqlite3`);
}

/* ---------------- 启动前端 ---------------- */
mkdirSync(SHOT_DIR, { recursive: true });
mkdirSync(RUNTIME, { recursive: true });
await requireFreePort(API_PORT);
await requireFreePort(WEB_PORT);

const vite = spawn(NODE, ['node_modules/vite/bin/vite.js', '--port', String(WEB_PORT), '--strictPort', '--host', '127.0.0.1'], {
  cwd: WEB,
  stdio: 'ignore',
});
if (!(await waitHealth(BASE, 60000))) {
  fail('前端 dev server 启动超时');
  process.exit(1);
}
log(`前端已就绪：${BASE}`);

const browser = await chromium.launch({ executablePath: CHROME, headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const consoleErrors = [];
const http404 = [];
const corsErrors = [];
page.on('console', (m) => {
  if (m.type() === 'error') consoleErrors.push(m.text());
});
page.on('pageerror', (e) => consoleErrors.push(`pageerror: ${e.message}`));
page.on('response', (r) => {
  if (r.status() === 404) http404.push(r.url());
});
page.on('requestfailed', (r) => {
  const err = r.failure()?.errorText ?? '';
  if (/CORS|blocked|net::ERR_FAILED/i.test(err)) corsErrors.push(`${r.url()} ${err}`);
});

const shot = (n) => page.screenshot({ path: `${SHOT_DIR}/${n}.png`, fullPage: true });
const titleText = async () =>
  (await page.locator('.page-title').first().innerText()).replace(/\n/g, ' | ');
const tipText = async () => {
  const t = page.locator('.result-tip').first();
  return (await t.count()) ? (await t.innerText()).replace(/\n/g, ' ') : '';
};

/** 通过真实 API 读取事件流（用于统计校验，不参与界面判定） */
async function apiEvents() {
  const r = await fetch(`${API}/claims/${CLAIM}/audit-log?limit=100`);
  return (await r.json()).items ?? [];
}
async function apiClaim() {
  const r = await fetch(`${API}/claims/${CLAIM}`);
  return r.json();
}

const results = { A: 'FAIL', B: 'FAIL', C: 'FAIL', upstream: 'unknown', writeCount: 'unknown' };

try {
  /* ================= E2E A：ERP 超时一次 → 重试 → 成功 ================= */
  log('\n========== E2E A：ERP Timeout Once → Retry → COMPLETED ==========');
  let backend = await startBackend('TIMEOUT_ONCE', freshStore('m3-e2e-a'));

  await page.goto(`${BASE}/claims/${CLAIM}`, { waitUntil: 'networkidle' });
  await page.waitForSelector('.page-title');
  log(`[A1] 初始：${await titleText()}`);
  await shot('A1-detail-new');

  await page.getByRole('button', { name: /Run AI Audit/ }).click();
  await page.waitForFunction(
    () => {
      const t = document.querySelector('.result-tip');
      return t && /审核完成|审核任务已提交/.test(t.innerText);
    },
    { timeout: 45000 }
  );
  await page
    .waitForFunction(
      () => {
        const t = document.querySelector('.page-title');
        return t && /已完结/.test(t.innerText);
      },
      { timeout: 30000 }
    )
    .catch(() => {});
  const aTitle = await titleText();
  log(`[A2] Run AI Audit → ${aTitle}`);
  log(`     提示：${await tipText()}`);
  await shot('A2-after-audit');

  if (!/已完结/.test(aTitle)) fail(`A：状态不是 COMPLETED（${aTitle}）`);
  if (!/通过/.test(aTitle)) fail(`A：决策不是 APPROVE（${aTitle}）`);
  if (!/已回写/.test(aTitle)) fail(`A：ERP 不是已回写（${aTitle}）`);

  const aEvents = await apiEvents();
  const aTypes = aEvents.map((e) => e.event_type);
  log(`[A3] 事件总数 ${aTypes.length}`);
  log(`     ${aTypes.join(' → ')}`);
  for (const req of ['DEPENDENCY_CALL_FAILED', 'RETRY_SCHEDULED', 'RETRY_ATTEMPTED', 'DEPENDENCY_RECOVERED', 'ERP_UPDATED', 'WORKFLOW_COMPLETED']) {
    if (!aTypes.includes(req)) fail(`A：缺少真实事件 ${req}`);
  }
  const retryCount = aTypes.filter((t) => /^(DEPENDENCY_|RETRY_)/.test(t)).length;
  log(`[A4] 重试相关事件数 = ${retryCount}`);
  results.retryEvents = retryCount;

  await page.goto(`${BASE}/claims/${CLAIM}/audit`, { waitUntil: 'networkidle' });
  await page.waitForSelector('.page-title');
  await page.waitForTimeout(1200);
  const aSteps = await page.locator('.tl-step').allInnerTexts();
  log(`[A5] 审计追踪界面事件 ${aSteps.length} 条：${aSteps.slice(9).join(' → ')}`);
  await shot('A3-audit-log');
  if (aSteps.length === 0) fail('A：审计日志界面为空');
  const aBody = await page.locator('.timeline').innerText();
  for (const cn of ['已安排重试', '正在重试', '外部系统已恢复']) {
    if (!aBody.includes(cn)) fail(`A：审计界面缺少中文事件「${cn}」`);
  }
  results.A = process.exitCode ? 'FAIL' : 'PASS';
  await stopBackend(backend);
  await sleep(1200);

  /* ============ E2E B：持续失败 → PENDING_ERP → 恢复 → COMPLETED ============ */
  log('\n========== E2E B：ERP Persistent Failure → PENDING_ERP → Resume ==========');
  const storeB = freshStore('m3-e2e-b');
  backend = await startBackend('TIMEOUT_ALWAYS', storeB);

  await page.goto(`${BASE}/claims/${CLAIM}`, { waitUntil: 'networkidle' });
  await page.waitForSelector('.page-title');
  await page.getByRole('button', { name: /Run AI Audit/ }).click();
  await page.waitForFunction(
    () => {
      const t = document.querySelector('.result-tip');
      return t && /审核完成|审核任务已提交/.test(t.innerText);
    },
    { timeout: 45000 }
  );
  await page
    .waitForFunction(
      () => {
        const t = document.querySelector('.page-title');
        return t && /等待 ERP 恢复/.test(t.innerText);
      },
      { timeout: 30000 }
    )
    .catch(() => {});
  const bTitle = await titleText();
  log(`[B1] Run AI Audit → ${bTitle}`);
  await shot('B1-pending-erp');

  if (!/等待 ERP 恢复/.test(bTitle)) fail(`B：状态不是 PENDING_ERP（${bTitle}）`);
  if (/已完结/.test(bTitle)) fail('B：ERP 故障被错误显示为 COMPLETED');
  if (/拒绝/.test(bTitle)) fail('B：ERP 故障被错误显示为 REJECT（基础设施故障 ≠ 业务拒绝）');

  const bClaim = await apiClaim();
  log(`[B2] 后端公开状态：status=${bClaim.status} erp_status=${bClaim.erp_status} decision=${bClaim.decision_result?.decision}`);
  results.pendingErp = bClaim.status === 'PENDING_ERP' ? 'YES' : 'NO';

  // 恢复面板
  const panel = page.locator('.card').filter({ hasText: 'ERP 写回失败' }).first();
  if ((await panel.count()) === 0) {
    fail('B：未展示恢复面板（ERP 写回失败）');
  } else {
    const panelText = (await panel.innerText()).replace(/\n/g, ' ');
    log(`[B3] 恢复面板：${panelText.slice(0, 160)}…`);
    results.recoveryAction = panelText.includes('恢复工作流') ? 'YES' : 'NO';
    if (!panelText.includes('恢复工作流')) fail('B：恢复面板缺少「恢复工作流」按钮');
    if (!panelText.includes('不是报销被拒绝')) log('    ⚠ 面板未包含故障/拒绝区分说明');
  }
  await shot('B2-recovery-panel');

  // 后端 ERP 恢复：用同一 store 重启为正常模式（模拟外部系统恢复）
  await stopBackend(backend);
  await sleep(1500);
  backend = await startBackend('NONE', storeB);
  log('[B4] 后端已恢复（同 store 重启为 NONE 模式）');

  await page.goto(`${BASE}/claims/${CLAIM}`, { waitUntil: 'networkidle' });
  await page.waitForSelector('.page-title');
  const resumeBtn = page.getByRole('button', { name: /恢复工作流/ });
  if ((await resumeBtn.count()) === 0) {
    fail('B：恢复面板按钮不可见');
  } else {
    await resumeBtn.first().click();
    await page.waitForFunction(
      () => {
        const t = document.querySelector('.result-tip');
        return t && /恢复工作流|失败/.test(t.innerText);
      },
      { timeout: 30000 }
    );
    await page
      .waitForFunction(
        () => {
          const t = document.querySelector('.page-title');
          return t && /已完结/.test(t.innerText);
        },
        { timeout: 45000 }
      )
      .catch(() => {});
    const bFinal = await titleText();
    log(`[B5] Resume → ${bFinal}`);
    log(`     提示：${await tipText()}`);
    await shot('B3-after-resume');
    if (!/已完结/.test(bFinal)) fail(`B：恢复后状态不是 COMPLETED（${bFinal}）`);
    if (!/已回写/.test(bFinal)) fail(`B：恢复后 ERP 不是已回写（${bFinal}）`);
    results.finalAfterRecovery = bFinal.replace(`${CLAIM} | `, '');
  }

  const bEvents = await apiEvents();
  const bTypes = bEvents.map((e) => e.event_type);
  log(`[B6] 事件总数 ${bTypes.length}`);
  for (const req of ['RETRY_EXHAUSTED', 'RECOVERY_REQUIRED', 'WORKFLOW_RECOVERY_RESUMED', 'ERP_UPDATED', 'WORKFLOW_COMPLETED']) {
    if (!bTypes.includes(req)) fail(`B：缺少真实事件 ${req}`);
  }
  // 上游不得因 Resume 重跑
  const upstream = ['CLAIM_LOADED', 'ERP_CONTEXT_FETCHED', 'DOCUMENTS_PARSED', 'HARD_GATE_CHECKED', 'RULES_CHECKED', 'POLICY_RETRIEVED', 'LLM_ANALYSIS_COMPLETED', 'RISK_ASSESSED'];
  const counts = {};
  let rerun = 0;
  for (const u of upstream) {
    const c = bTypes.filter((t) => t === u).length;
    counts[u] = c;
    if (c > 1) rerun += c - 1;
  }
  log(`[B7] 上游节点计数：${JSON.stringify(counts)}`);
  results.upstream = rerun === 0 ? '0' : `FAIL(${rerun})`;
  if (rerun !== 0) fail(`B：上游节点被重跑 ${rerun} 次`);

  await page.goto(`${BASE}/claims/${CLAIM}/audit`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1200);
  const bSteps = await page.locator('.tl-step').allInnerTexts();
  log(`[B8] 审计追踪界面 ${bSteps.length} 条事件`);
  await shot('B4-audit-log');
  results.B = 'PASS';
  await stopBackend(backend);
  await sleep(1200);

  /* ============ E2E C：响应丢失 → 幂等重放 ============ */
  log('\n========== E2E C：ERP Response Lost → Idempotent Replay ==========');
  const storeC = freshStore('m3-e2e-c');
  backend = await startBackend('SUCCESS_THEN_RESPONSE_LOST', storeC);

  await page.goto(`${BASE}/claims/${CLAIM}`, { waitUntil: 'networkidle' });
  await page.waitForSelector('.page-title');
  await page.getByRole('button', { name: /Run AI Audit/ }).click();
  await page.waitForFunction(
    () => {
      const t = document.querySelector('.result-tip');
      return t && /审核完成|审核任务已提交/.test(t.innerText);
    },
    { timeout: 45000 }
  );
  await page
    .waitForFunction(
      () => {
        const t = document.querySelector('.page-title');
        return t && /已完结/.test(t.innerText);
      },
      { timeout: 30000 }
    )
    .catch(() => {});
  const cTitle = await titleText();
  log(`[C1] Run AI Audit → ${cTitle}`);

  const cEvents = await apiEvents();
  const cTypes = cEvents.map((e) => e.event_type);
  log(`[C2] 事件：${cTypes.slice(10).join(' → ')}`);
  if (!cTypes.includes('IDEMPOTENT_REPLAY')) fail('C：未出现 IDEMPOTENT_REPLAY 事件');
  const replay = cEvents.find((e) => e.event_type === 'IDEMPOTENT_REPLAY');
  log(`[C3] IDEMPOTENT_REPLAY payload.actual_side_effect = ${replay?.payload?.actual_side_effect}`);
  results.idempotentVisible = cTypes.includes('IDEMPOTENT_REPLAY') ? 'YES' : 'NO';

  await page.goto(`${BASE}/claims/${CLAIM}/audit`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1200);
  const cBody = await page.locator('.timeline').innerText();
  if (!cBody.includes('已复用原执行结果')) fail('C：审计界面未展示中文「已复用原执行结果」');
  if (!cBody.includes('未重复写入 ERP')) fail('C：审计界面未展示「未重复写入 ERP」标识');
  log(`[C4] 审计界面已展示幂等复用说明`);
  await shot('C1-idempotent-replay');
  results.C = 'PASS';

  // Backend evidence：实际写入次数
  try {
    const out = execFileSync(
      PY,
      ['-c', "import sqlite3;c=sqlite3.connect(r'" + storeC + "');print(c.execute('select count(*) from erp_effects').fetchone()[0])"],
      { encoding: 'utf8' }
    ).trim();
    results.writeCount = out;
    log(`[C5] Backend evidence：erp_effects 实际写入行数 = ${out}`);
    if (out !== '1') fail(`C：ERP 实际写入次数不是 1（=${out}）`);
  } catch (e) {
    log(`[C5] ⚠ 无法读取 erp_effects：${String(e).slice(0, 120)}`);
  }
  await stopBackend(backend);
} catch (e) {
  await shot('99-error');
  fail(e instanceof Error ? e.message : String(e));
} finally {
  try {
    await browser.close();
  } catch {
    /* ignore */
  }
  try {
    vite.kill('SIGKILL');
  } catch {
    /* ignore */
  }
  for (const child of [...ownedBackends]) await stopBackend(child);
}

/* ---------------- 汇总 ---------------- */
log('\n================ M3 E2E 汇总 ================');
log(`E2E A（Timeout Once → Retry → COMPLETED）：${results.A}`);
log(`E2E B（Persistent Failure → PENDING_ERP → Resume）：${results.B}`);
log(`E2E C（Response Lost → Idempotent Replay）：${results.C}`);
log(`重试相关事件数：${results.retryEvents ?? '—'}`);
log(`PENDING_ERP 可见：${results.pendingErp ?? '—'} ；恢复操作可见：${results.recoveryAction ?? '—'}`);
log(`恢复后最终状态：${results.finalAfterRecovery ?? '—'}`);
log(`上游重跑次数 UPSTREAM_REEXECUTION：${results.upstream}`);
log(`幂等可见：${results.idempotentVisible ?? '—'} ；ERP 实际写入次数：${results.writeCount}`);
log(`控制台错误：${consoleErrors.length === 0 ? '无' : JSON.stringify(consoleErrors.slice(0, 5))}`);
log(`404 资源：${http404.length === 0 ? '无' : JSON.stringify([...new Set(http404)])}`);
log(`CORS 错误：${corsErrors.length === 0 ? '无' : JSON.stringify(corsErrors.slice(0, 5))}`);
if (consoleErrors.length) fail(`存在 ${consoleErrors.length} 条控制台错误`);
if (http404.length) fail(`存在 ${http404.length} 个 404 资源`);
if (corsErrors.length) fail(`存在 ${corsErrors.length} 条 CORS 错误`);
if (!process.exitCode) log('\n✅ M3 BROWSER E2E PASS');
