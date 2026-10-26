/**
 * 运行上下文（UI 层辅助记忆，不是业务状态）
 *
 * 用途：Contract 要求 human-review / resume 请求体必须携带 run_id 与 expected_revision，
 * 但 GET /claims/{claim_id} 的响应里没有 revision 字段。因此前端需要记住“最近一次
 * 写操作返回的 revision”，才能在下一步请求中填对 expected_revision。
 *
 * 严格边界：
 *  - 这里只保存后端返回的 revision / run_id，前端不推导、不递增、不猜测；
 *  - 不保存任何 decision / risk_level / 审批结论；
 *  - 仅存活于当前浏览器标签页（sessionStorage），刷新后可恢复，换标签页则重新获取；
 *  - 真实模式下表单仍允许人工修改 expected_revision 以应对版本冲突。
 */
const KEY = 'fde.run-context.v1';

export interface RunContext {
  run_id: string;
  revision: number;
  /** 人工复核意见是否已提交（用于决定是否可以 resume） */
  review_submitted?: boolean;
}

type Store = Record<string, RunContext>;

function read(): Store {
  try {
    const raw = sessionStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Store) : {};
  } catch {
    return {};
  }
}

function write(store: Store): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(store));
  } catch {
    /* 隐私模式下可能不可写，忽略即可（退化为不记忆） */
  }
}

export function getRunContext(claim_id: string): RunContext | null {
  return read()[claim_id] ?? null;
}

/** 记录一次写操作返回的 run_id / revision（值全部来自后端响应） */
export function saveRunContext(
  claim_id: string,
  patch: { run_id?: string; revision?: number; review_submitted?: boolean }
): void {
  const store = read();
  const prev = store[claim_id];
  store[claim_id] = {
    run_id: patch.run_id ?? prev?.run_id ?? '',
    revision: patch.revision ?? prev?.revision ?? 0,
    review_submitted: patch.review_submitted ?? prev?.review_submitted ?? false,
  };
  write(store);
}

export function clearRunContext(claim_id: string): void {
  const store = read();
  delete store[claim_id];
  write(store);
}
