"""Review fixes: injected providers must declare test mode; unbound payment events stop retrying."""
import pytest

from platform_payment import create_app

from .test_api import event, runtime  # noqa: F401 - runtime is a fixture


class Undeclared:
    name = "fixture"


def test_injected_provider_must_declare_test_mode(monkeypatch):
    for name in ("PAYMENT_STRIPE_API_KEY", "STRIPE_SECRET_KEY", "STRIPE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="live Stripe mode refused"):
        create_app(object(), object(), provider=Undeclared())


def test_unbound_payment_event_is_retried_then_ignored(runtime):  # noqa: F811
    c, _db, _ws, _provider = runtime
    for _ in range(2):
        assert event(c, "pi_unbound_checkout").status_code == 409
    third = event(c, "pi_unbound_checkout")
    assert third.status_code == 200 and third.json() == {"accepted": True, "ignored": True}
    again = event(c, "pi_unbound_checkout")
    assert again.status_code == 200 and again.json()["ignored"] is True
