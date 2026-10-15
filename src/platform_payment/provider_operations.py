"""Durable provider work: commit identity before invocation, retain ambiguity, fence retries."""

from uuid import uuid4
from fastapi import HTTPException
from .storage import transaction, fetch, bind

TABLES = {"billing_checkouts": "subscription_checkout", "payment_refunds": "refund"}


def claim(conn, table, identifier, provider):
    row = fetch(conn, table, identifier, True)
    if row["provider"] != provider.name:
        raise HTTPException(409, "provider configuration changed")
    if row["status"] not in {"creating", "unknown"}:
        return row, None, 200
    timing = conn.execute(
        "SELECT %s>now() AS leased,%s<now()-interval '20 hours' AS stale",
        (row["lease_until"], row["created_at"]),
    ).fetchone()
    if timing["stale"]:
        raise HTTPException(
            409, "provider idempotency window elapsed; manual reconciliation required"
        )
    if timing["leased"]:
        return row, None, 202
    fence = uuid4()
    conn.execute(
        "UPDATE "
        + table
        + " SET fence=%s,lease_until=now()+interval '60 seconds' WHERE id=%s",
        (fence, identifier),
    )
    return row, fence, 200


def invoke(database, workspace, table, row, provider, fence, call):
    if table not in TABLES:
        raise ValueError("unknown provider work")
    try:
        result = call()
        if not isinstance(result.get("id"), str) or not 1 <= len(result["id"]) <= 255:
            raise ValueError("invalid provider object")
    except Exception:
        with transaction(database, workspace) as conn:
            conn.execute(
                "UPDATE "
                + table
                + " SET status='unknown',fence=NULL,lease_until=NULL,updated_at=now() WHERE id=%s AND fence=%s",
                (row["id"], fence),
            )
            return fetch(conn, table, row["id"]), 202
    with transaction(database, workspace) as conn:
        current = fetch(conn, table, row["id"], True)
        if current["fence"] != fence:
            return current, 202 if current["status"] in {"creating", "unknown"} else 200
        bind(conn, provider.name, result["id"], workspace, TABLES[table], row["id"])
        if table == "billing_checkouts":
            current = conn.execute(
                "UPDATE billing_checkouts SET provider_object_id=%s,checkout_url=%s,status='pending',fence=NULL,lease_until=NULL,updated_at=now() WHERE id=%s RETURNING *",
                (result["id"], result.get("checkout_url"), row["id"]),
            ).fetchone()
        else:
            current = conn.execute(
                "UPDATE payment_refunds SET provider_object_id=%s,provider_payment_id=%s,status='pending',fence=NULL,lease_until=NULL,updated_at=now() WHERE id=%s RETURNING *",
                (result["id"], result.get("payment_id"), row["id"]),
            ).fetchone()
        return current, 200
