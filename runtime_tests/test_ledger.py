"""Double-entry ledger gate: balanced journals, refund bound, immutability, rollback.

Every settled payment and refund posts one journal in integer minor units. A
journal must balance inside its own INSERT statement, refunds can never exceed
the captured amount, entries are append-only, and a failed posting rolls back the
whole settlement so a redelivered webhook applies exactly once.
"""

import os
from uuid import uuid4

import psycopg
import pytest
from platform_backend_core import MigrationRunner
from platform_backend_core.testing import temporary_database

from platform_payment import MIGRATIONS

from .billing_support import BODY, OWN, event, paid, refund, signed


def journals(db, ws):
    with db.transaction(ws) as conn:
        return conn.execute(
            "SELECT source_kind,account,direction,amount,currency FROM payment_ledger_entries"
            " ORDER BY source_kind,account,direction"
        ).fetchall()


def settle_refund(c, record, amount=1000, event_id=None):
    return signed(
        c,
        kind="refund",
        object_id=record["provider_object_id"],
        status="succeeded",
        amount=amount,
        currency="usd",
        event_id=event_id,
    )


def test_payment_and_refund_post_balanced_journals_once(billing):
    c, db, ws, _p, _ = billing
    item = paid(c)
    assert journals(db, ws) == [
        ("payment_succeeded", "payment_revenue", "credit", 1500, "usd"),
        ("payment_succeeded", "provider_clearing", "debit", 1500, "usd"),
    ]
    # Redelivery of the same verified event posts nothing new.
    assert event(c, item["provider_object_id"]).json()["duplicate"] is True
    record = refund(c, item, 1000).json()
    assert settle_refund(c, record).status_code == 200
    assert settle_refund(c, record).json()["duplicate"] is True
    rows = journals(db, ws)
    assert rows == [
        ("payment_succeeded", "payment_revenue", "credit", 1500, "usd"),
        ("payment_succeeded", "provider_clearing", "debit", 1500, "usd"),
        ("refund_succeeded", "payment_refunds", "debit", 1000, "usd"),
        ("refund_succeeded", "provider_clearing", "credit", 1000, "usd"),
    ]
    debits = sum(r[3] for r in rows if r[2] == "debit")
    credits = sum(r[3] for r in rows if r[2] == "credit")
    assert debits == credits == 2500
    with db.transaction(ws) as conn:
        clearing = conn.execute(
            "SELECT sum(CASE direction WHEN 'debit' THEN amount ELSE -amount END)"
            " FROM payment_ledger_entries WHERE account='provider_clearing' AND intent_id=%s",
            (item["id"],),
        ).fetchone()[0]
        refunded = conn.execute(
            "SELECT amount-refunded_minor FROM payment_intents WHERE id=%s", (item["id"],)
        ).fetchone()[0]
    assert clearing == refunded == 500


def test_unbalanced_journal_is_rejected_and_nothing_is_written(billing):
    c, db, ws, _p, _ = billing
    item = paid(c)
    with pytest.raises(psycopg.errors.CheckViolation, match="unbalanced"), db.transaction(ws) as conn:
        conn.execute(
            "INSERT INTO payment_ledger_entries(workspace_id,source_kind,source_id,intent_id,account,direction,amount,currency)"
            " VALUES(%s,'refund_succeeded',%s,%s,'payment_refunds','debit',100,'usd')",
            (ws, uuid4(), item["id"]),
        )
    with pytest.raises(psycopg.errors.CheckViolation, match="unbalanced"), db.transaction(ws) as conn:
        source = uuid4()
        conn.execute(
            "INSERT INTO payment_ledger_entries(workspace_id,source_kind,source_id,intent_id,account,direction,amount,currency)"
            " VALUES(%s,'refund_succeeded',%s,%s,'payment_refunds','debit',100,'usd'),"
            " (%s,'refund_succeeded',%s,%s,'provider_clearing','credit',100,'eur')",
            (ws, source, item["id"], ws, source, item["id"]),
        )
    assert len(journals(db, ws)) == 2


def test_ledger_refunds_can_never_exceed_the_captured_amount(billing):
    c, db, ws, _p, _ = billing
    item = paid(c)
    source = uuid4()
    with pytest.raises(psycopg.errors.CheckViolation, match="captured"), db.transaction(ws) as conn:
        conn.execute(
            "INSERT INTO payment_ledger_entries(workspace_id,source_kind,source_id,intent_id,account,direction,amount,currency)"
            " VALUES(%s,'refund_succeeded',%s,%s,'payment_refunds','debit',1501,'usd'),"
            " (%s,'refund_succeeded',%s,%s,'provider_clearing','credit',1501,'usd')",
            (ws, source, item["id"], ws, source, item["id"]),
        )
    assert len(journals(db, ws)) == 2


def test_ledger_entries_are_append_only(billing):
    c, db, ws, _p, _ = billing
    paid(c)
    for statement in (
        "UPDATE payment_ledger_entries SET amount=amount+1",
        "DELETE FROM payment_ledger_entries",
    ):
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"), db.transaction(ws) as conn:
            conn.execute(statement)
    assert len(journals(db, ws)) == 2


