from uuid import UUID, uuid4
from fastapi import Depends, Header, HTTPException, Query, Response
from platform_backend_core import require_scope
from .storage import transaction, fetch


def subscription(row):
    return {
        k: row[k]
        for k in (
            "id",
            "plan",
            "quantity",
            "unit_amount",
            "currency",
            "interval",
            "status",
            "cancel_at_period_end",
            "cancel_status",
            "current_period_end",
            "created_at",
        )
    }


def invoice(row):
    return {
        k: row[k]
        for k in (
            "id",
            "subscription_id",
            "provider_object_id",
            "amount_due",
            "amount_paid",
            "currency",
            "status",
            "period_start",
            "period_end",
            "created_at",
        )
    }


def register(app, database, provider, catalog):
    def enabled():
        if (
            not catalog
            or provider is None
            or not callable(getattr(provider, "cancel_subscription", None))
        ):
            raise HTTPException(503, "subscription provider not configured")
        return provider

    @app.get("/v1/subscriptions")
    def subscriptions(
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=10000),
        principal=Depends(require_scope("billing:read")),
    ):
        with transaction(database, principal.workspace) as conn:
            return [
                subscription(r)
                for r in conn.execute(
                    "SELECT * FROM billing_subscriptions ORDER BY created_at DESC,id LIMIT %s OFFSET %s",
                    (limit, offset),
                ).fetchall()
            ]

    @app.get("/v1/subscriptions/{identifier}")
    def detail(identifier: UUID, principal=Depends(require_scope("billing:read"))):
        with transaction(database, principal.workspace) as conn:
            return subscription(fetch(conn, "billing_subscriptions", identifier))

    @app.get("/v1/invoices")
    def invoices(
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=10000),
        principal=Depends(require_scope("billing:read")),
    ):
        with transaction(database, principal.workspace) as conn:
            return [
                invoice(r)
                for r in conn.execute(
                    "SELECT * FROM billing_invoices ORDER BY created_at DESC,id LIMIT %s OFFSET %s",
                    (limit, offset),
                ).fetchall()
            ]

    @app.get("/v1/invoices/{identifier}")
    def invoice_detail(
        identifier: UUID, principal=Depends(require_scope("billing:read"))
    ):
        with transaction(database, principal.workspace) as conn:
            return invoice(fetch(conn, "billing_invoices", identifier))

    def perform(row, workspace, adapter, fence):
        try:
            result = adapter.cancel_subscription(
                subscription_id=row["provider_object_id"],
                idempotency_key=row["cancel_provider_key"],
            )
            if result.get("id") != row["provider_object_id"]:
                raise ValueError("invalid provider response")
            state = "pending"
            status = 200
        except Exception:
            state = "unknown"
            status = 202
        with transaction(database, workspace) as conn:
            conn.execute(
                "UPDATE billing_subscriptions SET cancel_status=%s,cancel_fence=NULL,cancel_lease_until=NULL,updated_at=now() WHERE id=%s AND cancel_fence=%s",
                (state, row["id"], fence),
            )
            return subscription(fetch(conn, "billing_subscriptions", row["id"])), status

    @app.post("/v1/subscriptions/{identifier}/cancel")
    def cancel(
        identifier: UUID,
        response: Response,
        principal=Depends(require_scope("billing:write")),
        key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
        adapter=Depends(enabled),
    ):
        fence = uuid4()
        with transaction(database, principal.workspace) as conn:
            row = fetch(conn, "billing_subscriptions", identifier, True)
            if row["provider"] != adapter.name:
                raise HTTPException(409, "provider configuration changed")
            if row["status"] in {"canceled", "incomplete_expired"}:
                return subscription(row)
            if row["cancel_status"] != "none":
                if row["cancel_idempotency_key"] != key:
                    raise HTTPException(409, "cancellation operation already reserved")
                response.status_code = (
                    202 if row["cancel_status"] in {"creating", "unknown"} else 200
                )
                return subscription(row)
            row = conn.execute(
                "UPDATE billing_subscriptions SET cancel_status='creating',cancel_idempotency_key=%s,cancel_provider_key=%s,cancel_fence=%s,cancel_lease_until=now()+interval '60 seconds',cancel_created_at=now() WHERE id=%s RETURNING *",
                (key, "platform-cancel-" + str(uuid4()), fence, identifier),
            ).fetchone()
        value, status = perform(row, principal.workspace, adapter, fence)
        response.status_code = status
        return value

    @app.post("/v1/subscriptions/{identifier}/cancel/reconcile")
    def reconcile(
        identifier: UUID,
        response: Response,
        principal=Depends(require_scope("billing:write")),
        adapter=Depends(enabled),
    ):
        fence = uuid4()
        with transaction(database, principal.workspace) as conn:
            row = fetch(conn, "billing_subscriptions", identifier, True)
            if row["provider"] != adapter.name:
                raise HTTPException(409, "provider configuration changed")
            if row["cancel_status"] not in {"creating", "unknown"}:
                return subscription(row)
            timing = conn.execute(
                "SELECT %s>now() AS leased,%s<now()-interval '20 hours' AS stale",
                (row["cancel_lease_until"], row["cancel_created_at"]),
            ).fetchone()
            if timing["stale"]:
                raise HTTPException(
                    409,
                    "provider idempotency window elapsed; manual reconciliation required",
                )
            if timing["leased"]:
                response.status_code = 202
                return subscription(row)
            conn.execute(
                "UPDATE billing_subscriptions SET cancel_fence=%s,cancel_lease_until=now()+interval '60 seconds' WHERE id=%s",
                (fence, identifier),
            )
        value, status = perform(row, principal.workspace, adapter, fence)
        response.status_code = status
        return value
