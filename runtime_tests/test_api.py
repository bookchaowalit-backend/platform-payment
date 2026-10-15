import os
from uuid import UUID, uuid4
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def runtime(monkeypatch):
    from platform_backend_core import Database, MigrationRunner, Principal
    from platform_payment import create_app, MIGRATIONS

    dsn = os.environ.get("PLATFORM_TEST_DSN")
    if not dsn:
        pytest.skip("PLATFORM_TEST_DSN required: disposable fixture admin PostgreSQL DSN")
    from platform_backend_core.testing import temporary_database
    with temporary_database(dsn, prefix="payment_test_") as isolated:
        MigrationRunner(isolated.admin_dsn, namespace="platform_payment").apply(MIGRATIONS)
        isolated.grant_schema()
        monkeypatch.setenv("PLATFORM_TEST_DSN", isolated.runtime_dsn)
        db = Database(isolated.runtime_dsn, max_size=8)
        workspace = uuid4()

        class Verifier:
            def verify(self, authorization):
                from fastapi import HTTPException

                if not authorization:
                    raise HTTPException(401, "authentication required")
                return Principal(
                    uuid4(),
                    workspace if authorization == "Bearer own" else other,
                    frozenset(["payments:read", "payments:write"]),
                )

        other = uuid4()
        adapter = FakeProvider()
        app = create_app(db, Verifier(), provider=adapter)
        with TestClient(app) as client:
            yield client, db, workspace, adapter


OWN = {"Authorization": "Bearer own", "Idempotency-Key": "fixture-key"}
OTHER = {"Authorization": "Bearer other", "Idempotency-Key": "fixture-key"}

import hashlib
import hmac
import json
import threading
import time


class FakeProvider:
    name = "fixture"

    def __init__(self):
        self.objects = {}
        self.calls = 0
        self.timeout = False
        self.lock = threading.Lock()

    def create(self, *, amount, currency, reference, idempotency_key, checkout):
        with self.lock:
            if idempotency_key not in self.objects:
                self.calls += 1
                self.objects[idempotency_key] = {
                    "id": "pi_" + reference,
                    "status": "pending",
                    "checkout_url": "http://127.0.0.1/checkout" if checkout else None,
                }
            if self.timeout:
                self.timeout = False
                raise TimeoutError("ambiguous provider response")
            return self.objects[idempotency_key]

    def verify_event(self, payload, signature):
        expected = hmac.new(
            b"synthetic-signing-secret", payload, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature or "", expected):
            raise ValueError("invalid signature")
        return json.loads(payload)


def event(
    c,
    provider_id,
    *,
    event_id=None,
    status="succeeded",
    amount=1500,
    currency="usd",
    workspace=None,
):
    body = json.dumps(
        {
            "id": (event_id or "evt_fixture") + "_" + provider_id,
            "provider_object_id": provider_id,
            "status": status,
            "amount": amount,
            "currency": currency,
            "workspace": str(workspace or UUID(int=0)),
        }
    ).encode()
    signature = hmac.new(b"synthetic-signing-secret", body, hashlib.sha256).hexdigest()
    return c.post(
        "/v1/webhooks/provider", content=body, headers={"Stripe-Signature": signature}
    )


BODY = {"amount": 1500, "currency": "usd"}


def test_payment_idempotency_and_tenant_isolation(runtime):
    c, db, ws, provider = runtime
    first = c.post("/v1/payment-intents", json=BODY, headers=OWN)
    assert first.status_code == 201, first.text
    item = first.json()
    assert (
        c.post("/v1/payment-intents", json=BODY, headers=OWN).json()["id"] == item["id"]
    )
    assert (
        c.post(
            "/v1/payment-intents", json={**BODY, "amount": 1600}, headers=OWN
        ).status_code
        == 409
    )
    assert provider.calls == 1
    assert c.get(f"/v1/payment-intents/{item['id']}", headers=OTHER).status_code == 404
    assert c.get("/v1/payment-intents", headers=OTHER).json() == []
    other = c.post("/v1/payment-intents", json=BODY, headers=OTHER)
    assert other.status_code == 201
    assert other.json()["id"] != item["id"]