def test_failed_posting_rolls_back_the_settlement_and_redelivery_recovers(
    billing, monkeypatch
):
    from platform_payment import ledger

    c, db, ws, _p, _ = billing
    item = c.post("/v1/payment-intents", json=BODY, headers=OWN).json()
    # Simulate a posting bug: a one-legged journal.
    monkeypatch.setitem(
        ledger.JOURNALS, "payment_succeeded", (("provider_clearing", "debit"),)
    )
    # The core error handler redacts the database error to a 500; the provider retries.
    failed = event(c, item["provider_object_id"])
    assert failed.status_code == 500
    assert "ledger" not in failed.text
    with db.transaction(ws) as conn:
        assert conn.execute(
            "SELECT status FROM payment_intents WHERE id=%s", (item["id"],)
        ).fetchone() == ("pending",)
        assert conn.execute("SELECT count(*) FROM payment_effects").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM payment_webhook_inbox").fetchone() == (0,)
    assert journals(db, ws) == []
    monkeypatch.undo()
    response = event(c, item["provider_object_id"])
    assert response.status_code == 200, response.text
    assert response.json()["duplicate"] is False
    assert len(journals(db, ws)) == 2


def test_failed_refund_posting_keeps_the_reservation_for_redelivery(
    billing, monkeypatch
):
    from platform_payment import ledger

    c, db, ws, _p, _ = billing
    item = paid(c)
    record = refund(c, item, 1000).json()
    monkeypatch.setitem(
        ledger.JOURNALS, "refund_succeeded", (("payment_refunds", "debit"),)
    )
    assert settle_refund(c, record).status_code == 500
    with db.transaction(ws) as conn:
        assert conn.execute(
            "SELECT refunded_minor,reserved_refund_minor FROM payment_intents WHERE id=%s",
            (item["id"],),
        ).fetchone() == (0, 1000)
        assert conn.execute("SELECT count(*) FROM payment_refund_effects").fetchone() == (0,)
    monkeypatch.undo()
    assert settle_refund(c, record).json()["duplicate"] is False
    assert len(journals(db, ws)) == 4


def test_migration_backfills_balanced_journals_from_existing_effects():
    admin = os.environ.get("PLATFORM_TEST_DSN")
    if not admin:
        pytest.skip("PLATFORM_TEST_DSN: disposable fixture admin required")
    pre_ledger = [m for m in MIGRATIONS if m.version in {"001-payments", "002-billing-refunds"}]
    assert len(pre_ledger) == 2
    with temporary_database(admin, prefix="ledger_backfill_") as isolated:
        MigrationRunner(isolated.admin_dsn, namespace="platform_payment").apply(pre_ledger)
        ws, intent, refund_id = uuid4(), uuid4(), uuid4()
        with psycopg.connect(isolated.admin_dsn) as conn:
            conn.execute(
                "INSERT INTO payment_intents(id,workspace_id,actor_id,operation,idempotency_key,fingerprint,amount,currency,provider,provider_key,provider_object_id,status,refunded_minor)"
                " VALUES(%s,%s,%s,'intent','k','f',1500,'usd','fixture','pk-1','pi_1','succeeded',400)",
                (intent, ws, uuid4()),
            )
            conn.execute(
                "INSERT INTO payment_effects(workspace_id,intent_id,kind,amount,currency) VALUES(%s,%s,'payment_succeeded',1500,'usd')",
                (ws, intent),
            )
            conn.execute(
                "INSERT INTO payment_refunds(id,workspace_id,actor_id,intent_id,idempotency_key,fingerprint,amount,currency,provider,provider_key,payment_object_id,status)"
                " VALUES(%s,%s,%s,%s,'rk','f',400,'usd','fixture','prk-1','pi_1','succeeded')",
                (refund_id, ws, uuid4(), intent),
            )
            conn.execute(
                "INSERT INTO payment_refund_effects(workspace_id,refund_id,intent_id,amount,currency) VALUES(%s,%s,%s,400,'usd')",
                (ws, refund_id, intent),
            )
        MigrationRunner(isolated.admin_dsn, namespace="platform_payment").apply(MIGRATIONS)
        with psycopg.connect(isolated.admin_dsn) as conn:
            rows = conn.execute(
                "SELECT source_kind,source_id,account,direction,amount FROM payment_ledger_entries"
                " ORDER BY source_kind,account"
            ).fetchall()
            forced = conn.execute(
                "SELECT relforcerowsecurity FROM pg_class WHERE relname=ANY(%s)",
                (["payment_effects", "payment_refund_effects", "payment_ledger_entries"],),
            ).fetchall()
    assert rows == [
        ("payment_succeeded", intent, "payment_revenue", "credit", 1500),
        ("payment_succeeded", intent, "provider_clearing", "debit", 1500),
        ("refund_succeeded", refund_id, "payment_refunds", "debit", 400),
        ("refund_succeeded", refund_id, "provider_clearing", "credit", 400),
    ]
    assert forced == [(True,), (True,), (True,)]
