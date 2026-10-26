# Verification

Date: 2026-10-26. Scope: public code and synthetic-asset snapshot; basic Mock workflow.

Verified source commit: `b82491241a5563cbcd0d2f481f1c650a5ffcc3e4`. The publication follow-up changes only this report and TESTING.md; the tested application and test files remain identical.

- Fresh independent clone, with a file-access guard preventing reads from private source worktrees: **914 passed, 110 skipped, 776 deselected; 0 failed, 0 errors**. Total collection: 1800. Skips are chiefly optional PostgreSQL/Chinese OCR and disabled paid-provider gates. Deselections are retained tests requiring private originals or historical evidence, not successful tests.
- Ruff and Python dependency consistency: passed.
- Frontend typecheck, production build and 14 frontend tests: passed on the preceding fresh clone; later code changes only restore backend policy-date and concurrency guards. Frontend source and lockfile are unchanged.
- After Pillow upgrade: 59 invoice/OCR/synthetic-policy checks passed, one optional Chinese OCR check skipped.
- npm official advisory bulk service: no affected packages in the lockfile. PyPI version advisory metadata: all 61 runtime pins checked, no affected packages, no query errors. These are point-in-time known-advisory checks, not a security guarantee.
- Independent Git root, no internal repository ancestry; reachable-object scan found no excluded original policy, private replay files, credentials, local home paths, databases or dependency environments. `git fsck --full --strict` passed. Final publication is scanned again after the report-only commit.

Dependencies used for local verification are external installed environments, excluded from Git; source, fixtures and expected results come from the fresh public clone. No real provider calls or real ERP writes were made. No public browser end-to-end acceptance was performed in this run.

Complete enterprise/P4B private-source reproduction, real model/retrieval quality and production acceptance are outside the public snapshot scope.

No project-wide open-source license is granted. Public visibility permits inspection of this code snapshot; dependency licenses remain separate.

## CI and documentation follow-up, 2026-10-26

The report above is the original publication baseline, not an assertion about later commits. The CI follow-up adds runner tests and browser/dependency entry points without modifying the business workflow.

- Local public profile on the isolated public branch: **918 passed, 110 skipped, 776 deselected**; total collection 1804. Dependencies were reused from an external installed environment. No claim of a new independent fresh-clone file-access guard is made for this follow-up.
- New runner safety tests: 4 passed. Frontend tests: 14 passed. Typecheck, production build, Ruff, `pip check`, Actionlint and relative documentation links passed.
- Local npm audit could not run because the configured mirror does not implement the advisory endpoint. No local registry/proxy/network configuration was changed. The GitHub-hosted frontend check executes `npm audit --audit-level=high` as a required command.
- GitHub-hosted verification is recorded on [public PR #1](https://github.com/piaojf/fde-expense-ai-auditor-demo/pull/1) and its linked Actions runs. Use the check results for the exact current commit, not the original snapshot counts. The real Chrome job exercises the synthetic basic Mock backend without API interception; it does not verify enterprise/P4B or real-provider behavior.
- The manual release workflow is statically checked; no version tag, GitHub Release or production deployment is created by this follow-up. Its actual publication path remains unexercised until a maintainer chooses a version and dispatches it.

The active recording services, business data, private policy originals and provider budget are outside this change.