def test_signature_binding_dedupe_and_out_of_order(runtime):
    c, db, ws, provider = runtime
    item = c.post("/v1/payment-intents", json=BODY, headers=OWN).json()
    provider_id = item["provider_object_id"]
    assert (
        c.post(
            "/v1/webhooks/provider",
            content=b"{}",
            headers={"Stripe-Signature": "forged"},
        ).status_code
        == 400
    )
    assert (
        event(c, provider_id, event_id="evt_wrong_amount", amount=1600).status_code
        == 409
    )
    assert (
        event(c, provider_id, event_id="evt_wrong_currency", currency="eur").status_code
        == 409
    )
    assert event(c, "pi_unknown").status_code == 409
    assert event(c, provider_id).status_code == 200
    assert event(c, provider_id).json()["duplicate"] is True
    assert (
        event(c, provider_id, event_id="evt_stale", status="pending").status_code == 200
    )
    assert (
        event(c, provider_id, event_id="evt_failure", status="failed").status_code
        == 200
    )
    assert (
        c.get(f"/v1/payment-intents/{item['id']}", headers=OWN).json()["status"]
        == "succeeded"
    )
    with db.transaction(ws) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM payment_effects WHERE intent_id=%s", (item["id"],)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM payment_webhook_inbox WHERE intent_id=%s",
                (item["id"],),
            ).fetchone()[0]
            == 3
        )


def test_concurrent_requests_and_webhooks(runtime):
    from concurrent.futures import ThreadPoolExecutor

    c, db, ws, provider = runtime
    with ThreadPoolExecutor(max_workers=6) as pool:
        values = list(
            pool.map(
                lambda _: c.post("/v1/payment-intents", json=BODY, headers=OWN),
                range(12),
            )
        )
    assert all(r.status_code in (200, 201, 202) for r in values), [
        (r.status_code, r.text) for r in values
    ]
    assert len({r.json()["id"] for r in values}) == 1
    assert provider.calls == 1
    item = c.post("/v1/payment-intents", json=BODY, headers=OWN).json()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(lambda _: event(c, item["provider_object_id"]), range(12))
        )
    assert all(r.status_code == 200 for r in results)
    assert sum(not r.json()["duplicate"] for r in results) == 1


def test_timeout_retains_identity_and_reconciles_provider_idempotently(runtime):
    c, db, ws, provider = runtime
    provider.timeout = True
    first = c.post("/v1/payment-intents", json=BODY, headers=OWN)
    assert first.status_code == 202
    item = first.json()
    assert item["status"] == "unknown"
    replay = c.post("/v1/payment-intents", json=BODY, headers=OWN)
    assert replay.status_code == 202
    result = c.post(f"/v1/payment-intents/{item['id']}/reconcile", headers=OWN)
    assert result.status_code == 200, result.text
    assert result.json()["id"] == item["id"]
    assert provider.calls == 1
    assert event(c, result.json()["provider_object_id"]).status_code == 200


def test_checkout_limits_rls_and_config(runtime):
    c, db, ws, provider = runtime
    result = c.post("/v1/checkout", json=BODY, headers=OWN)
    assert result.status_code == 201, result.text
    assert result.json()["checkout_url"] == "http://127.0.0.1/checkout"
    for body in [
        {"amount": True, "currency": "usd"},
        {"amount": 0, "currency": "usd"},
        {"amount": 1000000000, "currency": "usd"},
        {"amount": 1.5, "currency": "usd"},
        {"amount": 100, "currency": "USD"},
        {"amount": 100, "currency": "bad"},
        {**BODY, "workspace_id": str(uuid4())},
    ]:
        assert c.post("/v1/payment-intents", json=body, headers=OWN).status_code == 422
    with db.transaction(uuid4()) as conn:
        assert conn.execute("SELECT count(*) FROM payment_intents").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM payment_bindings").fetchone()[0] == 0
    with db.transaction(ws) as conn:
        for table in [
            "payment_intents",
            "payment_bindings",
            "payment_webhook_inbox",
            "payment_effects",
        ]:
            assert conn.execute(
                "SELECT relforcerowsecurity FROM pg_class WHERE oid=%s::regclass",
                (table,),
            ).fetchone()[0]
        assert not conn.execute(
            "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user"
        ).fetchone()[0]


def test_missing_provider_fails_closed():
    from platform_payment import create_app
    from platform_backend_core import Principal

    class V:
        def verify(self, auth):
            return Principal(uuid4(), uuid4(), frozenset(["payments:write"]))

    with TestClient(create_app(DisabledDatabase(), V())) as c:
        assert c.post("/v1/payment-intents", json=BODY, headers=OWN).status_code == 503
        assert c.post("/v1/webhooks/provider", content=b"{}").status_code == 503


class DisabledDatabase:
    def close(self):
        pass

    def ready(self):
        return True


def test_readiness_requires_provider_and_migrated_rls_schema():
    from platform_payment import create_app

    with TestClient(create_app(DisabledDatabase(), object())) as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/readyz").status_code == 503


