/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** true（默认）= DEMO MOCK 数据；false = 请求真实后端 */
  readonly VITE_USE_MOCK_API?: string;
  /** 真实后端地址，默认 http://localhost:8000（Contract：路由无 /api 前缀） */
  readonly VITE_API_BASE?: string;
  readonly VITE_M6_DEMO?: string;
  readonly VITE_M6_BUSINESS_DATE?: string;
  readonly VITE_ENTERPRISE_MODE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
