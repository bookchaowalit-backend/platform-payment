import hashlib
import hmac
import json
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from platform_backend_core import Database, MigrationRunner, Principal
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


def test_catalog_allowlist_idempotency_and_workspace_isolation(billing):
    c, db, ws, p, _ = billing
    result = checkout(c)
    assert result.status_code == 201, result.text
    item = result.json()
    assert item["plan"] == "basic" and item["quantity"] == 1
    assert checkout(c).json()["id"] == item["id"]
    assert (
        c.post(
            "/v1/billing/checkouts",
            json={"plan": "basic", "quantity": 2},
            headers={**OWN, "Idempotency-Key": "billing-checkout"},
        ).status_code
        == 409
    )
    assert p.extended_calls["checkout"] == 1
    for body in [
        {"plan": "unknown", "quantity": 1},
        {"plan": "basic", "quantity": 0},
        {"plan": "basic", "quantity": True},
        {"plan": "basic", "quantity": 101},
        {"plan": "basic", "quantity": 1, "price_id": "price_evil"},
        {"plan": "basic", "quantity": 1, "success_url": "https://evil.invalid"},
        {"plan": "basic", "quantity": 1, "workspace_id": str(ws)},
    ]:
        assert (
            c.post("/v1/billing/checkouts", json=body, headers=OWN).status_code == 422
        )
    assert (
        c.get(f"/v1/billing/checkouts/{item['id']}", headers=OTHER).status_code == 404
    )
    assert c.get("/v1/subscriptions", headers=OTHER).json() == []


def test_subscription_invoice_binding_dedupe_and_out_of_order(billing):
    c, db, ws, p, _ = billing
    item = checkout(c).json()
    assert subscription_event(c, item).status_code == 409
    assert activate(c, item).status_code == 200
    assert activate(c, item).json()["duplicate"] is True
    assert (
        subscription_event(
            c, item, unit_amount=999, event_id="evt_bad_price"
        ).status_code
        == 409
    )
    assert subscription_event(c, item).status_code == 200
    subs = c.get("/v1/subscriptions", headers=OWN).json()
    assert len(subs) == 1
    sub = subs[0]
    assert sub["status"] == "active"
    assert c.get(f"/v1/subscriptions/{sub['id']}", headers=OTHER).status_code == 404
    invoice = {
        "subscription_id": "sub_" + item["id"],
        "customer_id": "cus_" + item["id"],
        "amount_due": 1500,
        "amount_paid": 1500,
        "currency": "usd",
        "status": "paid",
        "period_start": 1800000000,
        "period_end": 1900000000,
    }
    assert (
        signed(c, kind="invoice", object_id="in_" + item["id"], **invoice).status_code
        == 200
    )
    assert (
        signed(c, kind="invoice", object_id="in_" + item["id"], **invoice).json()[
            "duplicate"
        ]
        is True
    )
    assert (
        signed(
            c,
            kind="invoice",
            object_id="in_" + item["id"],
            event_id="evt_old_invoice",
            created=99,
            **{**invoice, "status": "open", "amount_paid": 0},
        ).status_code
        == 200
    )
    inv = c.get("/v1/invoices", headers=OWN).json()[0]
    assert inv["status"] == "paid"
    assert c.get(f"/v1/invoices/{inv['id']}", headers=OTHER).status_code == 404
    assert (
        signed(
            c,
            kind="invoice",
            object_id="in_unknown",
            **{**invoice, "subscription_id": "sub_unknown"},
        ).status_code
        == 409
    )
    assert (
        signed(
            c,
            kind="invoice",
            object_id="in_wrong_currency",
            **{**invoice, "currency": "eur"},
        ).status_code
        == 409
    )
    assert (
        subscription_event(
            c, item, status="canceled", created=300, event_id="evt_canceled"
        ).status_code
        == 200
    )
    assert (
        subscription_event(
            c, item, status="active", created=250, event_id="evt_stale_active"
        ).status_code
        == 200
    )
    assert (
        c.get(f"/v1/subscriptions/{sub['id']}", headers=OWN).json()["status"]
        == "canceled"
    )
    with db.transaction(uuid4()) as conn:
        for table in (
            "billing_checkouts",
            "billing_subscriptions",
            "billing_invoices",
            "payment_refunds",
        ):
            assert conn.execute(
                "SELECT relforcerowsecurity FROM pg_class WHERE oid=%s::regclass",
                (table,),
            ).fetchone()[0]
            assert conn.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0


