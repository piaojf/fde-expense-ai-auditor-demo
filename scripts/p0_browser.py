"""Cached Playwright CLI driver; records commands without installing dependencies.

Usage: python -m scripts.p0_browser SESSION COMMAND [ARG ...]
Use fresh snapshots before UI actions. Evidence stays in ignored output/playwright/p0.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/playwright/p0"


def cli(session, *args):
    cached = sorted((Path.home() / ".npm/_npx").glob(
        "*/node_modules/@playwright/cli/playwright-cli.js"))
    if not cached or not shutil.which("node"):
        raise RuntimeError("Existing local Playwright CLI and Node required; no auto-install")
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = subprocess.run([shutil.which("node"), str(cached[0]), "-s=" + session, *args],
                            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                            timeout=60)
    output = result.stdout + result.stderr
    log = OUT / (session + "-cli.jsonl")
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(dict(args=args, output=output), ensure_ascii=False) + "\n")
    log.chmod(0o600)
    print(output)
    if result.returncode or "### Error" in output:
        raise SystemExit(result.returncode or 1)


if __name__ == "__main__":
    cli(*sys.argv[1:])