def test_stripe_sdk_is_bounded_and_supports_sync_service_calls():
    from platform_payment.adapters import StripeProvider

    adapter = StripeProvider(
        api_key="rk_test_synthetic_fixture",
        webhook_secret="synthetic-webhook",
        success_url="https://example.invalid/success",
        cancel_url="https://example.invalid/cancel",
    )
    http_client = adapter.client._requestor._get_http_client()
    assert http_client._timeout == 5
    assert http_client._client is not None
    for key in ("rk_live_synthetic_fixture", "sk_live_synthetic_fixture", ""):
        with pytest.raises(ValueError):
            StripeProvider(
                api_key=key,
                webhook_secret="synthetic",
                success_url="https://example.invalid/s",
                cancel_url="https://example.invalid/c",
            )


def test_actual_stripe_sdk_checkout_and_intent_payloads_without_network(monkeypatch):
    import httpx
    from urllib.parse import parse_qs
    from platform_payment.adapters import StripeProvider

    calls = []

    def request(client, method, url, **kwargs):
        calls.append((str(url), kwargs))
        value = {
            "id": "cs_synthetic" if "checkout" in str(url) else "pi_synthetic",
            "object": (
                "checkout.session" if "checkout" in str(url) else "payment_intent"
            ),
            "url": "https://checkout.stripe.com/synthetic",
        }
        return httpx.Response(200, json=value)

    monkeypatch.setattr(httpx.Client, "request", request)
    adapter = StripeProvider(
        api_key="rk_test_synthetic_fixture",
        webhook_secret="synthetic-webhook",
        success_url="https://example.invalid/success",
        cancel_url="https://example.invalid/cancel",
    )
    reference = str(uuid4())
    assert (
        adapter.create(
            amount=1500,
            currency="usd",
            reference=reference,
            idempotency_key="stable-provider-key",
            checkout=False,
        )["id"]
        == "pi_synthetic"
    )
    assert (
        adapter.create(
            amount=1500,
            currency="usd",
            reference=reference,
            idempotency_key="stable-checkout-key",
            checkout=True,
        )["id"]
        == "cs_synthetic"
    )
    for url, kwargs in calls:
        form = parse_qs(
            kwargs["data"].decode()
            if isinstance(kwargs["data"], bytes)
            else kwargs["data"]
        )
        assert "payment_method_types" not in form
        assert kwargs["headers"]["Idempotency-Key"].startswith("stable-")
        assert kwargs["headers"]["Stripe-Version"] == "2026-08-26.dahlia"
        if "checkout" in url:
            assert form["line_items[0][price_data][unit_amount]"] == ["1500"]
            assert form["integration_identifier"] == ["platform_checkout_kqzmpvtn"]
            assert form["success_url"] == ["https://example.invalid/success"]
        else:
            assert form["amount"] == ["1500"]


def test_stripe_signature_raw_body_livemode_and_checkout_payment_status():
    from platform_payment.adapters import StripeProvider

    adapter = StripeProvider(
        api_key="rk_test_synthetic_fixture",
        webhook_secret="synthetic-webhook",
        success_url="https://example.invalid/success",
        cancel_url="https://example.invalid/cancel",
    )

    def signed(value, timestamp=None):
        payload = json.dumps(value).encode()
        stamp = int(time.time()) if timestamp is None else timestamp
        signature = hmac.new(
            b"synthetic-webhook", str(stamp).encode() + b"." + payload, hashlib.sha256
        ).hexdigest()
        return payload, f"t={stamp},v1={signature}"

    body = {
        "id": "evt_synthetic",
        "object": "event",
        "livemode": False,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_synthetic",
                "object": "checkout.session",
                "payment_status": "unpaid",
                "amount_total": 1500,
                "currency": "usd",
            }
        },
    }
    payload, signature = signed(body)
    assert adapter.verify_event(payload, signature)["status"] == "pending"
    body["data"]["object"]["payment_status"] = "paid"
    payload, signature = signed(body)
    assert adapter.verify_event(payload, signature)["status"] == "succeeded"
    with pytest.raises(Exception):
        adapter.verify_event(payload + b" ", signature)
    payload, signature = signed(body, timestamp=int(time.time()) - 600)
    with pytest.raises(Exception):
        adapter.verify_event(payload, signature)
    body["livemode"] = True
    payload, signature = signed(body)
    with pytest.raises(ValueError):
        adapter.verify_event(payload, signature)


