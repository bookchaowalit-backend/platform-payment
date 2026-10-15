"""One-time payment settlement, shared by HTTP delivery and durable replay."""

from fastapi import HTTPException
from .storage import transaction, fetch, inbox
from .billing_events import lookup
from .pending_webhooks import applied


def handle(database, provider, event, fingerprint):
    binding = lookup(database, provider.name, event["provider_object_id"])
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
                conn.execute(
                    "INSERT INTO payment_effects(workspace_id,intent_id,kind,amount,currency) VALUES(%s,%s,'payment_succeeded',%s,%s) ON CONFLICT DO NOTHING",
                    (row["workspace_id"], row["id"], row["amount"], row["currency"]),
                )
        applied(conn, provider.name, event["id"])
        return {"accepted": True, "duplicate": not inserted}
