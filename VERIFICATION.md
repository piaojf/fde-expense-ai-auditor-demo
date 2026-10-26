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
