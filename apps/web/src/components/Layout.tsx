import { NavLink } from 'react-router-dom';
import { DEMO_CLAIMS, INTEGRATION_CASE_ID, USE_MOCK_API } from '../lib/api/client';
import { useState } from 'react';
import { ENTERPRISE_MODE, HOTEL_DEMO_MODE, ROLE_LABEL } from '../lib/api/enterprise';
import { useAuth } from '../lib/session/AuthContext';
import { NavIcon, type IconName } from './icons';

/** Demo Case 跳转（仅 Mock 模式存在这些单号） */
const DEMO_CASE_LINKS: { claimId: string; label: string; to?: string }[] = [
  { claimId: 'CLM-2026-1024', label: 'Case 1 · 合规自动通过' },
  { claimId: 'CLM-2026-1025', label: 'Case 2 · 超限确定性拒绝' },
  { claimId: 'CLM-2026-1026', label: 'Case 3 · 制度版本交界' },
  { claimId: 'CLM-2026-1027', label: 'Case 4 · 制度未覆盖' },
  { claimId: 'CLM-2026-1028', label: 'Case 5 · ERP 超时恢复', to: 'audit' },
];

/** 主导航（业务功能顺序与数量保持原样，仅补充图标） */
const MAIN_LINKS: { to: string; label: string; icon: IconName; end?: boolean }[] = [
  { to: '/', label: '工作台', icon: 'dashboard', end: true },
  { to: '/claims', label: '报销列表', icon: 'list' },
  { to: '/claims/new', label: '新建报销', icon: 'create' },
  { to: '/policies', label: '企业制度管理', icon: 'policy' },
];

function NavItem({ to, end, icon, children }: { to: string; end?: boolean; icon: IconName; children: React.ReactNode }) {
  return (
    <NavLink to={to} end={end} className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}>
      <span className="nav-icon">
        <NavIcon name={icon} />
      </span>
      <span className="nav-text">{children}</span>
    </NavLink>
  );
}

export default function Layout({ title, children }: { title: string; children: React.ReactNode }) {
  const { user, logout } = useAuth();
  const [logoutError, setLogoutError] = useState('');
  /** 企业门户导航：与主站保持同一套图标语言 */
  const links: { to: string; label: string; icon: IconName; end?: boolean }[] = user?.role === 'EMPLOYEE'
    ? [
        { to: '/my-claims', label: '我的报销', icon: 'list', end: true },
        { to: HOTEL_DEMO_MODE ? '/hotel-matrix' : '/my-claims/new', label: HOTEL_DEMO_MODE ? '申请住宿费' : '新建报销', icon: 'create', end: true },
      ]
    : [
        { to: '/workbench', label: '企业工作台', icon: 'dashboard' },
        { to: '/company/claims', label: user?.role === 'FINANCE' ? '财务审核' : '企业报销', icon: 'invoice' },
        ...(['ADMIN', 'FINANCE'].includes(user?.role ?? '')
          ? [{ to: '/policies', label: '企业制度', icon: 'policy' as IconName }] : []),
        ...(user?.role === 'ADMIN'
          ? [
              { to: '/accounts', label: '账号管理', icon: 'accounts' as IconName },
            ]
          : []),
        { to: '/company/audit', label: '审计记录', icon: 'audit' },
      ];
  if (user?.role === 'ADMIN' && import.meta.env.VITE_P4A_ENABLED === 'true') {
    links.push({ to: '/hotel-matrix', label: '住宿职级标准', icon: 'policy' });
  }
  if (user && import.meta.env.VITE_P4A_ENABLED === 'true') {
    links.push({ to: '/hotel-records', label: '住宿审核记录', icon: 'audit', end: true });
  }
  /** 系统模式：必须如实反映数据来源，不得展示为正式生产环境 */
  const modeLabel = USE_MOCK_API
    ? '离线演示 · Mock 数据'
    : import.meta.env.VITE_M6_DEMO === 'true'
      ? '本机演示 · 合成数据'
      : '已连接后端 API';
  const modeClass = USE_MOCK_API || import.meta.env.VITE_M6_DEMO === 'true' ? 'badge-tag-orange' : 'badge-tag-green';

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-logo">
          <div className="logo-mark" aria-hidden="true">
            <NavIcon name="shield" size={18} color="#ffffff" />
          </div>
          <div>
            <div className="logo-title">费控 AI 审核</div>
            <div className="logo-sub">FDE Expense AI Auditor</div>
          </div>
        </div>
        <nav className="sidebar-nav">
          {ENTERPRISE_MODE ? links.map((link) => (
            <NavItem key={link.to} to={link.to} end={link.end} icon={link.icon}>
              {link.label}
            </NavItem>
          )) : <>
          {MAIN_LINKS.map((link) => (
            <NavItem key={link.to} to={link.to} end={link.end} icon={link.icon}>
              {link.label}
            </NavItem>
          ))}
          {!USE_MOCK_API && (
            <>
              <div className="nav-group-title">示例报销单</div>
              <NavItem to={`/claims/${INTEGRATION_CASE_ID}`} icon="file">
                {INTEGRATION_CASE_ID}
              </NavItem>
            </>
          )}
          {USE_MOCK_API && (
            <>
              <div className="nav-group-title">离线演示场景</div>
              {DEMO_CASE_LINKS.map((c) => (
                <NavItem
                  key={c.claimId}
                  to={`/claims/${c.claimId}${c.to ? `/${c.to}` : ''}`}
                  icon="file"
                >
                  {c.label}
                </NavItem>
              ))}
            </>
          )}
          </>}
        </nav>
        <div className="sidebar-footer">{modeLabel}</div>
      </aside>
      <div className="main">
        <header className="topbar">
          <div className="topbar-title">{title}</div>
          <div className="topbar-meta">
            <span className={`badge badge-no-dot ${modeClass}`}>{modeLabel}</span>
            <span className="topbar-divider" aria-hidden="true" />
            {ENTERPRISE_MODE && user ? (
              <>
                <span className="topbar-context">
                  {user.name} · {ROLE_LABEL[user.role]}
                </span>
                <button className="btn btn-sm" onClick={() => logout().catch(e => setLogoutError(e.message))}>
                  退出登录
                </button>
              </>
            ) : (
              <span className="topbar-context">财务共享服务中心</span>
            )}
          </div>
        </header>
        <main className="content">
          {import.meta.env.VITE_M6_DEMO === 'true' && (
            <div className="page-width demo-notice" role="note" data-testid="m6-demo-notice">
              合成数据演示：业务日期 {import.meta.env.VITE_M6_BUSINESS_DATE ?? '2026-11-03'}，
              此日期用于演示，不代表当前真实日期。
              {ENTERPRISE_MODE ? 'ERP 同步使用模拟服务；AI 模式以页面提示和审核结果为准。制度原文在本机解析。' : 'AI 分析和 ERP 同步使用模拟服务，制度检索使用离线测试数据。'}
            </div>
          )}
          <div className="page-width">{children}</div>
          {logoutError && <div className="page-width alert alert-danger" role="alert">{logoutError}</div>}
        </main>
      </div>
    </div>
  );
}

export { DEMO_CLAIMS };