def test_checkout_timeout_reconcile_and_unknown_event_retries(billing):
    c, db, ws, p, _ = billing
    p.fail_next = "checkout"
    first = checkout(c)
    assert first.status_code == 202, first.text
    item = first.json()
    assert item["status"] == "unknown"
    provider_object_id = "cs_sub_" + item["id"]
    retry_event = {**item, "provider_object_id": provider_object_id}
    assert activate(c, retry_event).status_code == 409
    assert checkout(c).status_code == 202
    result = c.post(f"/v1/billing/checkouts/{item['id']}/reconcile", headers=OWN)
    assert result.status_code == 200, result.text
    assert result.json()["id"] == item["id"] and p.extended_calls["checkout"] == 1
    assert activate(c, result.json()).status_code == 200


def test_cancel_is_at_period_end_idempotent_and_reconciles(billing):
    c, db, ws, p, _ = billing
    item = checkout(c).json()
    assert activate(c, item).status_code == 200
    assert subscription_event(c, item).status_code == 200
    sub = c.get("/v1/subscriptions", headers=OWN).json()[0]
    url = f"/v1/subscriptions/{sub['id']}/cancel"
    assert c.post(url, headers=OTHER).status_code == 404
    p.fail_next = "cancel"
    assert c.post(url, headers=OWN).status_code == 202
    assert c.post(url, headers=OWN).status_code == 202
    assert c.post(url + "/reconcile", headers=OWN).status_code == 200
    assert p.extended_calls["cancel"] == 1
    assert (
        subscription_event(
            c,
            item,
            created=300,
            event_id="evt_cancel_update",
            cancel_at_period_end=True,
        ).status_code
        == 200
    )
    assert (
        c.get(f"/v1/subscriptions/{sub['id']}", headers=OWN).json()[
            "cancel_at_period_end"
        ]
        is True
    )
    assert c.post(url, headers=OWN).status_code == 200


def test_missing_catalog_and_legacy_provider_fail_closed(billing, monkeypatch):
    c, db, ws, p, dsn = billing
    monkeypatch.delenv("PAYMENT_PLAN_CATALOG_JSON")
    second = Database(dsn, max_size=2)
    with TestClient(
        create_app(second, c.app.state.verifier, provider=FakeProvider())
    ) as legacy:
        assert checkout(legacy).status_code == 503
        item = paid(legacy)
        assert refund(legacy, item).status_code == 503

    class ReadOnly:
        def verify(self, auth):
            return Principal(uuid4(), ws, frozenset(["payments:read"]))

    c.app.state.verifier = ReadOnly()
    assert checkout(c).status_code == 403
    assert c.get("/v1/subscriptions", headers=OWN).status_code == 403


def test_two_pools_share_billing_idempotency_and_catalog_is_snapshotted(billing):
    c, db, ws, p, dsn = billing
    with TestClient(
        create_app(
            Database(dsn, max_size=4),
            c.app.state.verifier,
            provider=p,
            plan_catalog=CATALOG,
        )
    ) as replica:
        with ThreadPoolExecutor(max_workers=6) as pool:
            replies = list(
                pool.map(lambda n: checkout(c if n % 2 else replica), range(12))
            )
        assert all(r.status_code in (200, 201, 202) for r in replies)
        assert len({r.json()["id"] for r in replies}) == 1
        assert p.extended_calls["checkout"] == 1
        item = checkout(c).json()
        assert activate(replica, item).status_code == 200
        assert len(c.get("/v1/subscriptions", headers=OWN).json()) == 1


