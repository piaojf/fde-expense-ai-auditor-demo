# Expense evaluation metrics

M5A uses this existing package for explicit numerator/denominator calculations.
`evals.dataset` validates Synthetic golden inputs; `evals.runner` executes the existing
LangGraph audit workflow in disposable storage; `evals.report` renders actual results.

Definitions and acceptance: `docs/m5a-evaluation-spec.md`.
Measured results: `docs/m5a-evaluation-report.md` and `docs/m5a-evaluation-results.json`.

Default offline command (from repository root):

```bash
.venv/bin/python -m evals.runner --output output/m5a-local/run.json
```

Use fresh output paths. Real DeepSeek needs both `--live-deepseek` and `--max-calls N`;
no paid retries, no automatic mock fallback. Subsets cannot certify unexercised governance.
The framework does not certify production automatic approval accuracy. No Shadow Mode
is implemented by M5A; the existing shadow_mode placeholder is unchanged.
