import { ApiError, type ClaimDetail, type OperationResponse } from './types';
import { API_BASE } from './client';

export const CITIES = ['深圳', '广州', '北京', '上海', '杭州', '南京', '苏州', '成都', '重庆', '武汉', '西安', '天津', '长沙', '厦门', '福州', '郑州', '济南', '青岛', '合肥', '宁波', '东莞', '佛山', '珠海', '昆明', '南宁', '贵阳', '海口', '沈阳', '大连', '长春', '哈尔滨', '石家庄', '太原', '南昌', '南通', '无锡', '温州', '兰州', '银川', '西宁', '乌鲁木齐', '拉萨', '呼和浩特'];
export interface SubmissionFields {
  applicant_name: string; department: string; position: string;
  category: 'HOTEL' | 'TRANSPORT' | 'MEAL' | 'OTHER'; amount: string;
  incurred_on: string; purpose: string; city: string; trip_start: string; trip_end: string;
  nights: number | null; seller: string; notes: string;
}
export interface SubmissionResponse {
  schema_version: string; claim: ClaimDetail; submission: SubmissionFields; trip_days: number;
  operation: OperationResponse | null;
  human_review_submitted: boolean;
  llm_analysis: { summary: string; provider: string; model: string; anomalies: string[] } | null;
  erp_effects: { status: string; source: string; idempotency_key: string }[];
}
export async function submissionHttp(path: string, body?: SubmissionFields & { idempotency_key: string }): Promise<SubmissionResponse> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/v1.1/claims${path}`, {
      method: body ? 'POST' : 'GET', headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(20000),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new ApiError(response.status, data.error?.code ?? 'HTTP_ERROR', data.error?.message ?? '请求失败', data.request_id);
    }
    return data;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError(0, 'NETWORK_ERROR', '连接失败或请求超时；提交结果可能已保存，请使用同一提交键重试。');
  }
}
