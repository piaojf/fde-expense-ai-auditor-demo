export type TravelType = 'HOTEL' | 'TRANSPORT' | 'ALLOWANCE';
export interface TravelDetails {
  expense_type: TravelType; transport_mode: string; seat: string; transport_count: number;
  allowance_days: number | null; breakfasts: number; lunches: number; dinners: number;
}
export const travelDetails = (type: TravelType): TravelDetails => ({ expense_type: type,
  transport_mode: '', seat: '', transport_count: 1, allowance_days: null,
  breakfasts: 0, lunches: 0, dinners: 0 });
export interface TravelRule {
  expense_type: TravelType; cities: string[]; departments: string[]; positions: string[];
  grades: string[]; unit: 'NIGHT' | 'DAY' | 'TRIP'; limit: string | null;
  transport_mode: string; seats: string[]; breakfast_rate: string; lunch_rate: string;
  dinner_rate: string; source_start: number; source_end: number; review_required: boolean;
  quote?: string;
}
export interface TravelSnapshot {
  policy_id: string; revision: number; published_at: string;
  rules: TravelRule[]; metadata: { title: string; version: string; effective_from: string; effective_to: string | null };
}
export interface TravelPolicy {
  revision: number; parsed: { text: string; lines: { start: number; end: number; text: string; line: number }[] };
  tables?: { cells: string[]; table_start: number; table_end: number; header: string }[];
  versions: TravelSnapshot[]; retirements: { effective_on: string; reason: string }[];
}
