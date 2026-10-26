# Invoice evidence API v1.2.0 (local controlled demo)

Mounted at `/api/v1.2`; root v1 and mounted v1.1 are unchanged. No identity, production RBAC, official tax validation or cloud OCR. Single active attachment per draft. Invoice content is non-authoritative input.

- `POST /drafts`: reserve idempotent draft with client UUID `draft_id` and UUID `token` in JSON. The token is a local capability, not an authenticated user.
- `POST /drafts/{draft_id}/read`: JSON `{token}`; returns draft, active attachment, OCR result and claim binding.
- `POST /drafts/{draft_id}/attachments`: multipart `token` + `file`. 10 MiB file limit, bounded request, decode actual PDF/JPEG/PNG, page/pixel limits. Duplicate same bytes in draft returns same record. Different bytes require deletion first.
- `POST /drafts/{draft_id}/attachments/{attachment_id}/delete`: JSON `{token}`. Idempotent tombstone, only unsubmitted drafts can mutate.
- `POST /drafts/{draft_id}/attachments/{attachment_id}/ocr`: JSON `{token}`. Hybrid embedded text/Tesseract; persisted idempotent results; 503 for provider unavailable, 422 for invalid contents, 409 for integrity loss. Mock provider exists only as injected test adapter.
- `POST /drafts/{draft_id}/attachments/{attachment_id}/preview`: JSON `{token}`; passive PNG of first page, no original PDF execution. Confirm all source pages outside preview when multi-page.
- `POST /drafts/{draft_id}/checks`: JSON `{token, fields, confirmed_fields}`; returns amount/date/seller discrepancy, missing required fields and duplicate invoice warnings. Recognized and corrected values both checked. Risk hints are not fraud findings.
- `POST /claims`: standard v1.1 submission fields and idempotency key plus `draft_id`, `token`, `attachment_id`, `confirmed=true`, `confirmed_fields` and `discrepancy_reason`. Creates claim + immutable evidence and consumes draft in one SQLite transaction; runs existing AuditService afterwards. Repeat identical key/payload returns same claim; differing evidence conflicts. Missing/unknown evidence can be explicitly confirmed but adds human verification alert; warning explanation required. Does not bypass hard blocks.
- `GET /claims/{claim_id}`: standard submission response plus `invoice_evidence` (OCR, original metadata, confirmed corrections, reason, risk hints, confirmation time). A read-only local demo claim view, matching existing claim visibility; not production authorization.
- `GET /claims/{claim_id}/invoice-preview`: passive first-page PNG by persisted claim association, no caller-supplied path.

Fields: invoice_number, invoice_code (optional), issue_date, seller_name, buyer_name, total_amount, tax_amount (optional). Recognition state is RECOGNIZED or UNKNOWN; value null for unknown/ambiguous. Available confidence and page/line provenance preserved. Human corrections stay separate. Monetary strings use Decimal and CNY; no inferred purpose or nights.

File lifecycle: STAGING -> READY -> DELETED/MISSING. Metadata carries generated ID, safe display name, actual MIME, byte size and SHA-256. fsync/atomic rename and startup reconciliation allow interrupted operations to recover; digest validated before recognition/preview/submission. Runtime directory ignored by Git; no raw content in normal application logs.
