import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { api, requestMoreInfo, USE_MOCK_API } from '../lib/api/client';
import { getRevision } from '../lib/mock/mock-store';
import { getRunContext, saveRunContext } from '../lib/session/runContext';
import { type ClaimDetail } from '../lib/api/types';
import Layout from '../components/Layout';
import RecordId from '../components/RecordId';
import { DemoMockTag, OutcomeBadge, RiskBadge, SeverityBadge, StatusBadge } from '../components/Badges';
import { Section } from '../components/ui';
import { useAsync } from '../hooks/useAsync';
import {
  CATEGORY_LABEL,
  CLAIM_STATUS_LABEL,
  DECISION_LABEL,
  fmtDate,
  fmtMoney,
  fmtPercent,
  isWithinWindow,
  POLICY_KEY_LABEL,
} from '../utils/format';
import { describeApiError } from '../utils/errors';

export default function HumanReview() {
  const { id = '' } = useParams();
  const navigate = useNavigate();
  const { data: claim, loading, error } = useAsync(() => api.getClaim(id), [id]);

  if (loading) {
    return (
      <Layout title="人工复核">
        <div className="loading">加载中…</div>
      </Layout>
    );
  }
  if (error || !claim) {
    return (
      <Layout title="人工复核">
        <div className="empty">加载失败：{error ?? '未找到单据'}</div>
      </Layout>
    );
  }
  return <ReviewBody claim={claim} onDone={() => navigate(`/claims/${claim.claim_id}`)} />;
}

