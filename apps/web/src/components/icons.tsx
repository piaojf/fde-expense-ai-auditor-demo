/**
 * 导航与界面图标（内联 SVG，不引入额外依赖）。
 * 统一 16px 线性图标，颜色继承 currentColor，保证导航高亮态自动跟随。
 */
export type IconName =
  | 'dashboard'
  | 'list'
  | 'create'
  | 'policy'
  | 'file'
  | 'shield'
  | 'audit'
  | 'building'
  | 'accounts'
  | 'invoice'
  | 'check'
  | 'alert'
  | 'clock';

const STROKE = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.5,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
};

function Paths({ name }: { name: IconName }) {
  switch (name) {
    case 'dashboard':
      return (
        <>
          <rect x="2.5" y="2.5" width="4.5" height="4.5" rx="1" />
          <rect x="9" y="2.5" width="4.5" height="4.5" rx="1" />
          <rect x="2.5" y="9" width="4.5" height="4.5" rx="1" />
          <rect x="9" y="9" width="4.5" height="4.5" rx="1" />
        </>
      );
    case 'list':
      return (
        <>
          <path d="M5.5 3.5h8M5.5 8h8M5.5 12.5h5" />
          <path d="M2.5 3.5h.01M2.5 8h.01M2.5 12.5h.01" />
        </>
      );
    case 'create':
      return (
        <>
          <path d="M8 3v10M3 8h10" />
        </>
      );
    case 'policy':
      return (
        <>
          <rect x="3" y="2.5" width="10" height="11" rx="1.5" />
          <path d="M5.75 6h4.5M5.75 9h4.5" />
        </>
      );
    case 'file':
      return (
        <>
          <path d="M4 2.5h5l3 3v8H4z" />
          <path d="M9 2.5v3h3" />
        </>
      );
    case 'shield':
      return (
        <>
          <path d="M8 2l5 2v4.5c0 3-2.2 4.8-5 5.5-2.8-.7-5-2.5-5-5.5V4z" />
          <path d="M5.8 8l1.6 1.6L10.4 6.6" />
        </>
      );
    case 'audit':
      return (
        <>
          <circle cx="8" cy="8" r="5.5" />
          <path d="M8 5v3.2l2 1.2" />
        </>
      );
    case 'building':
      return (
        <>
          <rect x="2.5" y="3" width="11" height="10" rx="1.5" />
          <path d="M5.5 6.5h2M5.5 9.5h2M9 6.5h1.5M9 9.5h1.5" />
        </>
      );
    case 'accounts':
      return (
        <>
          <circle cx="8" cy="5.5" r="2.5" />
          <path d="M3.5 13.5c0-2.5 2-4 4.5-4s4.5 1.5 4.5 4" />
        </>
      );
    case 'invoice':
      return (
        <>
          <path d="M3.5 2.5h9v11l-1.5-1.2L9.5 13.5 8 12.3 6.5 13.5 5 12.3 3.5 13.5z" />
          <path d="M5.75 6h4.5M5.75 8.75h3" />
        </>
      );
    case 'check':
      return <path d="M3.5 8.5l3 3 6-6.5" />;
    case 'alert':
      return (
        <>
          <path d="M8 2.8l6 10.4H2z" />
          <path d="M8 6.6v3M8 11.4h.01" />
        </>
      );
    case 'clock':
      return (
        <>
          <circle cx="8" cy="8" r="5.5" />
          <path d="M8 4.8V8l2.4 1.4" />
        </>
      );
  }
}

export function NavIcon({ name, size = 16, color }: { name: IconName; size?: number; color?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      aria-hidden="true"
      focusable="false"
      style={color ? { color } : undefined}
      {...STROKE}
    >
      <Paths name={name} />
    </svg>
  );
}
