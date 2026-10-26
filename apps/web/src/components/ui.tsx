/**
 * 通用展示组件（仅负责视觉呈现，不产生任何业务判断）。
 * 业务状态、金额、制度依据等数据全部由调用方传入，组件不做推导。
 */

/** 信息分区卡片：统一标题、留白与脚注 */
export function Section({
  title,
  extra,
  note,
  children,
  className = '',
}: {
  title: string;
  extra?: React.ReactNode;
  note?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={`card ${className}`.trim()}>
      <div className="card-title">
        <span>{title}</span>
        {extra ? <span className="card-title-extra">{extra}</span> : null}
      </div>
      {children}
      {note ? <div className="section-note">{note}</div> : null}
    </div>
  );
}

/** 键值对行：与 .kv-list 配合，保证表单字段对齐 */
export function KvRow({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="kv">
      <span className="k">{k}</span>
      <span className="v">{v}</span>
    </div>
  );
}

/** 顶部业务摘要指标：数值缺省时显示“未获取”，禁止编造 */
export function Metric({
  label,
  value,
  note,
  muted = false,
  tone,
}: {
  label: string;
  value: React.ReactNode;
  note?: React.ReactNode;
  muted?: boolean;
  /** 数值强调色（如风险等级），统一使用设计变量，不新增色值 */
  tone?: string;
}) {
  return (
    <div className={`summary-metric${muted ? ' is-muted' : ''}`}>
      <div className="sm-label">{label}</div>
      <div className="sm-value" style={tone ? { color: tone } : undefined}>
        {value}
      </div>
      {note ? <div className="sm-note">{note}</div> : null}
    </div>
  );
}

/** 规则检查条目：状态来自真实数据，文案由调用方给出 */
export function CheckItem({
  state,
  label,
  detail,
}: {
  state: 'pass' | 'fail' | 'warn' | 'info';
  label: string;
  detail: React.ReactNode;
}) {
  const mark = state === 'pass' ? '✓' : state === 'fail' ? '!' : state === 'warn' ? '?' : '·';
  return (
    <div className={`check-item ${state}`}>
      <span className="check-state">{mark}</span>
      <div>
        <div className="check-label">{label}</div>
        <div className="check-detail">{detail}</div>
      </div>
    </div>
  );
}

/** 空状态：用于“接口返回为空”的真实空态，不做占位数据 */
export function EmptyState({ text, hint }: { text: string; hint?: React.ReactNode }) {
  return (
    <div className="empty">
      {text}
      {hint ? (
        <>
          <br />
          <span style={{ fontSize: 12 }}>{hint}</span>
        </>
      ) : null}
    </div>
  );
}
