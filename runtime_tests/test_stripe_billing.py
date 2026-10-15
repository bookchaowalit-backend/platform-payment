import hashlib
import hmac
import json
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from platform_backend_core import Database, MigrationRunner
from platform_backend_core.testing import temporary_database
from platform_payment import create_app, MIGRATIONS
from .billing_support import (
    billing,
    checkout,
    activate,
    subscription_event,
    paid,
    refund,
    signed,
    CATALOG,
    OWN,
    OTHER,
    BODY,
    FakeProvider,
    event,
)


def test_actual_stripe_billing_and_refund_sdk_requests_are_bound(monkeypatch):
    import httpx
    from urllib.parse import parse_qs
    from platform_payment.adapters import StripeProvider

    calls = []

    def request(client, method, url, **kwargs):
        calls.append((method, str(url), kwargs))
        if "/checkout/sessions/cs_paid" in str(url):
            value = {
                "id": "cs_paid",
                "object": "checkout.session",
                "mode": "payment",
                "payment_status": "paid",
                "currency": "usd",
                "payment_intent": "pi_paid",
            }
        elif "/checkout/" in str(url):
            value = {
                "id": "cs_billing",
                "object": "checkout.session",
                "url": "https://checkout.stripe.com/synthetic",
            }
        elif "/subscriptions/" in str(url):
            value = {
                "id": "sub_bound",
                "object": "subscription",
                "cancel_at_period_end": True,
            }
        else:
            value = {"id": "re_bound", "object": "refund", "status": "pending"}
        return httpx.Response(200, json=value)

    monkeypatch.setattr(httpx.Client, "request", request)
    adapter = StripeProvider(
        api_key="rk_test_synthetic_fixture",
        webhook_secret="synthetic-webhook",
        success_url="https://example.invalid/success",
        cancel_url="https://example.invalid/cancel",
    )
    assert (
        adapter.create_subscription_checkout(
            price_id="price_basic",
            quantity=2,
            reference=str(uuid4()),
            idempotency_key="subscription-stable",
        )["id"]
        == "cs_billing"
    )
    assert (
        adapter.cancel_subscription(
            subscription_id="sub_bound", idempotency_key="cancel-stable"
        )["id"]
        == "sub_bound"
    )
    assert (
        adapter.create_refund(
            payment_id="cs_paid",
            amount=1000,
            currency="usd",
            reference=str(uuid4()),
            idempotency_key="refund-stable",
        )["id"]
        == "re_bound"
    )
    for method, url, kwargs in calls:
        if method.lower() == "get":
            continue
        raw = kwargs["data"]
        form = parse_qs(raw.decode() if isinstance(raw, bytes) else raw)
        assert kwargs["headers"]["Idempotency-Key"].endswith("-stable")
        assert "payment_method_types" not in form
        if "/checkout/" in url:
            assert form["mode"] == ["subscription"] and form[
                "line_items[0][price]"
            ] == ["price_basic"]
            assert form["line_items[0][quantity]"] == ["2"]
        elif "/subscriptions/" in url:
            assert form["cancel_at_period_end"] == ["true"]
        else:
            assert form["payment_intent"] == ["pi_paid"] and form["amount"] == ["1000"]


def test_stripe_signed_subscription_invoice_refund_normalization():
    import time
    from platform_payment.adapters import StripeProvider

    adapter = StripeProvider(
        api_key="rk_test_synthetic_fixture",
        webhook_secret="synthetic-webhook",
        success_url="https://example.invalid/success",
        cancel_url="https://example.invalid/cancel",
    )
    fixtures = [
        (
            "customer.subscription.updated",
            {
                "id": "sub_bound",
                "customer": "cus_bound",
                "status": "active",
                "cancel_at_period_end": False,
                "items": {
                    "data": [
                        {
                            "quantity": 1,
                            "current_period_end": 1900000000,
                            "price": {
                                "id": "price_basic",
                                "unit_amount": 1500,
                                "currency": "usd",
                                "recurring": {"interval": "month", "interval_count": 1},
                            },
                        }
                    ]
                },
            },
            "subscription",
        ),
        (
            "invoice.paid",
            {
                "id": "in_bound",
                "customer": "cus_bound",
                "parent": {"subscription_details": {"subscription": "sub_bound"}},
                "amount_due": 1500,
                "amount_paid": 1500,
                "currency": "usd",
                "status": "paid",
                "period_start": 1800000000,
                "period_end": 1900000000,
            },
            "invoice",
        ),
        (
            "refund.updated",
            {
                "id": "re_bound",
                "status": "succeeded",
                "payment_intent": "pi_bound",
                "amount": 1000,
                "currency": "usd",
            },
            "refund",
        ),
    ]
    for kind, obj, expected in fixtures:
        payload = json.dumps(
            {
                "id": "evt_" + expected,
                "type": kind,
                "livemode": False,
                "created": int(time.time()),
                "data": {"object": obj},
            }
        ).encode()
        timestamp = str(int(time.time()))
        digest = hmac.new(
            b"synthetic-webhook", timestamp.encode() + b"." + payload, hashlib.sha256
        ).hexdigest()
        normalized = adapter.verify_event(payload, f"t={timestamp},v1={digest}")
        assert (
            normalized["kind"] == expected
            and normalized["provider_object_id"] == obj["id"]
        )
