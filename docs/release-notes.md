# Public expense-auditor demo

This release packages a public code snapshot and a basic-mode frontend build. The supported reproduction path uses synthetic claims, a Mock model and a local Mock ERP. AI advice remains distinct from the human finance decision.

## Assets

- `source-vX.Y.Z.tar.gz`: tracked repository source; install the documented Python and npm dependencies.
- `frontend-vX.Y.Z.tar.gz`: basic frontend assets configured for a local backend at `http://127.0.0.1:8000`; not a standalone hosted application.
- `SHA256SUMS`: SHA-256 checksums for both archives; filenames use the chosen release version.

The release workflow reruns the public Python, frontend and synthetic browser profiles for the tagged commit. Inspect its Actions run for actual results, exclusions and skips.

## Boundaries

Private policy originals, historical model responses, credentials, runtime databases and provider budgets are not distributed. Full enterprise hotel/P4B reproduction still requires excluded originals and their immutable source bindings; synthetic fixtures do not substitute for them. Real model quality, semantic retrieval, real ERP and production deployment are not certified by this release. The repository currently grants no open-source license; dependency licenses remain their own.

See the packaged README, TESTING.md and docs/ci-cd.md for setup, limitations and verification instructions. Video and page screenshots are not included in this release template.