def test_failed_then_success_and_immutable_effects_and_stale_reconcile(runtime):
    import psycopg

    c, db, ws, provider = runtime
    item = c.post("/v1/payment-intents", json=BODY, headers=OWN).json()
    provider_id = item["provider_object_id"]
    assert (
        event(c, provider_id, event_id="evt_failed", status="failed").status_code == 200
    )
    assert (
        event(c, provider_id, event_id="evt_old_pending", status="pending").status_code
        == 200
    )
    assert (
        c.get(f"/v1/payment-intents/{item['id']}", headers=OWN).json()["status"]
        == "failed"
    )
    assert (
        event(c, provider_id, event_id="evt_success", status="succeeded").status_code
        == 200
    )
    with pytest.raises(psycopg.Error):
        with db.transaction(ws) as conn:
            conn.execute(
                "DELETE FROM payment_effects WHERE intent_id=%s", (item["id"],)
            )
    provider.timeout = True
    unknown = c.post(
        "/v1/payment-intents",
        json=BODY,
        headers={**OWN, "Idempotency-Key": "old-unknown"},
    ).json()
    with db.transaction(ws) as conn:
        conn.execute(
            "UPDATE payment_intents SET created_at=now()-interval '21 hours' WHERE id=%s",
            (unknown["id"],),
        )
    assert (
        c.post(
            f"/v1/payment-intents/{unknown['id']}/reconcile", headers=OWN
        ).status_code
        == 409
    )
    assert c.get("/readyz").status_code == 200


def test_two_independent_api_pools_share_creation_binding_and_inbox(runtime):
    from concurrent.futures import ThreadPoolExecutor
    from platform_backend_core import Database
    from platform_payment import create_app

    c, db, ws, provider = runtime
    second = Database(os.environ["PLATFORM_TEST_DSN"], max_size=4)
    with TestClient(
        create_app(second, c.app.state.verifier, provider=provider)
    ) as replica:
        with ThreadPoolExecutor(max_workers=6) as pool:
            values = list(
                pool.map(
                    lambda n: (c if n % 2 else replica).post(
                        "/v1/payment-intents", json=BODY, headers=OWN
                    ),
                    range(12),
                )
            )
        assert all(r.status_code in (200, 201, 202) for r in values)
        assert len({r.json()["id"] for r in values}) == 1
        item = replica.get(
            f"/v1/payment-intents/{values[0].json()['id']}", headers=OWN
        ).json()
        assert provider.calls == 1
        with ThreadPoolExecutor(max_workers=6) as pool:
            events = list(
                pool.map(
                    lambda n: event(
                        c if n % 2 else replica, item["provider_object_id"]
                    ),
                    range(12),
                )
            )
        assert all(r.status_code == 200 for r in events)
        assert sum(not r.json()["duplicate"] for r in events) == 1
        assert (
            c.get(f"/v1/payment-intents/{item['id']}", headers=OWN).json()["status"]
            == "succeeded"
        )
        assert (
            replica.get(f"/v1/payment-intents/{item['id']}", headers=OTHER).status_code
            == 404
        )


def test_concurrent_reconcilers_use_same_provider_identity(runtime):
    from concurrent.futures import ThreadPoolExecutor

    c, db, ws, provider = runtime
    provider.timeout = True
    item = c.post("/v1/payment-intents", json=BODY, headers=OWN).json()
    assert item["status"] == "unknown"
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(
                lambda _: c.post(
                    f"/v1/payment-intents/{item['id']}/reconcile", headers=OWN
                ),
                range(12),
            )
        )
    assert all(r.status_code in (200, 202) for r in results)
    assert {r.json()["id"] for r in results} == {item["id"]}
    assert provider.calls == 1
    assert (
        c.get(f"/v1/payment-intents/{item['id']}", headers=OWN).json()["status"]
        == "pending"
    )


def test_slow_webhook_work_does_not_block_health_endpoint(runtime):
    from concurrent.futures import ThreadPoolExecutor

    c, db, ws, provider = runtime
    item = c.post("/v1/payment-intents", json=BODY, headers=OWN).json()
    original = provider.verify_event
    started = threading.Event()
    release = threading.Event()

    def delayed(payload, signature):
        started.set()
        if not release.wait(3):
            raise ValueError("fixture gate timeout")
        return original(payload, signature)

    provider.verify_event = delayed
    with ThreadPoolExecutor(max_workers=2) as pool:
        webhook = pool.submit(event, c, item["provider_object_id"])
        assert started.wait(2)
        health = pool.submit(c.get, "/healthz")
        try:
            assert health.result(timeout=0.5).status_code == 200
        finally:
            release.set()
        assert webhook.result(timeout=2).status_code == 200
