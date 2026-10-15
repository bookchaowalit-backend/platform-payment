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


def test_refund_reservation_prevents_concurrent_overrefund_and_settles_once(billing):
    c, db, ws, p, _ = billing
    item = paid(c)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda k: refund(c, item, 1000, k), ["refund-a", "refund-b"])
        )
    assert sorted(r.status_code for r in results) == [201, 409], [
        (r.status_code, r.text) for r in results
    ]
    record = next(r.json() for r in results if r.status_code == 201)
    assert p.extended_calls["refund"] == 1
    assert (
        c.post(
            f"/v1/payment-intents/{item['id']}/refunds",
            json={"amount": 100},
            headers=OTHER,
        ).status_code
        == 404
    )
    assert (
        signed(
            c,
            kind="refund",
            object_id=record["provider_object_id"],
            status="succeeded",
            amount=999,
            currency="usd",
            event_id="evt_wrong_refund_amount",
        ).status_code
        == 409
    )

    def settle(_):
        return signed(
            c,
            kind="refund",
            object_id=record["provider_object_id"],
            status="succeeded",
            amount=1000,
            currency="usd",
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        events = list(pool.map(settle, range(12)))
    assert all(r.status_code == 200 for r in events)
    assert sum(not r.json()["duplicate"] for r in events) == 1
    assert (
        signed(
            c,
            kind="refund",
            object_id=record["provider_object_id"],
            event_id="evt_late_failed",
            status="failed",
            amount=1000,
            currency="usd",
            created=50,
        ).status_code
        == 200
    )
    assert refund(c, item, 501, "excess").status_code == 409
    with db.transaction(ws) as conn:
        assert conn.execute(
            "SELECT refunded_minor,reserved_refund_minor FROM payment_intents WHERE id=%s",
            (item["id"],),
        ).fetchone() == (1000, 0)
        assert (
            conn.execute("SELECT count(*) FROM payment_refund_effects").fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT count(*) FROM payment_effects").fetchone()[0] == 1


def test_failed_refund_releases_reservation_and_changed_key_conflicts(billing):
    c, db, ws, p, _ = billing
    item = paid(c)
    first = refund(c, item).json()
    assert refund(c, item).json()["id"] == first["id"]
    assert refund(c, item, 999).status_code == 409
    assert (
        signed(
            c,
            kind="refund",
            object_id=first["provider_object_id"],
            status="failed",
            amount=1000,
            currency="usd",
        ).status_code
        == 200
    )
    assert (
        signed(
            c,
            kind="refund",
            object_id=first["provider_object_id"],
            event_id="evt_stale_pending",
            created=50,
            status="pending",
            amount=1000,
            currency="usd",
        ).status_code
        == 200
    )
    assert refund(c, item, 1500, "replacement").status_code == 201
    assert c.get(f"/v1/refunds/{first['id']}", headers=OWN).json()["status"] == "failed"
    assert c.get(f"/v1/refunds/{first['id']}", headers=OTHER).status_code == 404


def test_refund_timeout_is_reconciled_with_persisted_provider_key(billing):
    c, db, ws, p, _ = billing
    item = paid(c)
    p.fail_next = "refund"
    first = refund(c, item)
    assert first.status_code == 202, first.text
    record = first.json()
    assert record["status"] == "unknown"
    assert refund(c, item).status_code == 202
    assert refund(c, item, 1000, "another").status_code == 409
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(
            pool.map(
                lambda _: c.post(f"/v1/refunds/{record['id']}/reconcile", headers=OWN),
                range(6),
            )
        )
    assert all(r.status_code in (200, 202) for r in replies)
    assert p.extended_calls["refund"] == 1
    final = c.get(f"/v1/refunds/{record['id']}", headers=OWN).json()
    assert final["provider_object_id"] == "re_" + record["id"]
    assert (
        signed(
            c,
            kind="refund",
            object_id=final["provider_object_id"],
            status="succeeded",
            amount=1000,
            currency="usd",
        ).status_code
        == 200
    )


def test_catalog_snapshot_replay_and_exhausted_refund_window(billing):
    c, db, ws, p, dsn = billing
    original = checkout(c).json()
    changed = {
        "basic": {
            **CATALOG["basic"],
            "unit_amount": 2500,
            "price_id": "price_reconfigured",
        }
    }
    with TestClient(
        create_app(
            Database(dsn, max_size=2),
            c.app.state.verifier,
            provider=p,
            plan_catalog=changed,
        )
    ) as replica:
        assert checkout(replica).json()["unit_amount"] == 1500
        assert p.extended_calls["checkout"] == 1
    item = paid(c)
    p.fail_next = "refund"
    record = refund(c, item).json()
    with db.transaction(ws) as conn:
        conn.execute(
            "UPDATE payment_refunds SET created_at=now()-interval '21 hours' WHERE id=%s",
            (record["id"],),
        )
    assert (
        c.post(f"/v1/refunds/{record['id']}/reconcile", headers=OWN).status_code == 409
    )
    assert refund(c, item, 501, "over-reservation").status_code == 409


def test_refund_signed_amount_is_strict_integer_and_currency_bound(billing):
    c, db, ws, p, _ = billing
    item = c.post(
        "/v1/payment-intents", json={"amount": 1, "currency": "usd"}, headers=OWN
    ).json()
    assert event(c, item["provider_object_id"], amount=1).status_code == 200
    record = refund(c, item, 1).json()
    assert (
        signed(
            c,
            kind="refund",
            object_id=record["provider_object_id"],
            status="succeeded",
            amount=True,
            currency="usd",
            event_id="evt_boolean_refund",
        ).status_code
        == 400
    )
    assert (
        signed(
            c,
            kind="refund",
            object_id=record["provider_object_id"],
            status="succeeded",
            amount=1,
            currency="eur",
            event_id="evt_wrong_refund_currency",
        ).status_code
        == 409
    )
    with db.transaction(ws) as conn:
        assert conn.execute(
            "SELECT refunded_minor,reserved_refund_minor FROM payment_intents WHERE id=%s",
            (item["id"],),
        ).fetchone() == (0, 1)
