# P4B Advisory v3: request-bound short evidence selection

Explicit opt-in `p4b_advisory_protocol=p4b.advisory.3` requires the existing enterprise/P4A/P4B/advisory configuration. Live calls remain disabled by default. Default protocol remains v1. v1/v2 wire DTOs, provider prompts, schemas and prior reports retain their meaning. The model wire contract changes, so short keys are a new version, never accepted as v2 full IDs.

## Input and request-local provenance

`p4b.input.3` supplies a unique `evidence_snapshot_id`, shared `evidence_scope`, policy document SHA/version/effective interval and original evidence records:

```json
{"short_id":"E01","clause_id":"实际条款名称","source_text":"完整222.docx原文或表格行","applicable_scope_ref":"evidence_scope"}
```

This is a shape example, not a policy excerpt. Every original candidate remains, in its original order. No clause or table text is shortened. Applicable scope includes expense category, effective grade, city/tier, department and expense date; full source positions and positive scope remain in the server manifest. Model-facing source provenance retains the document SHA and published version/effective interval. The catalog lists permissible short keys for each finding; selecting one does not prove the corresponding fact.

The server assigns `E01` through `E{N:02d}` for actual candidate count N (the historical hotel request has 39). The immutable canonical JSON manifest stores a fresh invocation nonce, full ComplianceRequest and one-to-one entries: short ID → original chunk ID → document ID/SHA → policy version → section/clause → XML position → complete source text → applicability scope → effective dates. SHA-256 of the whole manifest payload is the snapshot ID. Two identical inputs get different invocation IDs/fingerprints. Each attempt, including failed HTTP/timeouts, retains its own manifest and is linked to transport telemetry by snapshot ID. The active map never comes from model output.

## Model output

See `p4b-advisory-v3.schema.json`. Keep the v2 finite finding catalog, classification/resolution, risk/recommendation, missing evidence and fact/rule references. Replace `policy_ref_ids` with `policy_ref_keys` at report and finding levels, and require the exact echoed `evidence_snapshot_id`. The provider's request schema supplies exact key enums and the snapshot constant. Static DTO syntax checks do not replace request membership checks. Duplicate/unknown/other-request keys, labels or full IDs are rejected. No clause-name matching, fuzzy lookup, retroactive citation search, automatic insertion or retry repair exists.

## Two validation layers

First, verify the server manifest fingerprint and exact current request equality, invocation snapshot ID, published policy/version/SHA, expense date, complete source context, chunk text/position identity and applicable table rows. Resolve exact keys only into a separate v2 DTO. The selector does not rewrite the existing `original_output` or `raw_responses` records. `raw_responses` retains the inherited size/valid-JSON/credential filters and UTF-8 string storage; it does not promise unconditional retention of HTTP error bodies or every byte sequence. The private historical real-response file remains byte-for-byte unchanged.

Second, apply the existing v2 validator unchanged: exact per-code clause support, verified fact paths/provenance, rule IDs/outcomes, missing evidence, classifications, recommendation consistency, budget and finite P2 text guards. A real hotel limit clause cannot prove prior approval, budget availability or duplicate clearance. v3 additionally attributes every report-level reference to a validated finding whose fact references are included at report level, or the explicit effective-policy/deterministic-amount context. Any attribution error keeps the effective recommendation untrusted; filtering invalid findings never earns success.

## API and human authority

Existing authenticated API routes and `p4b.advisory.report.1` remain. Trusted display findings still use original full `policy_ref_ids`, so existing frontend evidence links continue working. `original_output` retains v3 keys. Additive report audit fields `evidence_selector` and `evidence_selector_attempts` contain the active/attempt manifests. No frontend implementation change is required; clients may ignore these extra audit fields.

All claims require independent authenticated finance review and explicit APPROVE/REJECT/RETURN. Model success grants no authority. Hard Gate, mandatory evidence, fresh finance role/company/session/revocation checks, first-write ERP revalidation and idempotency remain unchanged. Independent manual clearance may proceed despite acknowledged unreliable AI, but that does not convert model failure into success.

## Limits

This is an offline implementation over local original-DOCX retrieval and synthetic MockTransport/Mock ERP. Source publication and live source integrity remain owned by the existing RAG/P4A layer; mapping does not create policy authority. Finite attribution checks do not establish unrestricted natural-language truth. New v3 real DeepSeek/BGE acceptance is NOT_RUN. The historical v2 real FAIL stays FAIL; no old ledger is reused. Historical private replay tests depend on this machine's external, ignored evidence paths and the existing local OCR resources; the complete private regression suite is not a clean-clone portability guarantee. A future real call requires separate explicit authorization and a new independent budget ledger.
