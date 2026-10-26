import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api, USE_MOCK_API } from '../lib/api/client';
import { type ClaimDetail } from '../lib/api/types';
import { getRunContext, saveRunContext } from '../lib/session/runContext';
import Layout from '../components/Layout';
import RecordId from '../components/RecordId';
import RecoveryPanel from '../components/RecoveryPanel';
import {
  CategoryBadge,
  DecisionBadge,
  DemoMockTag,
  ErpBadge,
  OutcomeBadge,
  ParseBadge,
  RiskBadge,
  SeverityBadge,
  StatusBadge,
} from '../components/Badges';
import { useAsync } from '../hooks/useAsync';
import {
  CATEGORY_LABEL,
  CLAIM_STATUS_LABEL,
  DECISION_LABEL,
  EVENT_TYPE_LABEL,
  fmtDate,
  fmtDateTime,
  fmtMoney,
  fmtPercent,
  isWithinWindow,
  POLICY_KEY_LABEL,
  RISK_LABEL,
} from '../utils/format';
import { describeApiError } from '../utils/errors';
import { invoiceHttp, type InvoiceResponse } from '../lib/api/invoices';
import InvoiceEvidenceCard from '../components/InvoiceEvidenceCard';
import { CheckItem, KvRow, Metric, Section } from '../components/ui';
import AuditTimeline from '../components/AuditTimeline';

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** 详情底部时间线最多展示的事件条数（完整记录仍在审计追踪页） */
const DETAIL_AUDIT_LIMIT = 20;

/** 审计事件中的人工审批类事件（事件名全部来自后端，前端不创造） */
const HUMAN_DECISION_EVENTS = [
  'HUMAN_REVIEW_REQUIRED',
  'HUMAN_REVIEW_REQUESTED',
  'HUMAN_REVIEW_SUBMITTED',
  'HUMAN_REVIEW_RECORDED',
  'HUMAN_APPROVED',
  'HUMAN_REJECTED',
  'MORE_INFO_REQUESTED',
];

export default function ClaimDetail() {
  const { id = '' } = useParams();
  const { data: claim, loading, error, reload } = useAsync(() => api.getClaim(id), [id]);
  useEffect(() => {
    if (claim?.status !== 'AUDITING') return;
    const timer = setInterval(reload, 1500);
    return () => clearInterval(timer);
  }, [claim?.status, reload]);

  return (
    <Layout title={`报销单 ${id}`}>
      {loading && <div className="loading">加载中…</div>}
      {error && <div className="empty">加载失败：{error}</div>}
      {claim && claim.claim_id === id && <DetailBody key={claim.claim_id} claim={claim} onReload={reload} />}
    </Layout>
  );
}

