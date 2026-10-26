# Shadow mode foundation

Offline snapshot-only replay through the shared analysis graph; no business write nodes,
formal claim repository/checkpoint or human actions. Separate append-only run/case store.

```bash
.venv/bin/python -m evals.shadow_mode.runner --output output/m5b-local/new-run.json
```

All current references are SYNTHETIC_RULE_LABEL. Metrics reuse M5A, and Synthetic + Mock
results do not establish production accuracy. See [M5B-1 design and acceptance](../../docs/m5b-shadow-mode.md).
