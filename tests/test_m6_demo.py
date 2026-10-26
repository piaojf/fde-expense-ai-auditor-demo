"""Meaningful lifecycle boundaries: no accidental reset or unrelated PID action."""

import json
from types import SimpleNamespace

import pytest

from scripts import m6_demo as demo


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "OUT", tmp_path)
    monkeypatch.setattr(demo, "CURRENT", tmp_path / "current.json")
    directory = tmp_path / "generations" / "one"
    directory.mkdir(parents=True)
    demo.private_json(demo.CURRENT, dict(directory=str(directory), reset_key="same"))
    return directory


def test_same_reset_key_does_not_create_or_migrate_database(isolated, monkeypatch):
    def fail(*args):
        pytest.fail("Idempotent init must not contact/create database")

    monkeypatch.setattr(demo, "new_database", fail)
    assert demo.init("same") == isolated
    assert demo.init() == isolated


def test_external_generation_refused(isolated):
    demo.private_json(demo.CURRENT, dict(directory=str(isolated.parent.parent)))
    with pytest.raises(AssertionError, match="external runtime"):
        demo.active()


def test_symlink_generation_refused(isolated):
    link = isolated.parent / "alias"
    link.symlink_to(isolated, target_is_directory=True)
    demo.private_json(demo.CURRENT, dict(directory=str(link)))
    with pytest.raises(AssertionError, match="symlink"):
        demo.active()


def test_reset_refuses_live_generation(isolated, monkeypatch):
    (isolated / "processes.json").write_text(
        json.dumps({"backend": {"pid": 123}}), encoding="utf-8"
    )
    monkeypatch.setattr(demo, "alive", lambda item: True)
    with pytest.raises(AssertionError, match="Stop owned"):
        demo.init("different")


def test_recycled_pid_is_never_signalled(monkeypatch):
    monkeypatch.setattr(
        demo.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="different process")
    )
    with pytest.raises(AssertionError, match="PID identity changed"):
        demo.alive(dict(pid=123, identity="owned identity"))


def test_paid_environment_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setenv("RUN_LIVE_LLM_ACCEPTANCE", "1")
    env = demo.safe_environment()
    assert "DEEPSEEK_API_KEY" not in env and "RUN_LIVE_LLM_ACCEPTANCE" not in env
    assert env["LLM_PROVIDER"] == "mock"
    assert env["POLICY_REMOTE_ENABLED"] == "false"
    assert env["POLICY_REMOTE_MAX_REQUESTS"] == "0"


def test_exited_zombie_is_not_signalled(monkeypatch):
    monkeypatch.setattr(
        demo.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="Z owned process <defunct>")
    )
    assert not demo.alive(dict(pid=123, identity="owned identity"))
