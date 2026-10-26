# Public test scope

Run `python -m scripts.public_tests -q` from a fresh clone. The launcher discards inherited application secrets, paid-provider switches and tracing credentials while preserving host network and certificate settings.

`tests/public_scope.json` retains the earlier exact-node private dependency list and adds explicit module/fixture dependencies for the current source. Tests and assertions remain in the repository. Exclusions are reported as DESELECTED, never PASS. `--include-private` requires separately authorized private originals and is outside public verification.

The excluded hotel original is verified by immutable source SHA at multiple boundaries. The independently authored `public-synthetic-matrix.docx` has its own title, content and hash; public tests verify its provenance, matrix calculations and rejection by the private-source contract. This does not establish complete enterprise/P4B reproduction.

Default tests use temporary SQLite or in-process ASGI transports. Optional PostgreSQL integration tests require a newly created disposable `fde_m0_test_*` database and pgvector; supply its URL using `TEST_DATABASE_URL`. Never point tests at a demo or business database. Optional Chinese OCR requires local `chi_sim` and `eng` resources through `TEST_OCR_TESSDATA`. No model, font or browser binaries are distributed.

Frontend checks: `npm run typecheck --prefix apps/web`, `node --test apps/web/tests/*.test.mjs`, and `npm run build --prefix apps/web`. An optional legacy browser script is available at `apps/web/scripts/e2e-real-api.mjs`; it requires separately installed Chrome and Playwright and only verifies the basic Mock workflow.

Public code inspection, offline engineering verification, real-model quality, real-retrieval quality and production acceptance are separate outcomes. See VERIFICATION.md for actual counts, skips and limits.
