"""Loopback legacy Mock demo. Full P4B remains blocked by its private source pin."""

import argparse
import os
from pathlib import Path
from uuid import uuid4

from scripts.public_tests import safe_environment


def application(directory, web_port):
    from apps.api.config import Settings
    from apps.api.main import create_app
    from apps.api.repository import MockClaimRepository

    directory.mkdir(parents=True, exist_ok=False)
    settings = Settings(
        _env_file=None,
        app_env="development",
        llm_provider="mock",
        policy_retriever="inmemory",
        embedding_provider="deterministic",
        deepseek_api_key=None,
        embedding_api_key=None,
        enterprise_enabled=False,
        p4a_enabled=False,
        p4b_enabled=False,
        p4b_advisory_enabled=False,
        policy_remote_enabled=False,
        policy_demo_enabled=False,
        p4b_live_llm=False,
        cors_origins=[f"http://127.0.0.1:{web_port}"],
        mock_store_path=str(directory / "demo.sqlite3"),
    )
    return create_app(settings, MockClaimRepository(store_path=settings.mock_store_path))


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--web-port", type=int, default=5198)
    parser.add_argument(
        "--allow-loopback-cors",
        action="store_true",
        help="Explicitly consent to this process localhost frontend/API pairing",
    )
    args = parser.parse_args()
    if not args.allow_loopback_cors:
        parser.error("Explicit --allow-loopback-cors consent is required")
    if not all(1024 <= p <= 65535 for p in (args.api_port, args.web_port)):
        parser.error("Ports must be 1024..65535")
    # Preserve system proxy/certificate settings; discard application tracing credentials.
    for key in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
        os.environ[key] = "false"
    environment = safe_environment()
    os.environ.clear()
    os.environ.update(environment)
    directory = Path(".public-runtime") / uuid4().hex
    app = application(directory, args.web_port)
    print("Fresh synthetic Mock generation:", directory)
    uvicorn.run(app, host="127.0.0.1", port=args.api_port)


if __name__ == "__main__":
    main()