def test_events_before_binding_are_durably_retained_then_operator_replayed(billing):
    c, db, ws, p, _ = billing
    item = checkout(c).json()
    assert subscription_event(c, item).status_code == 409
    with db.transaction(ws) as conn:
        assert conn.execute(
            "SELECT to_regclass('payment_pending_webhooks') IS NOT NULL"
        ).fetchone()[0]
        assert (
            conn.execute("SELECT count(*) FROM payment_pending_webhooks").fetchone()[0]
            == 0
        )
    assert activate(c, item).status_code == 200
    from platform_payment.pending_webhooks import replay

    result = replay(db, p, limit=10)
    assert result["applied"] == 1 and result["pending"] == 0
    assert c.get("/v1/subscriptions", headers=OWN).json()[0]["status"] == "active"
    assert replay(db, p, limit=10)["applied"] == 0


def test_invoice_same_second_older_phase_cannot_replace_open(billing):
    c, db, ws, p, _ = billing
    item = checkout(c).json()
    assert activate(c, item).status_code == 200
    base = {
        "subscription_id": "sub_" + item["id"],
        "customer_id": "cus_" + item["id"],
        "amount_due": 1500,
        "amount_paid": 0,
        "currency": "usd",
        "period_start": 1800000000,
        "period_end": 1900000000,
    }
    assert (
        signed(
            c,
            kind="invoice",
            object_id="in_phase",
            event_id="evt_open",
            created=200,
            status="open",
            **base,
        ).status_code
        == 200
    )
    assert (
        signed(
            c,
            kind="invoice",
            object_id="in_phase",
            event_id="evt_draft_late",
            created=200,
            status="draft",
            **base,
        ).status_code
        == 200
    )
    assert c.get("/v1/invoices", headers=OWN).json()[0]["status"] == "open"


def test_migration002_upgrades_existing_binding_and_inbox_without_drift(monkeypatch):
    admin = os.environ.get("PLATFORM_TEST_DSN")
    if not admin:
        pytest.skip("disposable fixture admin required")
    assert (
        MIGRATIONS[0].checksum
        == "99fd787b2ed865bf64e8b418c9ec926ba0307b939aa5035ab5536f154fcd173f"
    )
    with temporary_database(admin, prefix="billing_upgrade_") as isolated:
        MigrationRunner(isolated.admin_dsn, namespace="platform_payment").apply(
            MIGRATIONS[:1]
        )
        isolated.grant_schema()
        db = Database(isolated.runtime_dsn, max_size=2)
        identifier = uuid4()
        ws = uuid4()
        try:
            with db.transaction(ws) as conn:
                conn.execute(
                    "INSERT INTO payment_intents(id,workspace_id,actor_id,operation,idempotency_key,fingerprint,amount,currency,provider,provider_key,provider_object_id,status) VALUES(%s,%s,%s,'intent','old-key','old-hash',1500,'usd','fixture','old-provider-key','pi_old','succeeded')",
                    (identifier, ws, uuid4()),
                )
                conn.execute(
                    "INSERT INTO payment_bindings(provider,provider_object_id,workspace_id,intent_id) VALUES('fixture','pi_old',%s,%s)",
                    (ws, identifier),
                )
                conn.execute(
                    "INSERT INTO payment_webhook_inbox(provider,event_id,workspace_id,intent_id,fingerprint) VALUES('fixture','evt_old',%s,%s,'old-body-hash')",
                    (ws, identifier),
                )
            assert MigrationRunner(
                isolated.admin_dsn, namespace="platform_payment"
            ).apply(MIGRATIONS) == ["002-billing-refunds"]
            isolated.grant_schema()
            with db.transaction(ws) as conn:
                assert conn.execute(
                    "SELECT resource_kind,resource_id FROM payment_bindings"
                ).fetchone() == ("payment", identifier)
                assert conn.execute(
                    "SELECT resource_id FROM payment_webhook_inbox"
                ).fetchone() == (identifier,)
                assert conn.execute(
                    "SELECT refunded_minor,reserved_refund_minor FROM payment_intents"
                ).fetchone() == (0, 0)
        finally:
            db.close()
