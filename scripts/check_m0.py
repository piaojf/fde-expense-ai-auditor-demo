"""Structural baseline/modified check, independent of site packages."""

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    present = (root / "apps/api/main.py").is_file()
    if not present:
        print("M0_ENTRYPOINT=ABSENT; STRUCTURE=FAIL")
        return 1
    required = [
        "docs/architecture.md",
        "docs/api-contract.md",
        "docs/decision-policy.md",
        "docs/development-rules.md",
        "agent/state.py",
        "docker-compose.yml",
        "pyproject.toml",
        "requirements.lock",
        "README.md",
        "database/models/entities.py",
        "tests/test_api.py",
    ]
    complete = all((root / path).is_file() for path in required)
    print("M0_ENTRYPOINT=PRESENT; STRUCTURE=" + ("PASS" if complete else "FAIL"))
    return 0 if complete else 1


if __name__ == "__main__":
    sys.exit(main())
