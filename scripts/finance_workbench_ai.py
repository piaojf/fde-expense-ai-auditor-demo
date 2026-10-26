"""Explicit real DeepSeek opt-in for the existing employee/finance runtime."""

import argparse
import json
from pathlib import Path

from dotenv import dotenv_values

from apps.api.config import Settings
from integrations.llm.deepseek import DeepSeekLLMProvider

PROFILE = "deepseek-live.json"
LEDGER = "deepseek-live-budget.sqlite3"


def settings_from_profile(profile):
    if profile.get("provider") != "deepseek" or profile.get("egress_consent") is not True:
        raise ValueError("Explicit real DeepSeek consent required; no Mock fallback")
    maximum = profile.get("maximum")
    if type(maximum) is not int or not 1 <= maximum <= 30:
        raise ValueError("DeepSeek call cap must be 1..30")
    source = Path(profile["config_source"])
    if not source.is_absolute() or not source.is_file():
        raise ValueError("Existing private credential configuration required")
    values = dotenv_values(source)
    settings = Settings(
        _env_file=None,
        llm_provider="deepseek",
        deepseek_api_key=values.get("DEEPSEEK_API_KEY"),
        deepseek_base_url=profile["base_url"],
        deepseek_model=profile["model"],
        deepseek_timeout_seconds=60,
        deepseek_max_tokens=1024,
        p4b_max_tokens=4096,
        p4b_live_llm=True,
        p4b_advisory_protocol=profile.get("advisory_protocol", "p4b.advisory.3"),
        p4b_live_max_requests=maximum,
    )
    # Validate credentials and endpoint locally before replacing the running service.
    DeepSeekLLMProvider(
        api_key=settings.deepseek_api_key, model=settings.deepseek_model,
        base_url=settings.deepseek_base_url,
    )
    return settings


def load_live_settings(directory):
    path = directory / PROFILE
    if not path.exists():
        return None
    if not (directory / "fixture-summary.json").is_file():
        raise ValueError("Real AI requires an initialized runtime; no live fixture seeding")
    profile = json.loads(path.read_text(encoding="utf-8"))
    return settings_from_profile(profile).model_copy(
        update={"p4b_live_budget_path": str(directory / LEDGER)}
    )


def enable(directory, source, *, consent=False, maximum=20, existing_data=False):
    if consent is not True:
        raise ValueError("Explicit consent for DeepSeek data egress and paid calls required")
    source = Path(source).resolve(strict=True)
    values = dotenv_values(source)
    profile = dict(
        provider="deepseek", egress_consent=True, maximum=maximum,
        config_source=str(source),
        base_url=values.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com",
        model=values.get("DEEPSEEK_MODEL") or "deepseek-flash",
    )
    path = directory / PROFILE
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if existing_data:
        profile["advisory_protocol"] = "p4b.advisory.4"
    elif existing and "advisory_protocol" in existing:
        profile["advisory_protocol"] = existing["advisory_protocol"]
    settings_from_profile(profile)
    if not (directory / "fixture-summary.json").is_file():
        raise ValueError("Initialize offline fixtures before enabling real DeepSeek")
    if (existing and {k: v for k, v in existing.items() if k != "advisory_protocol"}
            != {k: v for k, v in profile.items() if k != "advisory_protocol"}):
        raise ValueError("Existing live configuration cannot be silently changed")
    # Reuse the durable ledger: enabling again or restarting cannot replenish the cap.
    from integrations.llm.budgeted import BudgetedLLM

    budget = BudgetedLLM(None, directory / LEDGER, maximum)
    usage = budget.usage()
    budget.db.close()
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as f:
        temporary.chmod(0o600)
        json.dump(profile, f, ensure_ascii=False, indent=2)
    temporary.replace(path)
    return dict(provider="deepseek", model=profile["model"], budget=usage,
                advisory_protocol=profile.get("advisory_protocol", "p4b.advisory.3"),
                restart_required=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--consent", action="store_true")
    parser.add_argument("--source-config", type=Path, required=True)
    parser.add_argument("--maximum", type=int, default=20)
    parser.add_argument("--existing-data", action="store_true")
    args = parser.parse_args()
    from scripts.finance_workbench_demo import active

    print(json.dumps(enable(active(), args.source_config, consent=args.consent,
                            maximum=args.maximum, existing_data=args.existing_data)))


if __name__ == "__main__":
    main()
