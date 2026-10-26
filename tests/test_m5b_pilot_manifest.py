"""Approval preflight cannot make HTTP calls or change original Golden labels."""

import json

import httpx
import pytest

from evals.dataset import DEFAULT_DATASET
from evals.shadow_mode.pilot_manifest import SMOKE, main, manifest


def test_fixed_sample_is_offline_complete_and_preserves_labels(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Preflight must not access HTTP")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    before = DEFAULT_DATASET.read_bytes()
    result = manifest()
    assert result == manifest()
    assert DEFAULT_DATASET.read_bytes() == before
    assert result["selected_case_count"] == 24
    assert len(result["category_counts"]) == 12
    assert set(result["category_counts"].values()) == {2}
    assert len(set(result["execution_order"])) == 24
    assert result["execution_order"][:4] == list(SMOKE)
    assert result["real_inference_planned_count"] == 22
    assert sum(c["real_inference_planned"] for c in result["cases"]) == 22
    assert sum(bool(c["artificial_fault_injection"]) for c in result["cases"]) == 2
    assert all(c["reference_source"] == "SYNTHETIC_RULE_LABEL" for c in result["cases"])


def test_unmarked_fault_is_rejected(tmp_path):
    data = json.loads(DEFAULT_DATASET.read_text(encoding="utf-8"))
    next(c for c in data["cases"] if c["case_id"] == "compliant-03")["llm_fault"] = (
        "HOSTILE_LOW"
    )
    path = tmp_path / "changed.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="Unmarked artificial"):
        manifest(path)


def test_manifest_tracks_dataset_version_and_does_not_overwrite(tmp_path, capsys):
    output = tmp_path / "manifest.json"
    assert main(["--output", str(output)]) == 0
    assert capsys.readouterr().out.strip() == "PAID_API_APPROVAL_REQUIRED"
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        main(["--output", str(output)])
    assert output.read_bytes() == before
    assert json.loads(before)["dataset_version"] == manifest()["dataset_version"]
