export interface ComplianceEvidence {
  document_id: string; document_sha256: string; policy_version_id: string;
  section: string; clause_id: string; chunk_id: string; source_text: string;
  source_position: { part: string; body_index: number; table_index?: number; row_index?: number };
  effective_from: string; effective_to: string | null;
}
export interface ComplianceAnalysis {
  schema_version: 'p4b.1'; claim_id: string; run_id: string; policy_version_id: string | null;
  fact_revision: number; compliance_status: string; recommended_action: string;
  expense_type: string; policy_title: string | null;
  trusted_facts: { grade: string; city_tier: string };
  semantic_assessment: { suggested_risk: string; recommended_action: string } | null;
  semantic_coverage?: { protocol: string; model: Record<string, string>; server: { required: string[]; positive: Record<string, string>; rules: string[]; hard_gates: string[] } };
  citation_bindings?: (ComplianceEvidence & { finding_id: string })[];
  analysis_status: string; suggested_risk: string; provider: string; model: string;
  explanation: string; retrieval_method: string;
  rule_results: { rule_id: string; actual: string | null; limit: string | null; reason: string; outcome: string }[];
  semantic_findings: { finding_id: string; issue_type: string; conclusion: string;
    explanation: string; uncertainty: string; evidence_refs: string[]; factual_basis: string[] }[];
  supporting_evidence: ComplianceEvidence[]; missing_evidence: string[]; conflicts: string[]; exceptions: string[];
}
export interface ComplianceVerification {
  fact_revision: number;
  verification: { facts: Record<string, boolean | number | string | null>; source_reference: string;
    basis: string; actor_id: string; at: string } | null;
}

export interface AdvisoryReport {
  schema_version: 'p4b.advisory.report.1'; report_digest: string;
  deterministic_status: 'PASS' | 'FAIL' | 'UNKNOWN';
  ai_advisory_status: 'SUCCEEDED' | 'PARTIAL' | 'FAILED' | 'NOT_RUN';
  finance_decision_status: 'PENDING' | 'APPROVED' | 'REJECTED' | 'RETURNED' | 'ESCALATED';
  recommendation: string; risk_level: string; requires_human_review: true; summary: string;
  model: string; source_status: string;
  analysis_basis?: 'CURRENT_DATA_WITH_RAG'; manual_verification_required?: boolean;
  declared_amount_check?: { decision: string; claimed_amount: string; total_limit: string | null;
    limit_per_night: string | null; nights: number; reason: string; basis: string };
  issues: { topic: string; severity: string; explanation: string; finance_check: string;
    classification?: 'INFO' | 'WARNING' | 'BLOCKER'; resolution?: string;
    unverified_question?: string | null; source_quote?: string | null; rule_refs?: string[];
    policy_ref_ids: string[]; fact_refs: string[] }[];
  missing_evidence: string[]; positive_evidence: { missing: string[]; entries?: Record<string, {
    status: string; value: unknown; source_reference: string | null;
  }> };
  input_facts?: { candidate: Record<string, unknown> };
  rule_results?: ComplianceAnalysis['rule_results'];
  validation_errors: { code: string; field: string; scope?: string }[];
  original_output: unknown; raw_responses: string[]; supporting_evidence: ComplianceEvidence[];
  manual_review_current: boolean;
  manual_review: { outcome: string; exception_required: boolean; actor_id: string;
    basis: string; source_reference: string; at: string } | null;
}
