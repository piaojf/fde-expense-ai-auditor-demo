import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api } from '../lib/api/client';
import { CLAIM_STATUSES } from '../lib/api/enums';
import type { ClaimStatus } from '../lib/api/enums';
import Layout from '../components/Layout';
import RecordId from '../components/RecordId';
import { DecisionBadge, RiskBadge, StatusBadge } from '../components/Badges';
import { EmptyState } from '../components/ui';
import { useAsync } from '../hooks/useAsync';
import { CATEGORY_LABEL, CLAIM_STATUS_LABEL, fmtDateTime, fmtMoney } from '../utils/format';

type StatusFilter = 'ALL' | ClaimStatus;

export default function ClaimsList() {
  const navigate = useNavigate();
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('ALL');
  const [q, setQ] = useState('');

  const { data, loading, error } = useAsync(
    () => api.listClaims({ limit: 50, offset: 0, status: statusFilter === 'ALL' ? undefined : statusFilter }),
    [statusFilter]
  );

  const items = (data?.items ?? []).filter((c) => {
    if (!q) return true;
    const s = q.trim().toLowerCase();
    return (
      c.claim_id.toLowerCase().includes(s) ||
      c.employee.name.toLowerCase().includes(s) ||
      c.employee.department.toLowerCase().includes(s) ||
      c.expense.city.toLowerCase().includes(s) ||
      (CATEGORY_LABEL[c.expense.category] ?? '').includes(s)
    );
  });

  const hasFilter = statusFilter !== 'ALL' || q.trim().length > 0;

  function clearFilter() {
    setStatusFilter('ALL');
    setQ('');
  }

  return (
    <Layout title="报销列表">
      <div className="page-header">
        <div>
          <div className="page-title">报销单列表</div>
          <div className="page-desc">
            按报销事项查看审核进度、AI 建议与制度依据
          </div>
        </div>
        <div className="page-actions">
          <input
            className="input"
            aria-label="搜索报销记录"
            placeholder="搜索城市 / 员工 / 部门 / 单号"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <Link to="/claims/new" className="btn">
            + 新建报销
          </Link>
        </div>
      </div>

      <div className="card">
        <div className="list-toolbar">
          <div className="filter-bar">
            <button
              className={`filter-chip${statusFilter === 'ALL' ? ' active' : ''}`}
              onClick={() => setStatusFilter('ALL')}
            >
              全部状态
            </button>
            {CLAIM_STATUSES.map((s) => (
              <button
                key={s}
                className={`filter-chip${statusFilter === s ? ' active' : ''}`}
                onClick={() => setStatusFilter(s)}
              >
                {CLAIM_STATUS_LABEL[s]}
              </button>
            ))}
          </div>
          <div className="list-toolbar-meta">
            <span>
              匹配 <b>{data?.total ?? 0}</b> 条
              {hasFilter ? <> · 当前显示 <b>{items.length}</b> 条</> : null}
            </span>
            {hasFilter ? (
              <button className="link-btn" onClick={clearFilter}>
                清除筛选
              </button>
            ) : null}
          </div>
        </div>

        {loading && <div className="loading">加载中…</div>}
        {error && <div className="empty">加载失败：{error}</div>}
        {data && items.length === 0 && (
          <EmptyState
            text={hasFilter ? '没有符合条件的报销单' : '暂无报销单'}
            hint={
              hasFilter ? (
                <button className="link-btn" onClick={clearFilter}>
                  清除筛选条件
                </button>
              ) : (
                <>
                  还没有提交过报销单，可先
                  <Link to="/claims/new"> 新建报销</Link>。
                </>
              )
            }
          />
        )}

        {items.length > 0 && (
          <div className="table-wrap">
            <table className="table table-dense table-claims">
              <thead>
                <tr>
                  <th className="record-sequence" scope="col">序号</th>
                  <th>报销事项</th>
                  <th>申请人</th>
                  <th className="cell-num">金额</th>
                  <th>AI 风险</th>
                  <th>审核建议</th>
                  <th>处理进度</th>
                  <th className="col-actions">操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((c, index) => (
                  <tr key={c.claim_id} onClick={() => navigate(`/claims/${c.claim_id}`)}>
                    <td className="record-sequence">{index + 1}</td>
                    <td className="record-subject"><Link to={`/claims/${c.claim_id}`} onClick={(e) => e.stopPropagation()}><strong>{c.expense.city} · {CATEGORY_LABEL[c.expense.category] ?? c.expense.category}{c.expense.category === 'HOTEL' ? ` · ${c.expense.nights} 晚` : ''}</strong></Link><small>发生日期 {c.expense.incurred_on} · 创建 {fmtDateTime(c.created_at)}</small><RecordId value={c.claim_id} /></td>
                    <td className="cell-strong">{c.employee.name}<small className="cell-dim">{c.employee.department}</small></td>
                    <td className="cell-money cell-num">{fmtMoney(c.expense.amount)}</td>
                    <td><RiskBadge level={c.decision_result?.risk_level ?? null} /></td>
                    <td><DecisionBadge decision={c.decision_result?.decision ?? null} /></td>
                    <td><StatusBadge status={c.status} /></td>
                    <td className="col-actions">
                      {c.status === 'PENDING_HUMAN' ? (
                        <Link
                          to={`/claims/${c.claim_id}/review`}
                          onClick={(e) => e.stopPropagation()}
                          className="btn btn-sm"
                        >
                          人工复核
                        </Link>
                      ) : (
                        <Link
                          to={`/claims/${c.claim_id}`}
                          onClick={(e) => e.stopPropagation()}
                          className="btn btn-sm"
                        >
                          查看
                        </Link>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Layout>
  );
}
