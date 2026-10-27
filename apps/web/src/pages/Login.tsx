import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { Navigate } from 'react-router-dom';
import { portalHttp } from '../lib/api/enterprise';
import { useAuth } from '../lib/session/AuthContext';

interface Challenge { captcha_id: string; image: string; expires_in: number }

function Shield({ className }: { className?: string }) {
  return <svg className={className} width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6l-8-3Z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" /><path d="m8.5 12 2.5 2.5 4.5-5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>;
}

export default function Login() {
  const { user, login } = useAuth();
  const [company, setCompany] = useState('DEMO-CN');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [challenge, setChallenge] = useState<Challenge | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [captchaError, setCaptchaError] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const codeInput = useRef<HTMLInputElement>(null);
  const active = useRef(false);
  const refreshTask = useRef<Promise<void> | null>(null);
  const refreshCaptcha = useCallback(() => {
    // Reuse an in-flight request, including React StrictMode's repeated mount effect.
    // This keeps the displayed challenge and its HttpOnly binding cookie together.
    if (refreshTask.current) return refreshTask.current;
    setRefreshing(true); setChallenge(null); setCode(''); setCaptchaError('');
    const task = (async () => {
      try {
        const data = await portalHttp<Challenge>('/auth/captcha');
        if (active.current) setChallenge(data);
      } catch (e) {
        if (active.current) setCaptchaError(e instanceof Error ? e.message : '验证码加载失败，请重试');
      } finally {
        if (active.current) setRefreshing(false);
        refreshTask.current = null;
      }
    })();
    refreshTask.current = task;
    return task;
  }, []);
  useEffect(() => {
    active.current = true;
    void refreshCaptcha();
    return () => { active.current = false; };
  }, [refreshCaptcha]);
  useEffect(() => {
    if (!challenge) return;
    const timer = window.setTimeout(() => {
      setChallenge(null); setCode(''); setCaptchaError('验证码已过期，请换一张');
    }, challenge.expires_in * 1000);
    return () => window.clearTimeout(timer);
  }, [challenge]);
  if (user) return <Navigate to={user.role === 'EMPLOYEE' ? '/my-claims' : '/workbench'} replace />;
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || refreshing || !challenge) return;
    setBusy(true); setError('');
    try { await login(company.trim(), username.trim(), password, challenge.captcha_id, code.trim()); setPassword(''); }
    catch (e) {
      setError(e instanceof Error ? e.message : '登录失败，请重试');
      await refreshCaptcha(); codeInput.current?.focus();
    } finally { setBusy(false); }
  }
  return <main className="portal-login">
    <div className="portal-login-shell">
      <section className="portal-login-intro" aria-labelledby="login-headline">
        <div className="portal-login-brand"><span className="portal-brand-mark"><Shield /></span><span>智能ai财务审批系统<small>AI FINANCIAL APPROVAL SYSTEM</small></span></div>
        <div className="portal-login-story"><span className="portal-eyebrow"><i />让每一笔报销，清晰可循</span>
          <h1 id="login-headline">报销有进度，<br />审核<span>有依据。</span></h1>
          <p>从员工提交到财务审核，<br className="portal-intro-break" />让制度、票据与每一次决定有据可查。</p>
          <ol className="portal-login-steps">
            <li><span>01</span><div><strong>制度先行</strong><small>按企业制度校验费用与票据</small></div></li>
            <li><span>02</span><div><strong>智能协助</strong><small>AI 分析风险，财务作出最终决定</small></div></li>
            <li><span>03</span><div><strong>全程留痕</strong><small>每次提交、修改与审核都有记录</small></div></li>
          </ol>
        </div>
        <div className="portal-login-intro-footer">
          <div className="portal-login-roles"><span>员工提交</span><i /><span>财务审核</span><i /><span>管理员维护</span></div>
          {import.meta.env.VITE_M6_DEMO === 'true' && <div className="portal-demo-info">本机演示 · ERP 使用模拟服务 · AI 模型见每单审核记录<br />演示业务日期 {import.meta.env.VITE_M6_BUSINESS_DATE ?? '2026-11-03'}，不代表当前真实日期。</div>}
        </div>
        <div className="portal-intro-orbit" aria-hidden="true" />
      </section>
      <form onSubmit={submit} className="portal-login-form card" aria-labelledby="login-title">
        <div className="portal-login-form-heading"><span className="portal-login-welcome">欢迎回来</span><h2 id="login-title">登录企业工作区</h2><p>使用企业分配给你的个人账号。</p></div>
        <div className="portal-login-field"><label className="label" htmlFor="login-company">企业编号</label><input className="input" id="login-company" required maxLength={128} value={company} onChange={e => setCompany(e.target.value)} disabled={busy} autoCapitalize="off" spellCheck={false} /></div>
        <div className="portal-login-field"><label className="label" htmlFor="login-username">账号</label><input className="input" id="login-username" autoComplete="username" required value={username} onChange={e => setUsername(e.target.value)} disabled={busy} maxLength={100} placeholder="请输入个人账号" autoCapitalize="off" spellCheck={false} /></div>
        <div className="portal-login-field"><label className="label" htmlFor="login-password">密码</label><input className="input" id="login-password" type="password" autoComplete="current-password" required maxLength={128} value={password} onChange={e => setPassword(e.target.value)} disabled={busy} placeholder="请输入密码" /></div>
        <div className="portal-login-field"><label className="label" htmlFor="login-captcha">验证码</label>
          <div className="portal-captcha-row"><input ref={codeInput} className="input" id="login-captcha" required minLength={4} maxLength={4} value={code} onChange={e => setCode(e.target.value)} disabled={busy || !challenge} placeholder="输入图中字符" autoComplete="off" autoCapitalize="off" spellCheck={false} aria-describedby="captcha-help" />
            <button className="portal-captcha-image" type="button" onClick={() => { void refreshCaptcha(); }} disabled={busy || refreshing} aria-label="换一张验证码" title="点击换一张验证码">{challenge ? <img src={challenge.image} alt="四位字母数字验证码" width="160" height="56" /> : <span>{refreshing ? '正在加载…' : '点击重新加载'}</span>}</button>
          </div>
          <div id="captcha-help" className="portal-captcha-help"><span>不区分大小写 · 2 分钟内有效</span><button type="button" onClick={() => { void refreshCaptcha(); }} disabled={busy || refreshing}>换一张 <span aria-hidden="true">↻</span></button></div>
          {captchaError && <div role="alert" className="portal-captcha-error">{captchaError}</div>}
        </div>
        {error && <div role="alert" className="alert alert-danger">{error}</div>}
        <button className="btn btn-primary btn-lg portal-login-submit" disabled={busy || refreshing || !challenge}>{busy ? '正在登录…' : '登录工作区'}<span aria-hidden="true">→</span></button>
        <div className="portal-login-privacy"><Shield /><small>按账号权限进入工作区<br />员工仅能查看自己的报销</small></div>
      </form>
    </div>
    <footer className="portal-login-page-footer">制度有标准 · 审核有依据 · 过程可追溯</footer>
  </main>;
}
