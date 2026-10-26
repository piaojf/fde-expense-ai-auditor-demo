"""Explicit opt-in only; uses the existing configured endpoint and private .env key."""

import argparse
import json

from apps.api.config import Settings
from scripts.m6_demo import active, private_json


def enable(*, consent=False, maximum=20):
    if not consent:
        raise ValueError("Explicit consent for DeepSeek egress and paid calls required")
    if type(maximum) is not int or not 1 <= maximum <= 30:
        raise ValueError("Call cap must be 1..30")
    settings = Settings()
    if not settings.deepseek_api_key:
        raise ValueError("Existing DeepSeek key required; no key copied into profile")
    directory = active()
    path = directory / "enterprise-ai.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing["maximum"] != maximum:
            raise ValueError("Existing allowance cannot be changed")
    private_json(
        path,
        dict(
            provider="deepseek",
            egress_consent=True,
            maximum=maximum,
            model=settings.deepseek_model,
            base_url=settings.deepseek_base_url,
        ),
    )
    return dict(provider="deepseek", maximum=maximum, restart_required=True)


def disable(*, consent=False):
    if not consent:
        raise ValueError("Explicit consent to switch provider configuration required")
    path = active() / "enterprise-ai.json"
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        value["provider"] = "mock"
        private_json(path, value)
    return dict(provider="mock", restart_required=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["enable", "disable"])
    parser.add_argument("--consent", action="store_true")
    parser.add_argument("--maximum", type=int, default=20)
    args = parser.parse_args()
    print(
        enable(consent=args.consent, maximum=args.maximum)
        if args.action == "enable"
        else disable(consent=args.consent)
    )
