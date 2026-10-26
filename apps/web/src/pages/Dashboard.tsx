import { Link } from 'react-router-dom';
import { api, getDemoOnlyMetrics, USE_MOCK_API } from '../lib/api/client';
import { CLAIM_STATUSES, DECISIONS } from '../lib/api/enums';
import type { ClaimStatus, Decision } from '../lib/api/enums';
import type { ClaimSummary } from '../lib/api/types';
import Layout from '../components/Layout';
import RecordId from '../components/RecordId';
import { DecisionBadge, RiskBadge, StatusBadge } from '../components/Badges';
import { EmptyState } from '../components/ui';
import { useAsync } from '../hooks/useAsync';
import { CATEGORY_LABEL, CLAIM_STATUS_LABEL, DECISION_LABEL, fmtDateTime, fmtMoney, fmtPercent } from '../utils/format';

/**
 * 分布条配色：仅用于把已有枚举映射到设计令牌色，不参与任何业务判定。
 * 每个枚举值都必须能命中，否则回退为中性灰。
 */
const STATUS_TONE: Record<ClaimStatus, string> = {
  NEW: 'tone-gray',
  AUDITING: 'tone-blue',
  PENDING_HUMAN: 'tone-orange',
  PENDING_ERP: 'tone-orange',
  COMPLETED: 'tone-green',
  FAILED: 'tone-red',
};

const DECISION_TONE: Record<Decision, string> = {
  APPROVE: 'tone-green',
  REJECT: 'tone-red',
  HUMAN_REVIEW: 'tone-orange',
};

export default function Dashboard() {
  const { data, loading, error } = useAsync(() => api.getDashboardMetrics(), []);
  const demo = getDemoOnlyMetrics();

  return (
    <Layout title="工作台">
      <div className="page-header">
        <div>
          <div className="page-title">今日审核总览</div>
          <div className="page-desc">
            查看报销处理进度、审核结果与待办事项
          </div>
        </div>
        <div className="page-actions">
          <Link to="/claims/new" className="btn">
            + 新建报销
          </Link>
          <Link to="/claims" className="btn">
            查看全部报销单
          </Link>
        </div>
      </div>

      {loading && <div className="loading">加载中…</div>}
      {error && <div className="empty">加载失败：{error}</div>}

      {data && (
        <>
          <div className="kpi-grid">
            <div className="kpi-card">
              <div className="kpi-label">报销单总量</div>
              <div className="kpi-value">{data.total_claims}</div>
              <div className="kpi-meta">全部报销单</div>
            </div>
            <div className="kpi-card kpi-purple">
              <div className="kpi-label">待人工复核</div>
              <div className="kpi-value">{data.pending_human}</div>
              <div className="kpi-meta">等待复核处理</div>
            </div>
            <div className="kpi-card kpi-orange">
              <div className="kpi-label">待 ERP 同步</div>
              <div className="kpi-value">{data.pending_erp}</div>
              <div className="kpi-meta">ERP 同步未完成</div>
            </div>
            <div className="kpi-card kpi-green">
              <div className="kpi-label">已通过</div>
              <div className="kpi-value">{data.by_decision.APPROVE}</div>
              <div className="kpi-meta">审核结论为通过</div>
            </div>
            <div className="kpi-card kpi-red">
              <div className="kpi-label">已拒绝</div>
              <div className="kpi-value">{data.by_decision.REJECT}</div>
              <div className="kpi-meta">审核结论为拒绝</div>
            </div>
          </div>

          <div className="card">
            <div className="card-title">
              <span>单据流转分布</span>
              <span className="card-title-extra">合计 {data.total_claims} 张</span>
            </div>
            <div className="dist-grid">
              <div className="dist-block">
                <div className="dist-head">按单据状态</div>
                {CLAIM_STATUSES.map((s) => (
                  <DistRow
                    key={s}
                    name={CLAIM_STATUS_LABEL[s]}
                    count={data.by_status[s]}
                    total={data.total_claims}
                    tone={STATUS_TONE[s]}
                  />
                ))}
              </div>
              <div className="dist-block">
                <div className="dist-head">按审核结论</div>
                {DECISIONS.map((d) => (
                  <DistRow
                    key={d}
                    name={DECISION_LABEL[d]}
                    count={data.by_decision[d]}
                    total={data.total_claims}
                    tone={DECISION_TONE[d]}
                  />
                ))}
              </div>
            </div>
            <div className="section-note">
              占比为该状态（结论）单数 ÷ 报销单总量，只用于横向比较，不代表审批口径。
              单据状态与审核结论均由后端返回，前端不做推导。
            </div>
          </div>

          {/* Contract 未定义的演示指标：仅 Mock 模式展示 */}
          {USE_MOCK_API && demo && (
            <div className="card">
              <div className="card-title">
                <span>演示指标</span>
                <span className="card-title-extra">仅演示数据，不来自后端 Contract</span>
              </div>
              <div className="metric-grid">
                <div className="metric-card">
                  <div className="metric-head">
                    <span className="metric-label">自动化处理率</span>
                    <span className="metric-value">{fmtPercent(demo.automation_rate)}</span>
                  </div>
                  <div className="bar">
                    <div className="bar-fill green" style={{ width: `${demo.automation_rate * 100}%` }} />
                  </div>
                  <div className="metric-desc">无需人工介入即可完结的单据占比</div>
                </div>
                <div className="metric-card">
                  <div className="metric-head">
                    <span className="metric-label">人工复核率</span>
                    <span className="metric-value">{fmtPercent(demo.human_review_rate)}</span>
                  </div>
                  <div className="bar">
                    <div className="bar-fill" style={{ width: `${demo.human_review_rate * 100}%` }} />
                  </div>
                  <div className="metric-desc">转人工复核占比</div>
                </div>
                <div className="metric-card">
                  <div className="metric-head">
                    <span className="metric-label">误放行率</span>
                    <span className="metric-value">{fmtPercent(demo.false_approval_rate)}</span>
                  </div>
                  <div className="metric-desc">事后抽检误放行率（目标 &lt; 1%）</div>
                </div>
                <div className="metric-card">
                  <div className="metric-head">
                    <span className="metric-label">平均处理时长</span>
                    <span className="metric-value">{(demo.avg_processing_ms / 1000).toFixed(1)} s</span>
                  </div>
                  <div className="metric-desc">提交到出结论的平均端到端耗时</div>
                </div>
              </div>
            </div>
          )}

          <RecentClaims />
        </>
      )}
    </Layout>
  );
}

