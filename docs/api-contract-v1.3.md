# UI-P3A local policy draft API — v1.3.0

Independent FastAPI application mounted at `/api/v1.3`; root v1.0.0, v1.1 and v1.2 contracts remain frozen. Disabled by default. Opt-in requires POLICY_DEMO_ENABLED and a >=32-character POLICY_DEMO_TOKEN. Every request must come from a loopback peer with a loopback Host and (if present) local allowlisted Origin; forwarded proxy headers are refused. Capability is sent in JSON `token` or multipart `token`, never URL/log. No production RBAC or public admin endpoint.

All routes use POST; timestamps UTC, business dates ISO, IDs UUID, unknown extracted values `UNKNOWN`. Maximum whole body 10 MiB + 64 KiB; file 10 MiB, PDF 3 pages, DOCX 200 ZIP entries/20 MiB expanded/100:1 compression/3 declared pages/2000 paragraphs; extracted text 100k characters, worker 100s/CPU 80s. Explicit PostgreSQL migration required, no startup create_all. <=50 non-deleted uploads per controlled instance.

| Route | Request | Response |
| --- | --- | --- |
| `/policies/list` | token | items (up to 50), demo warning, embedding mode |
| `/policies/upload` | multipart token, file (.pdf/.docx) | upload: id, policy_version_id, DRAFT, lifecycle, original name/type/size/hash, parser methods/page count, raw text/lines, extracted metadata, confirmed metadata, error; same SHA non-deleted replay same IDs |
| `/policies/{upload_id}/read` | token | upload including ordered clauses and sources |
| `/policies/{upload_id}/original` | token | attachment bytes, no-store/nosniff/CSP sandbox; never inline active PDF/Word |
| `/policies/{upload_id}/confirm` | token, confirmed=true, metadata | persist human values; confirmed values frozen; same values replay; different values 409 |
| `/policies/{upload_id}/ingest` | token, remote_authorized=false | atomically insert only DRAFT version/chunks in M4A tables, never rules; replay indexed import no embedding calls |
| `/policies/{upload_id}/preview` | token, query, legal_entity, region, expense_type, version, incurred_on, remote_authorized=false | only requested indexed DRAFT ID+confirmed scope, ordered vector results with original source line offsets, model/dimension |
| `/policies/{upload_id}/delete` | token | idempotent tombstone, clear file/text/chunks and linked DRAFT version only; ACTIVE deletion forbidden |

Lifecycle PARSED → CONFIRMED → INDEXED; ingest failure → FAILED (confirmed metadata retained for explicit retry); deletion → DELETED. Policy business status always DRAFT, parsed upload exists before policy_versions row. UUID-based source `upload://policies/{upload_id}` links file, policy version, chunks, source spans and model metadata. Import computes vectors before atomic insert; errors roll back versions/chunks and return explicit failure. Confirmation preserves original extracted fields separately.

DRAFT preview is a separate query and never mutates normal approval authority. Same title/different company/version never resolves by name. A conflicting replay cannot overwrite confirmed scope. Date upper bound exclusive. No activation, model rule extraction, financial limit update or ERP action API.

Default draft embedding mode `disabled`: parsed/confirmed drafts stay local; ingest returns 503 until explicit provider selection. `deterministic` allowed only APP_ENV=test and labelled test-only, uses real PostgreSQL vector distance but is not BGE-M3. `remote` additionally requires POLICY_REMOTE_ENABLED, a finite persisted HTTP budget and per-request remote_authorized=true. Existing RemoteEmbeddingProvider validates model/dimension; no fallback. Model construction/download is never automatic. Internal documents require separate user authorization before remote enablement. Remote preview also sends query text to provider.

Errors follow existing `{error:{code,message,details},request_id}` envelope; 403 access/consent, 409 lifecycle/scope/model conflict, 413 body limit, 422 actual content/complexity/metadata invalid, 503 disabled/provider/parser/database unavailable. Filenames and raw parser/database errors are not logged or exposed.
