import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { portalHttp } from '../lib/api/enterprise';
interface Profile { id: string; name: string; department: string; position: string; grade: string; grade_effective_on: string | null }
export default function TravelProfiles() {
  const [items, setItems] = useState<Profile[]>([]); const [uid, setUid] = useState('');
  const [grade, setGrade] = useState('UNKNOWN'); const [effective, setEffective] = useState('');
  const [reason, setReason] = useState(''); const [error, setError] = useState('');
  const [busy, setBusy] = useState(false); const [notice, setNotice] = useState('');
  useEffect(() => { portalHttp<{ items: Profile[] }>('/travel/profiles').then(d => setItems(d.items)).catch(e => setError(e.message)); }, []);
  async function save() {
    if (busy || !uid || !effective || !reason.trim()) return;
    setBusy(true); setError(''); setNotice('');
    try {
      await portalHttp(`/users/${uid}/travel-profile`, { grade, effective_on: effective, reason });
      setItems((await portalHttp<{ items: Profile[] }>('/travel/profiles')).items); setNotice('职级档案已记录，已提交申请保留原档案快照。');
    } catch (e) { setError(e instanceof Error ? e.message : '保存失败'); } finally { setBusy(false); }
  }
  if (import.meta.env.VITE_P4A_ENABLED === 'true') return <section className="card policy-governance"><h2>差旅职级档案</h2><p>住宿、交通及补助统一使用有效期内的可信职级。请在住宿职级标准页面维护来源、版本和有效期。</p>{error && <div className="alert alert-danger" role="alert">{error}</div>}<Link className="btn btn-primary" to="/hotel-matrix">维护职级与城市档案</Link><ul>{items.map(p => <li key={p.id}>{p.name} · {p.department} · {p.position} · {p.grade === 'UNKNOWN' ? '职级待核实' : p.grade + '级'}</li>)}</ul></section>;
  return <section className="card policy-governance"><h2>差旅职级档案</h2><p>部门与职位来自企业账号；职级请按人事记录维护，费用发生日决定适用档案。职位名称不会自动升级职级。</p>
    {error && <div className="alert alert-danger" role="alert">{error}</div>}{notice && <div className="alert alert-success" role="status">{notice}</div>}
    <div className="submission-fields"><div className="field"><label className="label" htmlFor="profile-user">员工</label><select className="select" id="profile-user" value={uid} onChange={e => setUid(e.target.value)}><option value="">请选择员工</option>{items.map(p => <option key={p.id} value={p.id}>{p.name} · {p.department} · {p.position} · 当前{p.grade === 'UNKNOWN' ? '未确认职级' : p.grade + '级'}</option>)}</select></div>
      <div className="field"><label className="label" htmlFor="profile-grade">差旅职级</label><select className="select" id="profile-grade" value={grade} onChange={e => setGrade(e.target.value)}><option value="UNKNOWN">未确认</option>{['A', 'B', 'C', 'D'].map(g => <option key={g}>{g}</option>)}</select></div>
      <div className="field"><label className="label" htmlFor="profile-effective">档案生效日期</label><input className="input" id="profile-effective" type="date" value={effective} onChange={e => setEffective(e.target.value)} /></div>
      <div className="field"><label className="label" htmlFor="profile-reason">人事依据</label><input className="input" id="profile-reason" maxLength={1000} value={reason} onChange={e => setReason(e.target.value)} /></div></div>
    <button className="btn btn-primary" disabled={busy || !uid || !effective || !reason.trim()} onClick={() => void save()}>{busy ? '保存中…' : '保存职级档案'}</button>
  </section>;
}