/** 分布行：数值全部来自后端返回的 by_status / by_decision */
function DistRow({
  name,
  count,
  total,
  tone,
}: {
  name: string;
  count: number;
  total: number;
  tone: string;
}) {
  const ratio = total > 0 ? count / total : 0;
  return (
    <div className={`dist-row${count === 0 ? ' is-zero' : ''}`}>
      <span className="dist-name">{name}</span>
      <span className="dist-bar">
        <span className={`dist-fill ${tone}`} style={{ width: `${ratio * 100}%` }} />
      </span>
      <span className="dist-count">{count}</span>
      <span className="dist-pct">{total > 0 ? fmtPercent(ratio, 0) : '—'}</span>
    </div>
  );
}

function RecentClaims() {
  const { data, loading } = useAsync(() => api.listClaims({ limit: 5, offset: 0 }), []);

  return (
    <div className="card">
      <div className="card-title">
        <span>最近报销单</span>
        <span className="card-title-extra">
          <Link to="/claims">查看全部 →</Link>
        </span>
      </div>
      {loading && <div className="loading">加载中…</div>}
      {data && data.items.length === 0 && (
        <EmptyState text="暂无报销单" hint="提交一张报销单后，这里会显示最近的处理结果。" />
      )}
      {data && data.items.length > 0 && <ClaimRows items={data.items} />}
    </div>
  );
}

function ClaimRows({ items }: { items: ClaimSummary[] }) {
  return (
    <div className="table-wrap">
      <table className="table table-dense">
        <thead>
          <tr>
            <th className="record-sequence" scope="col">序号</th>
            <th>报销事项</th>
            <th>申请人</th>
            <th className="cell-num">金额</th>
            <th>处理进度</th>
            <th>审核建议</th>
            <th>风险</th>
          </tr>
        </thead>
        <tbody>
          {items.map((c, index) => (
            <tr key={c.claim_id}>
              <td className="record-sequence">{index + 1}</td>
              <td className="record-subject"><Link to={`/claims/${c.claim_id}`}><strong>{c.expense.city} · {CATEGORY_LABEL[c.expense.category] ?? c.expense.category}{c.expense.category === 'HOTEL' ? ` · ${c.expense.nights} 晚` : ''}</strong></Link><small>发生日期 {c.expense.incurred_on} · 创建 {fmtDateTime(c.created_at)}</small><RecordId value={c.claim_id} /></td>
              <td className="cell-strong">{c.employee.name}<small className="cell-dim">{c.employee.department}</small></td>
              <td className="cell-money cell-num">{fmtMoney(c.expense.amount)}</td>
              <td><StatusBadge status={c.status} /></td>
              <td><DecisionBadge decision={c.decision_result?.decision ?? null} /></td>
              <td><RiskBadge level={c.decision_result?.risk_level ?? null} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
