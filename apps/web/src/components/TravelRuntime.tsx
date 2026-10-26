import { useEffect, useState } from 'react';
import { portalHttp } from '../lib/api/enterprise';
export default function TravelRuntime() {
  const p4b = import.meta.env.VITE_P4B_ENABLED === 'true';
  const [runtime, setRuntime] = useState<{ provider?: string; model?: string; semantic_model?: string; budget?: { remaining: number; maximum: number } | null } | null>(null);
  useEffect(() => { portalHttp<NonNullable<typeof runtime>>(p4b ? '/p4b/v1/runtime' : '/travel/runtime').then(setRuntime).catch(() => setRuntime(null)); }, [p4b]);
  if (p4b) return <p className="page-desc" role="status">{runtime?.semantic_model
    ? runtime.semantic_model === 'offline-no-semantic-model' ? '当前 P4B 为离线模式，未执行真实语义分析。'
      : `P4B 配置模型：${runtime.semantic_model}。实际提供方与分析是否成功，请以本单审核报告为准。`
    : 'P4B 模型配置暂未取得，请以本单审核报告为准。'}</p>;
  return <p className="page-desc" role="status">{runtime ? runtime.provider === 'mock' ? '当前 AI：模拟分析。制度额度与席别仍按已发布标准校验。' : `当前 AI：${runtime.provider} / ${runtime.model}${runtime.budget ? ` · 本次授权剩余 ${runtime.budget.remaining}/${runtime.budget.maximum} 次调用` : ''}。` : 'AI 连接状态暂未取得，请以审核结果中的提供方为准。'}</p>;
}
