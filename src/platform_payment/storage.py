"""Tenant transactions restore connection attributes before returning pooled connections."""

from contextlib import contextmanager
from fastapi import HTTPException
from psycopg.rows import dict_row


@contextmanager
def transaction(database, workspace):
    with database.transaction(workspace) as conn:
        original = conn.row_factory
        conn.row_factory = dict_row
        try:
            yield conn
        finally:
            conn.row_factory = original


def fetch(conn, table, identifier, lock=False):
    allowed = {
        "billing_checkouts",
        "billing_subscriptions",
        "billing_invoices",
        "payment_refunds",
        "payment_intents",
    }
    if table not in allowed:
        raise ValueError("unknown internal table")
    row = conn.execute(
        "SELECT * FROM " + table + " WHERE id=%s" + (" FOR UPDATE" if lock else ""),
        (identifier,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "resource not found")
    return row


def bind(conn, provider, object_id, workspace, kind, resource_id):
    if not isinstance(object_id, str) or not 1 <= len(object_id) <= 255:
        raise HTTPException(409, "invalid provider object")
    conn.execute(
        "INSERT INTO payment_bindings(provider,provider_object_id,workspace_id,resource_kind,resource_id,intent_id) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (
            provider,
            object_id,
            workspace,
            kind,
            resource_id,
            resource_id if kind == "payment" else None,
        ),
    )
    bound = conn.execute(
        "SELECT workspace_id,resource_kind,resource_id FROM payment_bindings WHERE provider=%s AND provider_object_id=%s",
        (provider, object_id),
    ).fetchone()
    if bound is None or (
        bound["workspace_id"],
        bound["resource_kind"],
        bound["resource_id"],
    ) != (workspace, kind, resource_id):
        raise HTTPException(409, "provider object binding conflict")


def inbox(conn, provider, event, fingerprint, workspace, kind, resource_id):
    inserted = conn.execute(
        "INSERT INTO payment_webhook_inbox(provider,event_id,workspace_id,resource_kind,resource_id,intent_id,fingerprint) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING event_id",
        (
            provider,
            event["id"],
            workspace,
            kind,
            resource_id,
            resource_id if kind == "payment" else None,
            fingerprint,
        ),
    ).fetchone()
    if inserted:
        return True
    row = conn.execute(
        "SELECT fingerprint,resource_kind,resource_id FROM payment_webhook_inbox WHERE provider=%s AND event_id=%s",
        (provider, event["id"]),
    ).fetchone()
    if row is None or (
        row["fingerprint"],
        row["resource_kind"],
        row["resource_id"],
    ) != (fingerprint, kind, resource_id):
        raise HTTPException(409, "provider event identity conflict")
    return False
