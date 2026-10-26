import { useEffect, useState, type FormEvent } from 'react';
import { portalHttp, type PortalClaim } from '../lib/api/enterprise';

type Stay = { status: string; city: string; address: string; check_in: string; check_out: string; nights: number };

export default function EmployeeStayVerification({ item, finance, refresh }: {
  item: PortalClaim; finance: boolean; refresh: () => Promise<void>;
}) {
  const [stay, setStay] = useState<Stay | null>(null);
  const [reference, setReference] = useState('');
  const [basis, setBasis] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [pending, setPending] = useState<{ body: unknown; auditKey: string } | null>(null);
  useEffect(() => {
    if (!item.p4a) return;
    let active = true;
    portalHttp<{ facts: { employee_stay?: Stay } }>(`/p4a/v1/claims/${item.claim_id}`)
      .then(r => { if (active) setStay(r.facts.employee_stay ?? null); })
      .catch(e => { if (active) setError(e instanceof Error ? e.message : String(e)); });
    return () => { active = false; };
  }, [item.claim_id, item.revision]);
  async function verify(event: FormEvent) {
    event.preventDefault(); if (busy) return;
    setBusy(true); setError('');
    const request = pending ?? { body: { expected_revision: item.revision, confirmed: true,
      source_reference: reference.trim(), basis: basis.trim(), idempotency_key: crypto.randomUUID() },
      auditKey: crypto.randomUUID() };
    setPending(request);
    try {
      await portalHttp(`/p4a/v2/claims/${item.claim_id}/verify-stay`, request.body);
      await portalHttp(`/p4b/v1/claims/${item.claim_id}/reaudit`, { expected_revision: item.revision, idempotency_key: request.auditKey });
      setPending(null); await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  if (!stay) return error ? <p role="alert">{error}</p> : null;
  return <section className="portal-reason" aria-label="员工住宿申报">
    <h4>员工住宿申报 · {stay.status === 'VERIFIED' ? '已由财务核实' : '待财务核实'}</h4>
    <p>{stay.city} · {stay.address}</p><p>{stay.check_in} 入住 → {stay.check_out} 退房 · {stay.nights} 晚</p>
    {finance && stay.status === 'PENDING' && item.stage === 'FINANCE_REVIEW' && <form onSubmit={verify}>
      <p>请核对实际住宿地点、入住及退房日期与订单或住宿证明。此次确认仅核实住宿事实，票据、业务及最终批准仍需分别审核。</p>
      <label className="label" htmlFor={`stay-reference-${item.claim_id}`}>订单／住宿证明来源</label>
      <input className="input" id={`stay-reference-${item.claim_id}`} required maxLength={2000} value={reference} onChange={e => setReference(e.target.value)} disabled={busy} />
      <label className="label" htmlFor={`stay-basis-${item.claim_id}`}>核实依据及说明</label>
      <textarea className="input" id={`stay-basis-${item.claim_id}`} required maxLength={2000} value={basis} onChange={e => setBasis(e.target.value)} disabled={busy} />
      <button className="btn" disabled={busy || !reference.trim() || !basis.trim()}>{busy ? '正在核实…' : '确认住宿事实并重新检查'}</button>
    </form>}{error && <p role="alert">{error}</p>}
  </section>;
}
