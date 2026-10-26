"""Run the synthetic browser flow using only owned loopback service processes."""

import argparse
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import httpx

from scripts.public_tests import safe_environment

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output/ci/public-e2e"


def ensure_free(port):
    if not 1024 <= port <= 65535:
        raise ValueError("Ports must be between 1024 and 65535")
    with socket.socket() as connection:
        try:
            connection.bind(("127.0.0.1", port))
        except OSError as error:
            raise RuntimeError(f"Port {port} is occupied; no process will be replaced") from error


def stop_owned(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def wait_ready(process, url):
    deadline = time.monotonic() + 45
    with httpx.Client(timeout=1) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Owned service exited; inspect output/ci/public-e2e")
            try:
                if client.get(url).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
    raise RuntimeError("Owned service did not become ready: " + url)


def arguments(values=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-loopback-cors", action="store_true")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--web-port", type=int, default=5198)
    parser.add_argument("--chrome", type=Path, required=True)
    options = parser.parse_args(values)
    if not options.allow_loopback_cors:
        parser.error("Explicit --allow-loopback-cors consent is required")
    if options.api_port == options.web_port:
        parser.error("Backend and frontend ports must be different")
    if not options.chrome.is_file():
        parser.error("--chrome must identify an already installed Chrome executable")
    return options


def main():
    options = arguments()
    for port in (options.api_port, options.web_port):
        ensure_free(port)
    api = f"http://127.0.0.1:{options.api_port}"
    web = f"http://127.0.0.1:{options.web_port}"
    environment = safe_environment()
    environment.update(
        VITE_API_BASE=api, VITE_USE_MOCK_API="false", VITE_ENTERPRISE_MODE="false",
        VITE_P4A_ENABLED="false", VITE_P4B_ENABLED="false", API_BASE=api, WEB_BASE=web,
        CHROME_PATH=str(options.chrome.resolve()), E2E_OUTPUT=str(OUTPUT),
    )
    subprocess.run(["npm", "run", "build", "--prefix", "apps/web"], cwd=ROOT,
                   env=environment, check=True, timeout=180)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    commands = [
        ("backend", [sys.executable, "-m", "scripts.public_mock", "--allow-loopback-cors",
                     "--api-port", str(options.api_port), "--web-port", str(options.web_port)],
         api + "/health"),
        ("frontend", ["node", str(ROOT / "apps/web/node_modules/vite/bin/vite.js"), "preview",
                      str(ROOT / "apps/web"),
                      "--host", "127.0.0.1", "--port", str(options.web_port), "--strictPort"], web),
    ]
    with ExitStack() as stack:
        for name, command, url in commands:
            log = stack.enter_context((OUTPUT / (name + ".log")).open("wb"))
            process = subprocess.Popen(command, cwd=ROOT, env=environment,
                                       stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT)
            stack.callback(stop_owned, process)
            wait_ready(process, url)
        subprocess.run(["node", "apps/web/scripts/e2e-real-api.mjs"], cwd=ROOT,
                       env=environment, check=True, timeout=180)


if __name__ == "__main__":
    main()
