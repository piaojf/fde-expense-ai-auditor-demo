"""Private synthetic bootstrap is isolated, permissioned and repeatable."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

from scripts import enterprise_demo as demo


def test_enterprise_status_cli_uses_owned_process_manifest(tmp_path, monkeypatch, capsys):
    (tmp_path / "processes.json").write_text(
        json.dumps({"backend": {"pid": 123}, "frontend": {"pid": 456}}), encoding="utf-8"
    )
    monkeypatch.setattr(demo.m6, "active", lambda: tmp_path)
    monkeypatch.setattr(demo.m6, "alive", lambda item: item["pid"] == 123)
    monkeypatch.setattr(sys, "argv", ["enterprise_demo", "status"])
    demo.main()
    assert json.loads(capsys.readouterr().out) == {
        "generation": tmp_path.name,
        "owned_processes": {"backend": True, "frontend": False},
    }


def test_bootstrap_requires_consent_before_process_or_data_actions(monkeypatch):
    actions = []
    monkeypatch.setattr(demo.m6, "stop", lambda: actions.append("stop"))
    with pytest.raises(ValueError, match="consent"):
        demo.init("synthetic-new-key")
    assert actions == []


def test_bootstrap_personal_accounts_permissions_repeat_and_previous_data_retained(
    tmp_path, monkeypatch
):
    current = tmp_path / "current.json"
    old = tmp_path / "old-generation"
    old.mkdir()
    old_data = old / "untouched-evidence.bin"
    old_data.write_bytes(b"old-acceptance-must-be-retained")
    current.write_text(
        json.dumps({"reset_key": "old-key", "directory": str(old)}), encoding="utf-8"
    )
    new = tmp_path / "new-generation"
    stops = []
    monkeypatch.setattr(demo.m6, "CURRENT", current)
    monkeypatch.setattr(
        demo.m6,
        "active",
        lambda: Path(json.loads(current.read_text(encoding="utf-8"))["directory"]),
    )
    monkeypatch.setattr(demo.m6, "stop", lambda: stops.append("owned-stop"))

    def init(reset_key):
        new.mkdir(mode=0o700)
        current.write_text(
            json.dumps({"reset_key": reset_key, "directory": str(new)}), encoding="utf-8"
        )
        return new

    monkeypatch.setattr(demo.m6, "init", init)
    result = demo.init("enterprise-new-key", cookie_cors_consent=True)
    assert result == new and stops == ["owned-stop"]
    assert old_data.read_bytes() == b"old-acceptance-must-be-retained"
    credentials = json.loads((new / "enterprise-accounts.json").read_text(encoding="utf-8"))
    assert len(credentials["accounts"]) == 7
    passwords = {user["password"] for user in credentials["accounts"]}
    assert len(passwords) == 7
    assert {user["company_id"] for user in credentials["accounts"]} == {"DEMO-CN", "DEMO-B"}
    for filename in (
        "enterprise-accounts.json",
        "enterprise-accounts.txt",
        "enterprise-enabled.json",
    ):
        assert (new / filename).stat().st_mode & 0o777 == 0o600
    before = hashlib.sha256((new / "enterprise-accounts.json").read_bytes()).hexdigest()
    assert demo.init("enterprise-new-key", cookie_cors_consent=True) == new
    assert hashlib.sha256((new / "enterprise-accounts.json").read_bytes()).hexdigest() == before
    assert stops == ["owned-stop"]


def test_existing_nonenterprise_key_cannot_convert_acceptance_generation(tmp_path, monkeypatch):
    current = tmp_path / "current.json"
    current.write_text(json.dumps({"reset_key": "reserved-acceptance"}), encoding="utf-8")
    monkeypatch.setattr(demo.m6, "CURRENT", current)
    monkeypatch.setattr(demo.m6, "active", lambda: tmp_path)
    with pytest.raises(ValueError, match="reserved"):
        demo.init("reserved-acceptance", cookie_cors_consent=True)
    assert not (tmp_path / "enterprise-enabled.json").exists()
