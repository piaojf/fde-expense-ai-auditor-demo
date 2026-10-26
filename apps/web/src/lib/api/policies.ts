import { API_BASE } from "./client";
import { ApiError } from "./types";
import { ENTERPRISE_MODE, portalHttp } from './enterprise';
const POLICY_BASE = `${API_BASE}${ENTERPRISE_MODE ? '/api/v1.5' : '/api/v1.3'}/policies`;

export interface PolicyMetadata {
  title: string;
  legal_entity: string;
  region: string;
  expense_type: "ALL" | "HOTEL" | "MEAL" | "TRANSPORT" | "OTHER";
  version: string;
  effective_from: string;
  effective_to: string | null;
}
export interface PolicyLine {
  text: string;
  page: number | null;
  line: number;
  method: string;
  confidence: number | null;
  start: number;
  end: number;
}
export interface ClauseSource {
  upload_id: string;
  file_sha256: string;
  source_uri: string;
  embedding_model: string;
  embedding_dimension: number;
  version: string;
  start: number;
  end: number;
  lines: PolicyLine[];
}
export interface Clause {
  chunk_id: string;
  quote: string;
  source: ClauseSource;
  relevance_score?: number;
}
export interface PolicyUpload {
  upload_id: string;
  policy_version_id: string;
  status: "DRAFT";
  state: "PARSED" | "CONFIRMED" | "INDEXED" | "FAILED" | "DELETED";
  filename: string;
  content_type: string;
  size: number;
  sha256: string;
  source_uri: string;
  created_at: string;
  parsed: {
    text?: string;
    methods?: string[];
    pages?: number | null;
    lines?: PolicyLine[];
    extracted_metadata?: Record<
      string,
      { value: string; source: PolicyLine | null }
    >;
  };
  metadata: PolicyMetadata | null;
  error_code: string | null;
  clauses: Clause[];
}
export interface PolicyList {
  items: PolicyUpload[];
  embedding_mode: "disabled" | "deterministic" | "remote";
}
export interface PolicyPreview {
  results: Clause[];
  model: string;
  dimension: number;
  policy_version_id: string;
}
export async function policyHttp<T>(
  token: string,
  path: string,
  body = {},
): Promise<T> {
  if (ENTERPRISE_MODE) return portalHttp<T>(`/policies${path}`, body);
  const response = await fetch(`${POLICY_BASE}${path}`, {
    credentials: 'same-origin',
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token, ...body }),
  });
  const data = await response.json();
  if (!response.ok)
    throw new ApiError(
      response.status,
      data.error?.code ?? "POLICY_ERROR",
      data.error?.message ?? "制度操作失败",
    );
  return data as T;
}
export function uploadPolicy(
  token: string,
  file: File,
  progress: (n: number) => void,
): Promise<PolicyUpload> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${POLICY_BASE}/upload`);
    xhr.withCredentials = ENTERPRISE_MODE;
    xhr.timeout = 125000;
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) progress(Math.round((e.loaded / e.total) * 100));
    };
    xhr.onload = () => {
      let data;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        reject(new Error("文件超过上限或服务器拒绝上传"));
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else {
        if (ENTERPRISE_MODE && xhr.status === 401)
          window.dispatchEvent(new Event('enterprise-auth-expired'));
        reject(
          new ApiError(
            xhr.status,
            data.error?.code ?? "UPLOAD_ERROR",
            data.error?.message ?? "制度上传失败",
          ),
        );
      }
    };
    xhr.onerror = xhr.ontimeout = () =>
      reject(new Error("连接失败或超时；重传同一文件会复用原草稿"));
    const body = new FormData();
    if (!ENTERPRISE_MODE) body.append("token", token);
    body.append("file", file);
    xhr.send(body);
  });
}
export async function downloadPolicy(token: string, policy: PolicyUpload) {
  const response = await fetch(
    `${POLICY_BASE}/${policy.upload_id}/original`,
    {
      method: "POST",
      credentials: ENTERPRISE_MODE ? 'include' : 'same-origin',
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(ENTERPRISE_MODE ? {} : { token }),
    },
  );
  if (!response.ok) {
    if (ENTERPRISE_MODE && response.status === 401)
      window.dispatchEvent(new Event('enterprise-auth-expired'));
    const data = await response.json();
    throw new Error(data.error?.message ?? "原文件不可用");
  }
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = policy.filename;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
