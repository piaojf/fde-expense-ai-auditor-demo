import { useState } from 'react';

/** Display abbreviation only. Links and requests always retain the original ID. */
export function shortRecordId(value: string): string {
  return value.length > 24 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value;
}

export default function RecordId({ value, full = false, copy = false }: {
  value: string; full?: boolean; copy?: boolean;
}) {
  const [feedback, setFeedback] = useState('');
  const [failed, setFailed] = useState(false);
  async function copyId() {
    try {
      await navigator.clipboard.writeText(value);
      setFailed(false); setFeedback('单号已复制');
    } catch {
      setFailed(true); setFeedback('复制未成功，请选中完整单号复制。');
    }
  }
  return <span className="record-id">
    <span title={value} aria-label={`单号 ${value}`}>单号 {full || failed ? value : shortRecordId(value)}</span>
    {copy && <button type="button" className="link-btn" onClick={() => void copyId()}>复制单号</button>}
    {feedback && <span role="status">{feedback}</span>}
  </span>;
}
