# Shadow mode foundation

Offline snapshot-only replay through the shared analysis graph; no business write nodes,
formal claim repository/checkpoint or human actions. Separate append-only run/case store.

```bash
.venv/bin/python -m evals.shadow_mode.runner --output output/m5b-local/new-run.json
```

All current references are SYNTHETIC_RULE_LABEL. Metrics reuse M5A, and Synthetic + Mock
results do not establish production accuracy. See the distributed [metrics documentation](../metrics/README.md)
and [public verification boundaries](../../TESTING.md).
