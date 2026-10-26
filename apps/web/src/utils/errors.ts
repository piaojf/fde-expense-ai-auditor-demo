import { ApiError } from '../lib/api/types';

/**
 * 统一错误文案：把 HTTP 状态 + error.code 转成财务人员可读的中文提示。
 *
 * 原则（Contract §7）：客户端不能用 message 做程序分支，应使用 HTTP status + error.code。
 * 错误 Response 永远不能展示成“审核结果”。
 */
export function describeApiError(e: unknown, action = '操作'): string {
  if (e instanceof ApiError) {
    switch (e.status) {
      case 0:
        return `网络异常：无法连接后端服务，请检查 API 地址与服务是否已启动。（${action}未执行）`;
      case 404:
        return `未找到该报销单（404 ${e.code}）。请确认单号是否存在。`;
      case 409: {
        const hint =
          e.code === 'REVISION_CONFLICT'
            ? '当前版本号已变化，请刷新页面后重试'
            : e.code === 'INVALID_TRANSITION'
              ? '当前状态不允许执行此操作'
              : '当前状态不允许执行此操作';
        return `${hint}（409 ${e.code}）。前端不会自行改变业务状态。`;
      }
      case 422:
        return `请求参数未通过后端校验（422 ${e.code}）。请检查填写内容后重试。`;
      case 501:
        return `后端未开放该能力（501 ${e.code}）。这是「未开放状态」，不是审核结果。`;
      default:
        if (e.status >= 500) return `后端服务异常（${e.status} ${e.code}）。请稍后重试或联系管理员。`;
        return `${action}失败：${e.status} ${e.code} ${e.message}`;
    }
  }
  return `${action}失败：${e instanceof Error ? e.message : String(e)}`;
}
