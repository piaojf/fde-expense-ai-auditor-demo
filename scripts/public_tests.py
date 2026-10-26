"""Run the public suite without inherited app secrets or live-provider gates."""

import os
import subprocess
import sys
from pathlib import Path


def safe_environment():
    allowed = {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "TEST_DATABASE_URL",
        "TEST_OCR_TESSDATA",
    }
    env = {k: v for k, v in os.environ.items() if k in allowed}
    env.update(
        DATA_SOURCE="mock",
        LLM_PROVIDER="mock",
        POLICY_RETRIEVER="inmemory",
        RUN_LIVE_LLM_TESTS="0",
        RUN_LIVE_LLM_ACCEPTANCE="0",
        P4B_LIVE_LLM="false",
        LANGSMITH_TRACING="false",
        LANGCHAIN_TRACING_V2="false",
        POLICY_REMOTE_ENABLED="false",
    )
    return env


def main():
    env = safe_environment()
    return subprocess.call(
        [sys.executable, "-m", "pytest", *sys.argv[1:]],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
    )


if __name__ == "__main__":
    sys.exit(main())
