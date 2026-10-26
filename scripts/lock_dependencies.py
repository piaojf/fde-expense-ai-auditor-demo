"""Regenerate exact dependency closures from a resolved M0 virtual environment."""

import importlib.metadata as metadata
import tomllib
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def closure(requirements):
    pending = [
        (Requirement(req), extra_env) for req in requirements for extra_env in ("windows", "linux")
    ]
    seen = set()
    names = set()
    while pending:
        req, platform = pending.pop()
        key = (canonicalize_name(req.name), tuple(sorted(req.extras)), platform)
        if key in seen:
            continue
        seen.add(key)
        names.add(canonicalize_name(req.name))
        dist = metadata.distribution(req.name)
        env = default_environment()
        if platform == "linux":
            env.update(os_name="posix", sys_platform="linux", platform_system="Linux")
        else:
            env.update(os_name="nt", sys_platform="win32", platform_system="Windows")
        for raw in dist.requires or []:
            dependency = Requirement(raw)
            extras = req.extras or {""}
            if dependency.marker is None or any(
                dependency.marker.evaluate({**env, "extra": extra}) for extra in extras
            ):
                pending.append((dependency, platform))
    return names


def pinned(names):
    lines = []
    for name in sorted(names):
        suffix = ""
        if name in {"colorama", "tzdata"}:
            suffix = '; sys_platform == "win32"'
        lines.append(f"{name}=={metadata.version(name)}{suffix}")
    return "\n".join(lines) + "\n"


def main():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = config["project"]["dependencies"]
    dev = config["project"]["optional-dependencies"]["dev"]
    (ROOT / "requirements.lock").write_text(pinned(closure(runtime)), encoding="utf-8")
    (ROOT / "requirements-dev.lock").write_text(pinned(closure(runtime + dev)), encoding="utf-8")
    print("DEPENDENCY_LOCKS=GENERATED; PLATFORMS=Windows/Linux; PYTHON=3.12")


if __name__ == "__main__":
    main()
