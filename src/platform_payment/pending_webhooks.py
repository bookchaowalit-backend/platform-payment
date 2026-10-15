"""Durable normalized signed-event spool. No tenant API can scan unbound events."""

import json
from uuid import UUID
from fastapi import HTTPException
from psycopg.types.json import Jsonb
from .storage import transaction

FIELDS = {
    "id",
    "kind",
    "provider_object_id",
    "created",
    "status",
    "amount",
    "currency",
    "subscription_id",
    "customer_id",
    "price_id",
    "unit_amount",
    "quantity",
    "interval",
    "current_period_end",
    "cancel_at_period_end",
    "amount_due",
    "amount_paid",
    "period_start",
    "period_end",
    "payment_id",
}


def context(conn, provider, event_id):
    conn.execute(
        "SELECT set_config('app.payment_provider',%s,true),set_config('app.payment_event',%s,true)",
        (provider, event_id),
    )


def stage(database, provider, event, fingerprint):
    normalized = {k: v for k, v in event.items() if k in FIELDS}
    if len(json.dumps(normalized).encode()) > 60000:
        raise HTTPException(400, "provider event too large")
    with transaction(database, UUID(int=0)) as conn:
        context(conn, provider, event["id"])
        row = conn.execute(
            "INSERT INTO payment_pending_webhooks(provider,event_id,provider_object_id,fingerprint,normalized) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(provider,event_id) DO UPDATE SET attempts=least(payment_pending_webhooks.attempts+1,100),updated_at=now() RETURNING fingerprint",
            (
                provider,
                event["id"],
                event["provider_object_id"],
                fingerprint,
                Jsonb(normalized),
            ),
        ).fetchone()
        if row["fingerprint"] != fingerprint:
            raise HTTPException(409, "provider event identity conflict")


def applied(conn, provider, event_id):
    context(conn, provider, event_id)
    conn.execute(
        "UPDATE payment_pending_webhooks SET state='applied',updated_at=now() WHERE provider=%s AND event_id=%s",
        (provider, event_id),
    )


def replay(database, provider, *, limit=50):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("replay limit must be 1..100")
    with transaction(database, UUID(int=0)) as conn:
        conn.execute(
            "SELECT set_config('app.payment_provider',%s,true),set_config('app.payment_replay','true',true)",
            (provider.name,),
        )
        rows = conn.execute(
            "SELECT normalized,fingerprint FROM payment_pending_webhooks WHERE provider=%s AND state='pending' AND attempts<20 ORDER BY received_at,event_id LIMIT %s",
            (provider.name, limit),
        ).fetchall()
    from .event_router import apply_verified

    count = 0
    for row in rows:
        try:
            apply_verified(database, provider, row["normalized"], row["fingerprint"])
            count += 1
        except HTTPException:
            pass
    return {"applied": count, "pending": len(rows) - count}