function DetailBody({ claim, onReload }: { claim: ClaimDetail; onReload: () => void }) {
  const dr = claim.decision_result;
  const needReview = claim.status === 'PENDING_HUMAN';
  const canRunAudit = claim.status === 'NEW';
  const dynamic = !USE_MOCK_API && /^CLM-\d{4}-[a-f0-9]{32}$/.test(claim.claim_id);
  const { data: submitted, error: submissionError } = useAsync(
    () => dynamic ? invoiceHttp<InvoiceResponse>(`/claims/${claim.claim_id}`) : Promise.resolve(null),
    [claim.claim_id, claim.updated_at],
  );
  useEffect(() => {
    if (submitted?.operation) saveRunContext(claim.claim_id, {
      run_id: submitted.operation.run_id, revision: submitted.operation.revision,
      review_submitted: submitted.human_review_submitted,
    });
  }, [submitted, claim.claim_id]);

  const ctx = getRunContext(claim.claim_id);
  /** 人工意见已提交且仍停在 PENDING_HUMAN → 需要调用 resume 推进流程 */
  const canResume =
    (claim.status === 'PENDING_HUMAN' && !!(submitted?.human_review_submitted ?? ctx?.review_submitted)) ||
    claim.status === 'PENDING_ERP' ||
    claim.erp_status === 'FAILED';

  /** PENDING_ERP 由恢复面板承载操作，顶部不再重复按钮 */
  const isPendingErp = claim.status === 'PENDING_ERP';
  const showTopResume = canResume && !isPendingErp;

  /** 人工复核意见是否已提交（来自后端返回，非前端推断） */
  const reviewSubmitted = !!(submitted?.human_review_submitted ?? ctx?.review_submitted);

  const [running, setRunning] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [auditMsg, setAuditMsg] = useState<{ ok: boolean; text: string } | null>(null);

  /* ---------------- 顶部指标：全部来自现有接口，缺失显示“未获取” ---------------- */

  /** 适用制度限额：取接口返回的规则限额字段（第一条带 limit 的规则结果） */
  const limitRule = (dr?.rule_results ?? []).find((r) => r.limit != null && r.limit !== '');
  const limitValue = limitRule?.limit ? fmtMoney(limitRule.limit) : '未获取';
  const limitNote = limitRule ? `来自规则 ${limitRule.rule_id}` : '接口未返回适用限额';
  const riskValue = dr ? RISK_LABEL[dr.risk_level] : '未获取';
  const riskTone = !dr ? undefined : dr.risk_level === 'HIGH' ? 'var(--danger)' : dr.risk_level === 'MEDIUM' ? 'var(--warning)' : 'var(--success)';

  /* ---------------- 底部审计时间线 ---------------- */

  const { data: log, loading: logLoading, error: logError } = useAsync(
    () => api.getAuditLog(claim.claim_id, { limit: DETAIL_AUDIT_LIMIT, offset: 0 }),
    [claim.claim_id, claim.updated_at],
  );
  const auditItems = log?.items ?? [];
  const humanEvents = auditItems.filter((e) => HUMAN_DECISION_EVENTS.includes(e.event_type));

  /* ---------------- 规则检查：结论均来自 DecisionResult ---------------- */

  const policyEvidence = dr?.policy_evidence ?? [];
  const inWindowCount = policyEvidence.filter((p) => isWithinWindow(p, claim.expense.incurred_on)).length;
  const invoiceWarnings = submitted?.invoice_evidence?.warnings ?? [];

  async function runAudit() {
    setRunning(true);
    setAuditMsg(null);
    try {
      const res = await api.runAudit(claim.claim_id, {
        idempotency_key: `ui-audit-${claim.claim_id}-${Date.now()}`,
        mode: 'LIVE',
      });
      // 只记忆后端返回的 run_id / revision，供下一步 human-review / resume 使用
      saveRunContext(claim.claim_id, { run_id: res.run_id, revision: res.revision });

      if (res.decision_result) {
        setAuditMsg({
          ok: true,
          text: `审核完成：${DECISION_LABEL[res.decision_result.decision]}，风险等级：${RISK_LABEL[res.decision_result.risk_level]}。`,
        });
        onReload();
      } else {
        // Contract：audit 返回 202 仅表示任务已持久化，前端轮询 GET detail
        setAuditMsg({
          ok: true,
          text: `审核任务已提交，正在等待审核结果…`,
        });
        for (let i = 0; i < 10; i += 1) {
          await sleep(1200);
          const latest = await api.getClaim(claim.claim_id);
          if (latest.status !== 'AUDITING') break;
        }
        onReload();
      }
    } catch (e) {
      setAuditMsg({ ok: false, text: describeApiError(e, '执行 AI 审核') });
    } finally {
      setRunning(false);
    }
  }

  async function handleResume() {
    setResuming(true);
    setAuditMsg(null);
    try {
      const c = getRunContext(claim.claim_id);
      const res = await api.resume(claim.claim_id, {
        run_id: claim.active_run_id ?? c?.run_id ?? '',
        expected_revision: c?.revision ?? 0,
        idempotency_key: `ui-resume-${claim.claim_id}-${Date.now()}`,
      });
      saveRunContext(claim.claim_id, { run_id: res.run_id, revision: res.revision, review_submitted: false });
      setAuditMsg({
        ok: true,
        text: isPendingErp
          ? `正在恢复 ERP 同步，已完成的审核不会重复执行。`
          : `恢复请求已提交，系统将从上次中断的位置继续处理。`,
      });
      for (let i = 0; i < 10; i += 1) {
        await sleep(1200);
        const latest = await api.getClaim(claim.claim_id);
        if (latest.status === 'COMPLETED' || latest.status === 'FAILED') break;
      }
      onReload();
    } catch (e) {
      setAuditMsg({ ok: false, text: describeApiError(e, '恢复处理') });
    } finally {
      setResuming(false);
    }
  }

  return (
    <>
      {/* ---------------- 顶部业务摘要 ---------------- */}
      <div className="card summary-card">
        <div className="summary-head">
          <div>
            <div className="summary-id">{claim.expense.city} · {CATEGORY_LABEL[claim.expense.category]}</div>
            <RecordId value={claim.claim_id} full copy />
            <div className="summary-badges">
              <StatusBadge status={claim.status} />
              <DecisionBadge decision={dr?.decision ?? null} />
              <RiskBadge level={dr?.risk_level ?? null} />
              <ErpBadge status={claim.erp_status} />
              {USE_MOCK_API && <DemoMockTag text="预置演示数据" />}
            </div>
            <div className="summary-meta">
              <span>
                申请人 <b>{claim.employee.name}</b>
              </span>
              <span>
                所属部门 <b>{claim.employee.department}</b>
              </span>
              <span>
                费用类型 <b>{CATEGORY_LABEL[claim.expense.category] ?? claim.expense.category}</b>
              </span>
              <span>
                费用日期 <b>{fmtDate(claim.expense.incurred_on)}</b>
              </span>
              {claim.active_run_id ? (
                <span>
                  运行编号 <b>{claim.active_run_id.slice(0, 8)}…</b>
                </span>
              ) : null}
            </div>
          </div>
          <div className="summary-actions">
            {canRunAudit && (
              <button className="btn btn-primary" disabled={running || resuming} onClick={runAudit}>
                {running ? '审核执行中…' : '▶ 执行 AI 审核'}
              </button>
            )}
            {showTopResume && (
              <button className="btn btn-primary" disabled={running || resuming} onClick={handleResume}>
                {resuming ? '恢复处理中…' : '⟳ 恢复处理'}
              </button>
            )}
            {needReview && (
              <Link to={`/claims/${claim.claim_id}/review`} className="btn">
                进入人工复核
              </Link>
            )}
            <Link to={`/claims/${claim.claim_id}/audit`} className="btn">
              审计追踪
            </Link>
          </div>
        </div>

        <div className="summary-metrics">
          <Metric
            label="申请金额"
            value={fmtMoney(claim.expense.amount)}
            note={`币种 ${claim.expense.currency}`}
          />
          <Metric label="适用制度限额" value={limitValue} note={limitNote} muted={!limitRule} />
          <Metric
            label="风险等级"
            value={riskValue}
            tone={riskTone}
            muted={!dr}
            note={dr ? `置信度 ${fmtPercent(dr.confidence, 0)}（证据完整度，不是审批权）` : 'AI 审核尚未产生结论'}
          />
        </div>
      </div>

      {/* UI 层临时状态：审核中 / 恢复中，业务状态 Enum 仍以 API Contract 为准 */}
      {(running || resuming || claim.status === 'AUDITING') && (
        <div className="result-tip" style={{ marginBottom: 12 }}>
          {resuming ? '流程恢复中，请等待处理结果…' : 'AI 审核执行中，请等待审核结果…'}
        </div>
      )}

      {auditMsg && (
        <div className={`result-tip ${auditMsg.ok ? 'ok' : 'err'}`} style={{ marginBottom: 12 }}>
          {auditMsg.text}
        </div>
      )}

      {/* M3：仅当后端公开状态为 PENDING_ERP 时展示（业务已完成 + ERP 故障挂起） */}
      <RecoveryPanel
        status={claim.status}
        erpStatus={claim.erp_status}
        decision={dr?.decision ?? null}
        busy={resuming}
        onResume={handleResume}
      />

      {submissionError && <div role="alert" className="result-tip err">申请详情加载失败：{submissionError}</div>}

      <div className="detail-grid">
        {/* ==================== 左栏：申请与证据 ==================== */}
        <div className="detail-col">
          <Section title="报销申请信息">
            <div className="kv-list">
              <KvRow k="申请人" v={claim.employee.name} />
              <KvRow k="员工编号" v={claim.employee.employee_id} />
              <KvRow k="所属部门" v={claim.employee.department} />
              <KvRow k="法人主体" v={claim.employee.legal_entity} />
              <KvRow k="职位" v={submitted?.submission.position ?? '未获取'} />
              <KvRow k="费用类型" v={<CategoryBadge category={claim.expense.category} />} />
              <KvRow k="出差城市" v={claim.expense.city} />
              <KvRow k="费用日期" v={fmtDate(claim.expense.incurred_on)} />
              <KvRow
                k="出差天数"
                v={submitted ? `${submitted.trip_days} 天` : '未获取'}
              />
              <KvRow
                k="住宿晚数"
                v={
                  submitted?.submission.nights != null
                    ? `${submitted.submission.nights} 晚`
                    : claim.expense.category === 'HOTEL'
                      ? `${claim.expense.nights} 晚`
                      : '不适用'
                }
              />
              <KvRow
                k="申报金额"
                v={
                  <span className="cell-money">
                    {fmtMoney(claim.expense.amount)} {claim.expense.currency}
                  </span>
                }
              />
              <KvRow k="开票单位（销售方）" v={submitted?.submission.seller ?? '未获取'} />
              <KvRow k="报销事项" v={submitted?.submission.purpose ?? '未获取'} />
              <KvRow k="特殊说明" v={submitted?.submission.notes || '无'} />
            </div>
          </Section>

          {submitted && (
            <Section
              title="结构化申报与 AI 分析"
              note="仅审核结构化申报。尚未核验发票或外部员工身份。"
            >
              <div className="kv-list">
                <KvRow
                  k="行程"
                  v={`${submitted.submission.trip_start} 至 ${submitted.submission.trip_end}`}
                />
                <KvRow k="住宿晚数" v={submitted.submission.nights ?? '不适用'} />
                <KvRow k="开票单位（销售方）" v={submitted.submission.seller} />
                <KvRow k="AI 分析说明" v={submitted.llm_analysis?.summary ?? '尚未产生分析结果'} />
                <KvRow
                  k="分析来源"
                  v={
                    submitted.llm_analysis
                      ? `${submitted.llm_analysis.provider} / ${submitted.llm_analysis.model}`
                      : '尚无结果'
                  }
                />
                <KvRow
                  k="ERP Mock 写入结果"
                  v={
                    submitted.erp_effects.length
                      ? submitted.erp_effects.map((e) => `${e.source}: ${e.status}`).join('；')
                      : '未写入'
                  }
                />
              </div>
            </Section>
          )}

          {/* 发票证据：识别原值与人工修正值分别展示 */}
          {submitted?.invoice_evidence && (
            <InvoiceEvidenceCard claimId={claim.claim_id} evidence={submitted.invoice_evidence} />
          )}

          <Section
            title={dynamic ? '结构化申报记录' : `相关附件 · ${claim.documents.length} 个`}
            note={
              <>
                {dynamic ? '申报快照校验值（未核验发票）：' : '演示附件未经过发票核验，文件校验值为占位：'}
                {claim.documents[0]?.sha256 ? `${claim.documents[0].sha256.slice(0, 12)}…` : '—'}
              </>
            }
          >
            {claim.documents.length === 0 ? (
              <div className="section-note">接口未返回附件记录</div>
            ) : (
              claim.documents.map((d) => (
                <div className="attachment-item" key={d.document_id}>
                  <ParseBadge status={d.parse_status} />
                  <span className="att-name">{d.file_name}</span>
                  <span className="att-size">{d.content_type.split('/')[1] ?? d.content_type}</span>
                </div>
              ))
            )}
          </Section>

          <Section
            title="单据状态"
            note="审批通过后仍需完成 ERP 同步，请分别查看审核结论与同步状态。"
          >
            <div className="kv-list">
              <KvRow k="单据状态" v={<StatusBadge status={claim.status} />} />
              <KvRow k="ERP 同步状态" v={<ErpBadge status={claim.erp_status} />} />
              <KvRow k="创建时间" v={fmtDateTime(claim.created_at)} />
              <KvRow k="更新时间" v={fmtDateTime(claim.updated_at)} />
            </div>
          </Section>
        </div>

        {/* ==================== 右栏：审核与审批结果 ==================== */}
        <div className="detail-col">
          <Section title="AI 审核结果" extra={USE_MOCK_API ? <DemoMockTag text="预置演示数据" /> : undefined}>
            {!dr && (
              <div className="decision-banner pending">
                <div>
                  <div className="d-label">AI 决策</div>
                  <div className="d-value">尚无决策结果</div>
                </div>
                <div className="decision-side">
                  审核结果尚未生成
                  <br />
                  {claim.status === 'NEW' ? '点击右上角「执行 AI 审核」启动流程。' : '审核流程正在处理，请稍后查看结果。'}
                  <br />
                  <Link to={`/claims/${claim.claim_id}/audit`}>查看审计追踪 →</Link>
                </div>
              </div>
            )}

            {dr && (
              <>
                <div
                  className={`decision-banner ${dr.decision === 'HUMAN_REVIEW' ? 'human_review' : dr.decision.toLowerCase()}`}
                >
                  <div>
                    <div className="d-label">AI 决策</div>
                    <div className="d-value">{DECISION_LABEL[dr.decision]}</div>
                  </div>
                  <div className="decision-side">
                    <div>
                      置信度 <b>{fmtPercent(dr.confidence, 0)}</b>
                      <span className="confidence-track" style={{ marginLeft: 8 }}>
                        <span
                          className="confidence-fill"
                          style={{ width: `${dr.confidence * 100}%`, display: 'block' }}
                        />
                      </span>
                    </div>
                    <div>
                      风险等级：{RISK_LABEL[dr.risk_level]} · 需人工介入：{dr.requires_human ? '是' : '否'}
                    </div>
                    <div>决策时间：{fmtDateTime(dr.decided_at)}</div>
                  </div>
                </div>

                <div className="reason-block">
                  <b>判定说明：</b>
                  {dr.reason}
                </div>

                <div className="section-note">审核记录编号：{dr.run_id}</div>
              </>
            )}
          </Section>

          <Section
            title="制度依据"
            extra={USE_MOCK_API ? <DemoMockTag text="预置演示数据" /> : undefined}
            note={
              policyEvidence.length > 0
                ? '制度按费用发生日期匹配，请结合适用版本及审核规则查看结果。'
                : undefined
            }
          >
            {!dr && <div className="empty">尚无决策结果，因此没有制度证据</div>}

            {policyEvidence.map((p) => {
              const inWindow = isWithinWindow(p, claim.expense.incurred_on);
              return (
                <div key={p.policy_version_id} className={`policy-card ${inWindow ? 'current' : 'expired'}`}>
                  <div className="policy-head">
                    <div className="policy-name">{POLICY_KEY_LABEL[p.policy_key] ?? p.policy_key}</div>
                    <div className="policy-version">{p.version} 版</div>
                  </div>
                  <div className="policy-meta">
                    制度版本编号：{p.policy_version_id}
                    <br />
                    有效期：{fmtDate(p.effective_from)} 起{p.effective_to ? `，${fmtDate(p.effective_to)} 当日失效` : '，长期有效'}
                    <br />
                    {inWindow ? '✅ 费用发生日在有效期内（当前适用版本）' : '⛔ 费用发生日不在有效期内（仅作比对）'}
                  </div>
                  <div className="policy-quote">{p.quote}</div>
                  <div className="policy-source">
                    <span className="badge badge-no-dot badge-tag-gray">条款切片：{p.chunk_id}</span>
                    <span className="badge badge-no-dot badge-tag-gray">
                      相关度：{p.relevance_score ?? '无'}
                    </span>
                  </div>
                  <div className="section-note" style={{ wordBreak: 'break-all' }}>
                    来源：{p.source_uri}
                  </div>
                </div>
              );
            })}
          </Section>

          <Section title="规则检查">
            <div className="check-list">
              <CheckItem
                state={!dr ? 'info' : dr.hard_gate_passed ? 'pass' : 'fail'}
                label="硬性准入（Hard Gate）"
                detail={
                  !dr
                    ? '尚未执行审核，未获取硬门结果'
                    : dr.hard_gate_passed
                      ? '已通过硬性准入校验'
                      : '未通过硬性准入，存在阻断项，无法审批通过'
                }
              />
              <CheckItem
                state={
                  !limitRule ? 'info' : limitRule.outcome === 'PASS' ? 'pass' : limitRule.outcome === 'FAIL' ? 'fail' : 'warn'
                }
                label="金额与限额比对"
                detail={
                  limitRule
                    ? `规则 ${limitRule.rule_id}：实际值 ${limitRule.actual ?? '—'} / 限额 ${limitRule.limit ?? '—'}`
                    : '接口未返回可比对限额'
                }
              />
              <CheckItem
                state={
                  !submitted
                    ? 'info'
                    : !submitted.invoice_evidence
                      ? 'info'
                      : invoiceWarnings.length > 0
                        ? 'warn'
                        : 'pass'
                }
                label="发票一致性"
                detail={
                  !submitted
                    ? '结构化申报记录未获取'
                    : !submitted.invoice_evidence
                      ? '本次申报未附带已确认的发票证据'
                      : invoiceWarnings.length > 0
                        ? invoiceWarnings.map((w) => w.message).join('；')
                        : '已确认发票字段，接口未返回一致性告警'
                }
              />
              <CheckItem
                state={
                  !dr ? 'info' : policyEvidence.length === 0 ? 'warn' : inWindowCount > 0 ? 'pass' : 'warn'
                }
                label="制度命中与适用性"
                detail={
                  !dr
                    ? '尚未执行审核，未获取制度证据'
                    : policyEvidence.length === 0
                      ? '未检索到适用制度条款，需人工确认'
                      : `命中 ${policyEvidence.length} 条制度条款，其中 ${inWindowCount} 条在费用发生日有效`
                }
              />
              <CheckItem
                state={dr?.requires_human ? 'warn' : 'info'}
                label="需人工确认事项"
                detail={dr ? (dr.requires_human ? 'AI 结论要求人工介入' : 'AI 结论未要求人工介入') : '尚未执行审核'}
              />
            </div>

            {dr && dr.rule_results.length > 0 && (
              <div className="sub-section">
                <div className="sub-title">规则计算结果</div>
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>规则编号</th>
                        <th>结果</th>
                        <th>严重级别</th>
                        <th>实际值</th>
                        <th>限额</th>
                      </tr>
                    </thead>
                    <tbody>
                      {dr.rule_results.map((r) => (
                        <tr key={r.rule_id}>
                          <td>
                            <div className="cell-strong">{r.rule_id}</div>
                            <div className="cell-dim">{r.policy_version_id ?? '—'}</div>
                          </td>
                          <td>
                            <OutcomeBadge outcome={r.outcome} />
                          </td>
                          <td>
                            <SeverityBadge severity={r.severity} />
                          </td>
                          <td className="cell-dim">{r.actual ?? '—'}</td>
                          <td className="cell-dim">{r.limit ?? '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div style={{ marginTop: 10 }}>
                  {dr.rule_results.map((r) => (
                    <div key={r.rule_id} style={{ fontSize: 12, color: 'var(--text-2)', lineHeight: 1.8 }}>
                      · <b>{r.rule_id}</b>：{r.reason}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </Section>

          <Section
            title="审批操作"
            note="人工复核不能绕过硬性准入与阻断规则；最终审批权限由后端校验。"
            className="approval-panel"
          >
            <div className="approval-row">
              <span className="ar-label">当前状态</span>
              <span className="ar-value">
                <StatusBadge status={claim.status} />
              </span>
            </div>
            <div className="approval-row">
              <span className="ar-label">复核状态</span>
              <span className="ar-value">
                {claim.status === 'PENDING_HUMAN'
                  ? reviewSubmitted
                    ? '复核意见已提交，需返回本页点击「恢复处理」继续流程'
                    : '待人工复核，尚未提交复核意见'
                  : `${CLAIM_STATUS_LABEL[claim.status]}，当前无需人工复核`}
              </span>
            </div>
            <div className="approval-row">
              <span className="ar-label">AI 建议</span>
              <span className="ar-value">
                {dr ? `${DECISION_LABEL[dr.decision]} · 需人工介入：${dr.requires_human ? '是' : '否'}` : '尚无 AI 决策结果'}
              </span>
            </div>
            <div className="approval-row">
              <span className="ar-label">审批记录</span>
              <span className="ar-value">
                {humanEvents.length === 0 ? (
                  '暂无人工审批事件记录'
                ) : (
                  <>
                    {humanEvents.map((e) => (
                      <div key={e.event_id}>
                        {EVENT_TYPE_LABEL[e.event_type] ?? e.event_type}
                        {' · '}
                        {fmtDateTime(e.occurred_at)}
                        {' · '}
                        {e.actor}
                      </div>
                    ))}
                  </>
                )}
              </span>
            </div>

            {dr && !dr.hard_gate_passed && (
              <div className="blocked-note" role="alert">
                <span>硬性准入未通过（Hard Block）：按现行制度该报销单不允许批准，人工复核无法绕过。</span>
              </div>
            )}

            {claim.status !== 'PENDING_HUMAN' && (
              <div className="section-note">
                人工复核入口仅在当前状态为「待人工复核」时可用，当前状态为「{CLAIM_STATUS_LABEL[claim.status]}」。
              </div>
            )}

            <div className="approval-actions">
              <Link
                to={`/claims/${claim.claim_id}/review`}
                className={`btn${claim.status === 'PENDING_HUMAN' ? ' btn-primary' : ''}`}
                aria-disabled={claim.status !== 'PENDING_HUMAN'}
              >
                人工复核页
              </Link>
              <Link to={`/claims/${claim.claim_id}/audit`} className="btn">
                审计追踪
              </Link>
            </div>
          </Section>
        </div>
      </div>

      {/* ---------------- 底部审计时间线 ---------------- */}
      <div className="card timeline-card">
        <div className="card-title">
          <span>审计时间线</span>
          <span className="card-title-extra">
            仅展示后端实际记录的事件（最近 {DETAIL_AUDIT_LIMIT} 条 / 共 {log?.total ?? 0} 条）
          </span>
        </div>
        <AuditTimeline
          items={auditItems}
          loading={logLoading}
          error={logError}
          compact
          emptyHint="执行审核或人工复核后，事件会出现在这里。"
        />
        <div className="section-note">
          <Link to={`/claims/${claim.claim_id}/audit`}>查看完整审计追踪 →</Link>
        </div>
      </div>
    </>
  );
}
