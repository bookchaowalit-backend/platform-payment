"""One-time payment settlement, shared by HTTP delivery and durable replay."""

from uuid import UUID

from fastapi import HTTPException

from .billing_events import lookup
from .ledger import post
from .pending_webhooks import applied, context
from .storage import fetch, inbox, transaction

# A binding is written right after the provider object is created, so a short race is
# retried; an object still unbound after this many deliveries belongs to nothing here
# (for example the payment intent under a Checkout session) and is acknowledged.
UNBOUND_DELIVERIES = 3


def ignore_unbound(database, provider, event_id):
    with transaction(database, UUID(int=0)) as conn:
        context(conn, provider, event_id)
        row = conn.execute(
            "SELECT attempts FROM payment_pending_webhooks WHERE provider=%s AND event_id=%s",
            (provider, event_id),
        ).fetchone()
        if row is None or row["attempts"] < UNBOUND_DELIVERIES:
            return False
        applied(conn, provider, event_id)
        return True


def handle(database, provider, event, fingerprint):
    binding = lookup(database, provider.name, event["provider_object_id"])
    if binding is None and ignore_unbound(database, provider.name, event["id"]):
        return {"accepted": True, "ignored": True}
    if binding is None or binding["resource_kind"] != "payment":
        raise HTTPException(
            409, "provider object not bound; retry after reconciliation"
        )
    with transaction(database, binding["workspace_id"]) as conn:
        row = fetch(conn, "payment_intents", binding["resource_id"], True)
        if row["amount"] != event.get("amount") or row["currency"] != event.get(
            "currency"
        ):
            raise HTTPException(409, "provider amount or currency mismatch")
        inserted = inbox(
            conn,
            provider.name,
            event,
            fingerprint,
            row["workspace_id"],
            "payment",
            row["id"],
        )
        if inserted:
            status = event["status"]
            if row["status"] != "succeeded" and (
                status == "succeeded" or row["status"] != "failed"
            ):
                conn.execute(
                    "UPDATE payment_intents SET status=%s,updated_at=now() WHERE id=%s",
                    (status, row["id"]),
                )
            if status == "succeeded":
                effect = conn.execute(
                    "INSERT INTO payment_effects(workspace_id,intent_id,kind,amount,currency) VALUES(%s,%s,'payment_succeeded',%s,%s) ON CONFLICT DO NOTHING RETURNING intent_id",
                    (row["workspace_id"], row["id"], row["amount"], row["currency"]),
                ).fetchone()
                if effect is not None:
                    post(
                        conn,
                        workspace=row["workspace_id"],
                        source_kind="payment_succeeded",
                        source_id=row["id"],
                        intent_id=row["id"],
                        amount=row["amount"],
                        currency=row["currency"],
                    )
        applied(conn, provider.name, event["id"])
        return {"accepted": True, "duplicate": not inserted}
