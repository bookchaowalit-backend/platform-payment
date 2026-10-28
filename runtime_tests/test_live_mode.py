"""This runtime is test-mode only: any live Stripe key refuses startup and readiness.

Live activation, and any move of webhook ownership away from
bookchaowalit/platform-webhooks-worker, needs a separate owner decision (D8).
Fixture keys are assembled at runtime and are never sent anywhere.
"""

import os
import subprocess
import sys

import pytest

from platform_payment import create_app
from runtime_tests.stripe_keys import fixture_key

from .test_api import FakeProvider

LIVE_KEYS = [fixture_key("sk", "live"), fixture_key("rk", "live")]
STRIPE_NAMES = ["PAYMENT_STRIPE_API_KEY", "STRIPE_SECRET_KEY", "STRIPE_API_KEY"]


def clean_environment(monkeypatch):
    for name in list(os.environ):
        if "STRIPE" in name or name.startswith("PAYMENT_"):
            monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("name", STRIPE_NAMES)
@pytest.mark.parametrize("key", LIVE_KEYS, ids=["secret", "restricted"])
def test_startup_refuses_a_live_key_even_with_an_injected_provider(
    monkeypatch, name, key
):
    clean_environment(monkeypatch)
    monkeypatch.setenv(name, key)
    with pytest.raises(ValueError, match="live Stripe mode refused") as refused:
        create_app(object(), object(), provider=FakeProvider())
    assert name in str(refused.value)
    assert key not in str(refused.value)


@pytest.mark.parametrize("key", LIVE_KEYS, ids=["secret", "restricted"])
def test_startup_refuses_a_live_key_when_provider_config_is_incomplete(
    monkeypatch, key
):
    # Partial configuration normally starts with the provider disabled (503);
    # a live key must still stop the process instead of starting quietly.
    clean_environment(monkeypatch)
    monkeypatch.setenv("PAYMENT_STRIPE_API_KEY", key)
    with pytest.raises(ValueError, match="live Stripe mode refused"):
        create_app(object(), object())


def test_startup_refuses_a_provider_that_reports_live_mode(monkeypatch):
    clean_environment(monkeypatch)
    provider = FakeProvider()
    provider.livemode = True
    with pytest.raises(ValueError, match="live Stripe mode refused"):
        create_app(object(), object(), provider=provider)


def test_sandbox_stripe_provider_reports_test_mode():
    from platform_payment.adapters import StripeProvider

    adapter = StripeProvider(
        api_key=fixture_key(),
        webhook_secret="synthetic-webhook",
        success_url="https://example.invalid/success",
        cancel_url="https://example.invalid/cancel",
    )
    assert adapter.livemode is False


@pytest.mark.parametrize("command", ["serve", "replay-webhooks", "migrate"])
def test_cli_refuses_live_mode_before_touching_the_database(command):
    key = fixture_key("sk", "live")
    env = {
        name: value
        for name, value in os.environ.items()
        if "STRIPE" not in name and not name.startswith("PAYMENT_")
    }
    env.update(
        {
            "PAYMENT_STRIPE_API_KEY": key,
            "PAYMENT_STRIPE_WEBHOOK_SECRET": "synthetic-webhook",
            "PAYMENT_SUCCESS_URL": "https://example.invalid/success",
            "PAYMENT_CANCEL_URL": "https://example.invalid/cancel",
            # Unreachable on purpose: refusal must happen before any connection.
            "DATABASE_URL": "host=/nonexistent-socket-dir dbname=none",
            "DATABASE_MIGRATION_DSN": "host=/nonexistent-socket-dir dbname=none",
            "JWT_ISSUER": "fixture",
            "JWT_AUDIENCE": "fixture",
            "JWT_JWKS_URL": "https://example.invalid/jwks",
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "platform_payment", command],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 2, output
    assert "live Stripe mode refused" in output
    assert "PAYMENT_STRIPE_API_KEY" in output
    assert key not in output


@pytest.mark.parametrize("key", LIVE_KEYS, ids=["secret", "restricted"])
def test_readiness_fails_when_a_live_key_appears_after_startup(
    runtime, monkeypatch, key
):
    c, _db, _ws, _provider = runtime
    assert c.get("/readyz").status_code == 200
    monkeypatch.setenv("PAYMENT_STRIPE_API_KEY", key)
    response = c.get("/readyz")
    assert response.status_code == 503
    assert key not in response.text
    monkeypatch.delenv("PAYMENT_STRIPE_API_KEY")
    assert c.get("/readyz").status_code == 200


def test_readiness_fails_when_the_provider_switches_to_live_mode(runtime):
    c, _db, _ws, provider = runtime
    assert c.get("/readyz").status_code == 200
    provider.livemode = True
    assert c.get("/readyz").status_code == 503
