# Public CI/CD

## Verification contract

| Check | Runtime | Required result |
| --- | --- | --- |
| Python public tests | Ubuntu 24.04, Python 3.12, Tesseract English | Ruff, dependency consistency, `scripts.public_tests` |
| Frontend checks | Ubuntu 24.04, Node.js 24 | Locked install, typecheck, unit tests, high/critical dependency audit, build |
| Browser Mock flow | Ubuntu 24.04, Python 3.12, Node.js 24, installed Chrome | Basic synthetic audit → human decision → Mock ERP → audit timeline, no intercepted API responses |
| Required CI | All three jobs | Every dependency must succeed; failed, cancelled or skipped jobs fail the aggregate |

CI runs for pull requests, pushes to `main`, manual dispatch and the reusable release verification call. It does not use `pull_request_target`, repository secrets, private policy originals, paid models, a business database or a real ERP connection. Fork PRs receive no write token. Source checkout does not persist credentials; third-party Actions are pinned to official commit SHAs. Routine CI has only `contents: read` permission.

The Python runner reports public-scope exclusions as deselected and optional external capabilities as skipped. A successful job is not a claim that all enterprise tests ran. See [TESTING.md](../TESTING.md). Chinese OCR, disposable PostgreSQL/pgvector, real-model quality, real retrieval and production acceptance need separate evidence.

JUnit XML, browser logs/screenshots/result JSON and the frontend build are stored as Actions artifacts for seven days. Browser verification starts only its own isolated loopback services on the hosted runner, uses a temporary synthetic database and cleans up its processes. There is no proxy, DNS, VPN or host network configuration change.

## Pull requests and protection

Changes should arrive through a PR with a stated problem, scope, executed checks and public/private-data assessment. `main` is intended to require the `Required CI` check against an up-to-date branch, resolved review conversations, and a PR. Force pushes and branch deletion are disabled. Settings are repository controls, not files: maintainers should verify them in **Settings → Branches** after setup.

This is a single-maintainer project. Zero mandatory approving reviews avoids requiring the author to approve their own PR; CI success is not independent human review. Add an actual reviewer and raise the review requirement when collaborators join. Do not bypass failures using an administrator merge.

Weekly Dependabot PRs cover npm, the PEP 621 Python manifest and GitHub Actions dependencies. The custom `requirements.lock` / `requirements-dev.lock` files are not standard Dependabot lockfiles: maintainers must update both pinned environments alongside Python manifest changes, then rerun CI; a manifest-only PR does not prove the installed pins changed. Security alerts, available automated dependency fixes, secret scanning and push protection should be enabled in repository security settings. These controls reduce risk but are not a guarantee that no secret or vulnerability exists.

## Manual release, not production deployment

The `Release` workflow is manually dispatched from `main`. It does not create a version tag, publish on every merge, deploy a website, call a model or modify an ERP. Video and page screenshots are a separate maintainer task.

1. Merge the intended PR and wait for `main` CI to pass. Choose an unused semantic version and review the public source scope.
2. From a clean, up-to-date public checkout, create and push an annotated `vX.Y.Z` tag at the current `main` commit. Replace `vX.Y.Z` with the version you actually choose; do not move an existing release tag.
3. In **Actions → Release → Run workflow**, select `main` and enter that existing tag.
4. The workflow rejects malformed tags, absent tags, non-main dispatches and tags not pointing to the dispatch commit and fetched `origin/main`. It then reruns the complete reusable CI profile for that exact SHA.
5. Only after successful verification, a separate job with `contents: write` creates the GitHub Release, attaches tracked-source and basic-frontend `.tar.gz` files plus `SHA256SUMS`, and uses [release notes](release-notes.md). It refuses to invent a missing tag. An already published release fails rather than silently overwriting assets.

The frontend archive is not a standalone application. Serve it with the Python backend on `http://127.0.0.1:8000`; it is built for basic mode. Both archives exclude runtime databases and untracked files because source packaging uses `git archive`. Installation still requires the documented runtimes and locked dependencies. No release is created merely by adding this workflow.

Downloaded release archives can be checked with `sha256sum -c SHA256SUMS` (Linux) or `shasum -a 256 -c SHA256SUMS` (macOS), from the directory containing all three downloaded assets.

## Local checks

```bash
.venv/bin/python -m scripts.public_tests -q
.venv/bin/python -m ruff check .
.venv/bin/python -m pip check
npm ci --prefix apps/web
npm run typecheck --prefix apps/web
npm test --prefix apps/web
npm audit --prefix apps/web --audit-level=high
npm run build --prefix apps/web
```

Optional local browser acceptance requires explicitly allowing the temporary loopback frontend/backend pairing and selecting an existing Chrome executable:

```bash
.venv/bin/python -m scripts.public_e2e --allow-loopback-cors --chrome /path/to/chrome
```

Keep any active recording services and their data separate. The runner refuses occupied ports; never stop another process just to satisfy a test.
