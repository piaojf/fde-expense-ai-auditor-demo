import { useEffect, useState } from 'react';
import { API_BASE } from '../lib/api/client';
import { portalHttp } from '../lib/api/enterprise';

interface UploadedFile {
  upload_id: string; filename: string; version: string | null; status: string;
  effective_from: string | null; effective_to: string | null;
}
interface FileList { items: UploadedFile[]; as_of: string }
const STATUS: Record<string, string> = {
  DRAFT: '未发布 · 不参与审核', IMPORT_FAILED: '导入失败 · 未发布',
  EFFECTIVE: '已发布 · 生效中', SCHEDULED: '已发布 · 待生效',
  REPLACED: '历史版本 · 已替代', RETIRED: '历史版本 · 已退休', EXPIRED: '历史版本 · 已到期',
};

export default function UploadedPolicyFiles({ refreshKey = '' }: { refreshKey?: string }) {
  const [list, setList] = useState<FileList>({ items: [], as_of: '' });
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [downloading, setDownloading] = useState('');
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    setLoading(true); setError('');
    portalHttp<FileList>('/policies/files').then(data => { if (active) setList(data); })
      .catch(e => { if (active) setError(e instanceof Error ? e.message : '制度列表暂时不可用'); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [refreshKey, revision]);
  async function download(file: UploadedFile) {
    setDownloading(file.upload_id); setError('');
    try {
      const response = await fetch(`${API_BASE}/api/v1.5/policies/files/${file.upload_id}/original`, {
        credentials: 'include', signal: AbortSignal.timeout(30000),
      });
      if (!response.ok) {
        if (response.status === 401) window.dispatchEvent(new Event('enterprise-auth-expired'));
        const data = await response.json();
        throw new Error(data.error?.message ?? '制度原件暂时不可用');
      }
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement('a');
      anchor.href = url; anchor.download = file.filename; anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      setError(e instanceof Error ? e.message : '制度原件下载失败，请重试');
    } finally { setDownloading(''); }
  }
  return <section className="card" aria-labelledby="uploaded-policy-files-heading">
    <div className="card-title"><h2 id="uploaded-policy-files-heading">已上传企业制度</h2>
      <button className="btn btn-sm" disabled={loading} onClick={() => setRevision(n => n + 1)}>刷新文件列表</button></div>
    <p className="page-desc">仅显示本企业文件。未发布制度不参与审核；已发布制度仍按适用范围和费用发生日期使用。
      {list.as_of && ` 状态日期：${list.as_of}。`}</p>
    {error && <p className="alert alert-danger" role="alert">{error}</p>}
    {loading ? <p role="status">正在读取制度文件…</p> : list.items.length === 0
      ? <p className="page-desc">本企业尚无已上传的制度文件。</p>
      : <div className="table-wrap"><table className="table" aria-label="已上传企业制度">
        <thead><tr><th>文件名</th><th>版本</th><th>状态</th><th>生效日期</th><th>截止日期（不含）</th><th>操作</th></tr></thead>
        <tbody>{list.items.map(file => <tr key={file.upload_id}>
          <td><button className="link-btn" disabled={!!downloading} onClick={() => void download(file)}>{file.filename}</button></td>
          <td>{file.version || '待确认'}</td><td>{STATUS[file.status] ?? '状态待确认'}</td>
          <td>{file.effective_from || '待确认'}</td><td>{file.effective_to || '未设置'}</td>
          <td><button className="btn btn-sm" disabled={!!downloading} onClick={() => void download(file)}
            aria-label={`下载原件 ${file.filename}`}>{downloading === file.upload_id ? '下载中…' : '下载原件'}</button></td>
        </tr>)}</tbody>
      </table></div>}
  </section>;
}
