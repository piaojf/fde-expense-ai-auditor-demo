import pytest
from fastapi.testclient import TestClient

from apps.api.config import Settings
from apps.api.main import create_app


@pytest.fixture
def client():
    with TestClient(create_app(Settings(app_env="test", _env_file=None))) as test_client:
        yield test_client


def pytest_addoption(parser):
    parser.addoption(
        "--include-private",
        action="store_true",
        default=False,
        help="Include retained tests requiring excluded private policy/replay assets",
    )


def pytest_collection_modifyitems(config, items):
    """Public scope is an explicit asset dependency manifest, never last-run outcomes."""
    import json
    import os
    from pathlib import Path

    manifest = json.loads((Path(__file__).parent / "public_scope.json").read_text(encoding="utf-8"))
    private = manifest["private_tests"]
    excluded, selected = [], []
    for item in items:
        private_module = item.nodeid.split("::", 1)[0] in manifest.get("private_modules", {})
        private_fixture = set(item.fixturenames) & set(manifest.get("private_fixtures", {}))
        if (item.nodeid in private or private_module or private_fixture) and not config.getoption(
            "--include-private"
        ):
            excluded.append(item)
            continue
        # Imported policy_pg fixture does not inherit its defining module's skip mark.
        if "policy_pg" in item.fixturenames and not os.getenv("TEST_DATABASE_URL"):
            item.add_marker(pytest.mark.skip(reason="Disposable PostgreSQL not configured"))
        if item.nodeid == "tests/test_invoices.py::test_real_chinese_scan_and_provenance":
            tessdata = Path(os.getenv("TEST_OCR_TESSDATA", "output/ui-p2/tessdata"))
            if not all((tessdata / f"{lang}.traineddata").is_file() for lang in ("chi_sim", "eng")):
                item.add_marker(
                    pytest.mark.skip(
                        reason="Optional Chinese OCR resources unavailable; set TEST_OCR_TESSDATA"
                    )
                )
        selected.append(item)
    if excluded:
        config.hook.pytest_deselected(items=excluded)
        items[:] = selected
    config._public_private_count = len(excluded)


def pytest_report_header(config):
    return "Public asset scope: explicit private test exclusions in tests/public_scope.json"
