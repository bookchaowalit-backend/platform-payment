import hashlib
import hmac
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from platform_backend_core import Database, MigrationRunner, Principal
from platform_backend_core.testing import temporary_database
from platform_payment import create_app, MIGRATIONS
from .test_api import FakeProvider, OWN, OTHER, BODY, event

CATALOG = {
    "basic": {
        "price_id": "price_basic",
        "unit_amount": 1500,
        "currency": "usd",
        "interval": "month",
    }
}


class BillingProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.extended_calls = {"checkout": 0, "cancel": 0, "refund": 0}
        self.fail_next = None

    def operation(self, kind, key, result):
        with self.lock:
            if key not in self.objects:
                self.objects[key] = result
                self.extended_calls[kind] += 1
            if self.fail_next == kind:
                self.fail_next = None
                raise TimeoutError("ambiguous fixture response")
            return self.objects[key]

    def create_subscription_checkout(
        self, *, price_id, quantity, reference, idempotency_key
    ):
        assert price_id == "price_basic"
        return self.operation(
            "checkout",
            idempotency_key,
            {
                "id": "cs_sub_" + reference,
                "checkout_url": "http://127.0.0.1/subscription",
            },
        )

    def cancel_subscription(self, *, subscription_id, idempotency_key):
        return self.operation("cancel", idempotency_key, {"id": subscription_id})

    def create_refund(
        self, *, payment_id, amount, currency, reference, idempotency_key
    ):
        return self.operation(
            "refund",
            idempotency_key,
            {"id": "re_" + reference, "payment_id": payment_id},
        )


@pytest.fixture
def billing(monkeypatch):
    admin = os.environ.get("PLATFORM_TEST_DSN")
    if not admin:
        pytest.skip("PLATFORM_TEST_DSN: disposable fixture admin required")
    with temporary_database(admin, prefix="billing_test_") as isolated:
        MigrationRunner(isolated.admin_dsn, namespace="platform_payment").apply(
            MIGRATIONS
        )
        isolated.grant_schema()
        db = Database(isolated.runtime_dsn, max_size=8)
        ws = uuid4()
        other = uuid4()

        class Verifier:
            def verify(self, auth):
                from fastapi import HTTPException

                if not auth:
                    raise HTTPException(401, "authentication required")
                scopes = frozenset(
                    [
                        "payments:read",
                        "payments:write",
                        "payments:refund",
                        "billing:read",
                        "billing:write",
                    ]
                )
                return Principal(uuid4(), ws if auth == "Bearer own" else other, scopes)

        provider = BillingProvider()
        monkeypatch.setenv("PAYMENT_PLAN_CATALOG_JSON", json.dumps(CATALOG))
        with TestClient(create_app(db, Verifier(), provider=provider)) as client:
            yield client, db, ws, provider, isolated.runtime_dsn


def signed(c, *, kind, object_id, event_id=None, created=100, **fields):
    payload = json.dumps(
        {
            "kind": kind,
            "id": event_id or "evt_" + kind + "_" + object_id,
            "provider_object_id": object_id,
            "created": created,
            **fields,
        },
        sort_keys=True,
    ).encode()
    signature = hmac.new(
        b"synthetic-signing-secret", payload, hashlib.sha256
    ).hexdigest()
    return c.post(
        "/v1/webhooks/provider",
        content=payload,
        headers={"Stripe-Signature": signature},
    )


def checkout(c, key="billing-checkout"):
    return c.post(
        "/v1/billing/checkouts",
        json={"plan": "basic", "quantity": 1},
        headers={**OWN, "Idempotency-Key": key},
    )


def activate(c, item, **overrides):
    return signed(
        c,
        kind="subscription_checkout",
        object_id=item["provider_object_id"],
        status="complete",
        subscription_id="sub_" + item["id"],
        customer_id="cus_" + item["id"],
        amount=1500,
        currency="usd",
        **overrides,
    )


def subscription_event(
    c,
    item,
    *,
    status="active",
    created=200,
    event_id=None,
    cancel_at_period_end=False,
    **overrides,
):
    data = {
        "customer_id": "cus_" + item["id"],
        "price_id": "price_basic",
        "unit_amount": 1500,
        "quantity": 1,
        "currency": "usd",
        "interval": "month",
        "current_period_end": 1900000000,
        **overrides,
    }
    return signed(
        c,
        kind="subscription",
        object_id="sub_" + item["id"],
        status=status,
        created=created,
        event_id=event_id,
        cancel_at_period_end=cancel_at_period_end,
        **data,
    )


def paid(c):
    item = c.post("/v1/payment-intents", json=BODY, headers=OWN).json()
    assert event(c, item["provider_object_id"]).status_code == 200
    return item


def refund(c, item, amount=1000, key="refund-key"):
    return c.post(
        f"/v1/payment-intents/{item['id']}/refunds",
        json={"amount": amount},
        headers={**OWN, "Idempotency-Key": key},
    )