function ReviewBody({ claim, onDone }: { claim: ClaimDetail; onDone: () => void }) {
  const dr = claim.decision_result;
  const runId = claim.active_run_id ?? '';

  const ctx = getRunContext(claim.claim_id);
  const [reviewerId, setReviewerId] = useState('finance_reviewer_01');
  const [comment, setComment] = useState('');
  // 真实模式：使用最近一次后端写操作返回的 revision；Mock 模式：沿用 Mock store 版本号
  const [expectedRevision, setExpectedRevision] = useState<number>(() =>
    USE_MOCK_API ? getRevision(claim.claim_id) : ctx?.revision ?? 1
  );
  const [submitting, setSubmitting] = useState<string | null>(null);
  const [result, setResult] = useState<{ ok: boolean; msg: string } | null>(null);

  const alreadyDone = claim.status !== 'PENDING_HUMAN';

  async function submit(decision: 'APPROVE' | 'REJECT') {
    setSubmitting(decision);
    setResult(null);
    try {
      const res = await api.submitHumanReview(claim.claim_id, {
        run_id: runId,
        expected_revision: expectedRevision,
        decision,
        reviewer_id: reviewerId,
        comment: comment || (decision === 'APPROVE' ? '核实后批准' : '不符合制度要求'),
        idempotency_key: `ui-review-${claim.claim_id}-${decision}-${Date.now()}`,
      });
      // 记录后端返回的新版本号，供下一步 resume 使用（前端不推导版本）
      saveRunContext(claim.claim_id, {
        run_id: res.run_id,
        revision: res.revision,
        review_submitted: true,
      });
      setResult({
        ok: true,
        msg: `复核意见已保存：${res.decision_result ? DECISION_LABEL[res.decision_result.decision] : '—'}。请返回详情页点击「恢复处理」，继续后续流程。`,
      });
      setTimeout(onDone, 2000);
    } catch (e) {
      setResult({ ok: false, msg: describeApiError(e, '提交人工复核') });
    } finally {
      setSubmitting(null);
    }
  }

  function requestInfo() {
    setSubmitting('REQUEST_INFO');
    setResult(null);
    try {
      requestMoreInfo(claim.claim_id, reviewerId, comment || '请补充事前审批记录');
      setResult({ ok: true, msg: '离线演示：已记录「要求补充材料」。此操作仅用于演示。' });
    } catch (e) {
      setResult({ ok: false, msg: `失败：${e instanceof Error ? e.message : String(e)}` });
    } finally {
      setSubmitting(null);
    }
  }

  const rules = dr?.rule_results ?? [];
  const evidence = dr?.policy_evidence ?? [];

  return (
    <Layout title="人工复核">
      {/* 顶部业务摘要：与报销详情页保持同一视觉语言 */}
      <div className="card review-summary">
        <div className="rs-main">
          <div>
            <div className="rs-main" style={{ marginBottom: 4 }}>
              <span className="rs-id">{claim.expense.city} · {CATEGORY_LABEL[claim.expense.category]}</span>
              <StatusBadge status={claim.status} />
              {USE_MOCK_API && <DemoMockTag text="演示表单" />}
            </div>
            <RecordId value={claim.claim_id} full copy />
            <div className="rs-meta">
              {claim.employee.name}（{claim.employee.department}）· {claim.expense.city} ·{' '}
              费用发生日 {fmtDate(claim.expense.incurred_on)}
            </div>
          </div>
        </div>
        <dl className="rs-metrics">
          <div>
            <dt>申报金额</dt>
            <dd>{fmtMoney(claim.expense.amount)}</dd>
          </div>
          <div>
            <dt>AI 建议</dt>
            <dd>{dr ? DECISION_LABEL[dr.decision] : '未获取'}</dd>
          </div>
          <div>
            <dt>风险等级</dt>
            <dd>{dr ? <RiskBadge level={dr.risk_level} /> : '未获取'}</dd>
          </div>
          <div>
            <dt>是否需人工介入</dt>
            <dd>{dr ? (dr.requires_human ? '需要' : '不需要') : '未获取'}</dd>
          </div>
        </dl>
        <div className="summary-actions">
          <Link to={`/claims/${claim.claim_id}`} className="btn">
            返回单据详情
          </Link>
        </div>
      </div>

      <div className="review-grid">
        <div className="review-col">
          <Section
            title="AI 审核判断（仅供参考，不具备审批权）"
            note="AI 分析结论不构成最终业务批准；是否通过取决于人工复核与硬性准入条件。"
          >
            {dr ? (
              <div className={`decision-banner ${dr.decision === 'HUMAN_REVIEW' ? 'human_review' : dr.decision.toLowerCase()}`}>
                <div>
                  <div className="d-label">AI 决策</div>
                  <div className="d-value">{DECISION_LABEL[dr.decision]}</div>
                </div>
                <div className="decision-side">
                  <div>置信度 <b>{fmtPercent(dr.confidence, 0)}</b>（证据完整度，不是审批权）</div>
                  <div>风险等级：<RiskBadge level={dr.risk_level} /></div>
                  <div>需人工介入：{dr.requires_human ? '是' : '否'}</div>
                </div>
              </div>
            ) : (
              <div className="empty">尚无 AI 决策结果</div>
            )}
            <div className="reason-block">
              <b>风险原因：</b>
              {dr?.reason ?? '—'}
            </div>
          </Section>

          <Section title="规则检查结果" extra={`${rules.length} 条`}>
            {rules.length ? (
              <>
                <div className="table-wrap">
                  <table className="table rule-table">
                    <thead>
                      <tr><th>规则编号</th><th>结果</th><th>严重级别</th><th>实际值</th><th>限额</th></tr>
                    </thead>
                    <tbody>
                      {rules.map((r) => (
                        <tr key={r.rule_id}>
                          <td className="cell-strong">{r.rule_id}</td>
                          <td><OutcomeBadge outcome={r.outcome} /></td>
                          <td><SeverityBadge severity={r.severity} /></td>
                          <td className="cell-dim">{r.actual ?? '—'}</td>
                          <td className="cell-dim">{r.limit ?? '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="rule-notes">
                  {rules.map((r) => (
                    <div key={r.rule_id}>
                      · <b>{r.rule_id}</b>：{r.reason}
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <div className="empty">尚无规则检查结果</div>
            )}
          </Section>

          <Section title="制度依据" extra={`${evidence.length} 版`}>
            {evidence.length ? (
              evidence.map((p) => {
                const inWindow = isWithinWindow(p, claim.expense.incurred_on);
                return (
                  <div key={p.policy_version_id} className={`policy-card ${inWindow ? 'current' : 'expired'}`}>
                    <div className="policy-head">
                      <div className="policy-name">{POLICY_KEY_LABEL[p.policy_key] ?? p.policy_key}</div>
                      <div className="policy-version">{p.version} 版</div>
                    </div>
                    <div className="policy-meta">
                      {p.policy_version_id} · {fmtDate(p.effective_from)} 至 {fmtDate(p.effective_to)}
                      <br />
                      <span className={`policy-window ${inWindow ? 'current' : 'expired'}`}>
                        {inWindow ? '适用：费用发生日在有效期内' : '仅作比对：费用发生日不在有效期内'}
                      </span>
                    </div>
                    <div className="policy-quote">{p.quote}</div>
                  </div>
                );
              })
            ) : (
              <div className="empty">尚无制度依据</div>
            )}
          </Section>
        </div>

        <div className="card action-panel">
          <div className="card-title">
            <span>复核操作</span>
            <span className="card-title-extra"><StatusBadge status={claim.status} /></span>
          </div>

          {alreadyDone ? (
            <>
              <div className="result-tip ok">
                该单据当前状态为「{CLAIM_STATUS_LABEL[claim.status]}」，无需人工复核。
              </div>
              <div className="action-text">
                人工复核只在「待人工复核」状态下可操作。若状态已推进，说明复核意见已记录或流程已完成。
              </div>
              <div className="action-buttons">
                <Link to={`/claims/${claim.claim_id}`} className="btn">
                  返回详情 →
                </Link>
              </div>
            </>
          ) : (
            <>
              <div className="field">
                <label className="label" htmlFor="reviewer-id">复核人（演示身份）</label>
                <input className="input" id="reviewer-id" value={reviewerId} onChange={(e) => setReviewerId(e.target.value)} />
              </div>
              <div className="field">
                <label className="label" htmlFor="review-comment">复核意见（必填，1–2000 字）</label>
                <textarea
                  className="textarea"
                  id="review-comment"
                  placeholder="例如：结合事前审批记录判断 / 版本交界期予以通过…"
                  value={comment}
                  onChange={(e) => setComment(e.target.value)}
                />
              </div>

              <div className="action-buttons">
                <button className="btn btn-success btn-lg" disabled={!!submitting} onClick={() => submit('APPROVE')}>
                  {submitting === 'APPROVE' ? '提交中…' : '通过'}
                </button>
                <button className="btn btn-danger btn-lg" disabled={!!submitting} onClick={() => submit('REJECT')}>
                  {submitting === 'REJECT' ? '提交中…' : '拒绝'}
                </button>
              </div>
              <div className="action-buttons">
                <button className="btn btn-lg" disabled={!!submitting} onClick={requestInfo}>
                  {submitting === 'REQUEST_INFO' ? '提交中…' : '要求补充材料（演示功能）'}
                </button>
              </div>

              {result && <div className={`result-tip ${result.ok ? 'ok' : 'err'}`}>{result.msg}</div>}

              {/* 记录编号与版本号是流程技术上下文，折叠保留原有可编辑能力 */}
              <details className="action-meta">
                <summary>技术上下文（记录编号 / 版本号）</summary>
                <div className="field" style={{ marginTop: 12 }}>
                  <label className="label" htmlFor="run-id">审核记录编号</label>
                  <input className="input" id="run-id" value={runId} readOnly />
                </div>
                <div className="field">
                  <label className="label" htmlFor="expected-revision">记录版本号</label>
                  <input
                    className="input"
                    id="expected-revision"
                    type="number"
                    min={0}
                    value={expectedRevision}
                    onChange={(e) => setExpectedRevision(Number(e.target.value))}
                  />
                </div>
              </details>

              <div className="action-text">
                <b>操作说明</b>
                <ol>
                  <li>人工复核可选择通过或拒绝，不能绕过硬性准入与阻断规则。</li>
                  <li>保存意见后需返回详情页恢复处理，审批通过后仍需完成 ERP 同步。</li>
                  <li>「要求补充材料」仅用于离线演示，在线处理暂不支持此操作。</li>
                </ol>
              </div>
            </>
          )}
        </div>
      </div>
    </Layout>
  );
}
