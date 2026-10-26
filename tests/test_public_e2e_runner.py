import socket
import subprocess
from types import SimpleNamespace

import pytest

from scripts.public_e2e import arguments, ensure_free, stop_owned


def test_occupied_port_is_refused_without_replacing_listener():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(RuntimeError, match="occupied"):
            ensure_free(port)
        assert listener.getsockname()[1] == port


def test_browser_flow_requires_explicit_loopback_consent(tmp_path):
    chrome = tmp_path / "chrome"
    chrome.touch()
    with pytest.raises(SystemExit):
        arguments(["--chrome", str(chrome)])


def test_browser_flow_refuses_shared_service_port(tmp_path):
    chrome = tmp_path / "chrome"
    chrome.touch()
    with pytest.raises(SystemExit):
        arguments(["--allow-loopback-cors", "--chrome", str(chrome),
                   "--api-port", "8000", "--web-port", "8000"])


def test_cleanup_only_stops_the_owned_process():
    actions = []

    def wait(timeout):
        actions.append(("wait", timeout))
        if timeout == 10:
            raise subprocess.TimeoutExpired("owned", timeout)

    process = SimpleNamespace(poll=lambda: None, terminate=lambda: actions.append("terminate"),
                              kill=lambda: actions.append("kill"), wait=wait)
    stop_owned(process)
    assert actions == ["terminate", ("wait", 10), "kill", ("wait", 5)]
