import hashlib
import json
from uuid import UUID, uuid4
from fastapi import Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from platform_backend_core import require_scope
from .storage import transaction, fetch
from .provider_operations import invoke, claim


class Checkout(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    plan: str = Field(min_length=1, max_length=64)
    quantity: int = Field(default=1, ge=1, le=100)


def public(row):
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
            "provider_object_id",
            "checkout_url",
            "created_at",
        )
    }


def register(app, database, provider, catalog):
    def enabled():
        if (
            not catalog
            or provider is None
            or not callable(getattr(provider, "create_subscription_checkout", None))
        ):
            raise HTTPException(
                503, "subscription provider or plan catalog not configured"
            )
        return provider

    def perform(row, workspace, adapter, fence):
        value, status = invoke(
            database,
            workspace,
            "billing_checkouts",
            row,
            adapter,
            fence,
            lambda: adapter.create_subscription_checkout(
                price_id=row["price_id"],
                quantity=row["quantity"],
                reference=str(row["id"]),
                idempotency_key=row["provider_key"],
            ),
        )
        return public(value), status

    @app.post("/v1/billing/checkouts")
    def checkout(
        body: Checkout,
        response: Response,
        principal=Depends(require_scope("billing:write")),
        key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    ):
        fingerprint = hashlib.sha256(
            json.dumps(
                body.model_dump(), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        identifier = uuid4()
        fence = uuid4()
        with transaction(database, principal.workspace) as conn:
            existing = conn.execute(
                "SELECT * FROM billing_checkouts WHERE workspace_id=%s AND idempotency_key=%s",
                (principal.workspace, key),
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise HTTPException(409, "idempotency key payload conflict")
                response.status_code = (
                    202 if existing["status"] in {"creating", "unknown"} else 200
                )
                return public(existing)
            adapter = enabled()
            plan = catalog.get(body.plan)
            if plan is None:
                raise HTTPException(422, "unknown configured plan")
            if body.quantity * plan.unit_amount > 999999999:
                raise HTTPException(422, "plan total exceeds limit")
            row = conn.execute(
                "INSERT INTO billing_checkouts(id,workspace_id,actor_id,idempotency_key,fingerprint,plan,price_id,unit_amount,currency,quantity,interval,provider,provider_key,status,fence,lease_until) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'creating',%s,now()+interval '60 seconds') ON CONFLICT(workspace_id,idempotency_key) DO NOTHING RETURNING *",
                (
                    identifier,
                    principal.workspace,
                    principal.actor,
                    key,
                    fingerprint,
                    body.plan,
                    plan.price_id,
                    plan.unit_amount,
                    plan.currency,
                    body.quantity,
                    plan.interval,
                    adapter.name,
                    "platform-subscription-" + str(identifier),
                    fence,
                ),
            ).fetchone()
            existing = conn.execute(
                "SELECT * FROM billing_checkouts WHERE workspace_id=%s AND idempotency_key=%s",
                (principal.workspace, key),
            ).fetchone()
            if existing["fingerprint"] != fingerprint:
                raise HTTPException(409, "idempotency key payload conflict")
            if row is None:
                response.status_code = (
                    202 if existing["status"] in {"creating", "unknown"} else 200
                )
                return public(existing)
        value, status = perform(row, principal.workspace, adapter, fence)
        response.status_code = 201 if status == 200 else status
        return value

    @app.get("/v1/billing/checkouts/{identifier}")
    def detail(identifier: UUID, principal=Depends(require_scope("billing:read"))):
        with transaction(database, principal.workspace) as conn:
            return public(fetch(conn, "billing_checkouts", identifier))

    @app.post("/v1/billing/checkouts/{identifier}/reconcile")
    def reconcile(
        identifier: UUID,
        response: Response,
        principal=Depends(require_scope("billing:write")),
        adapter=Depends(enabled),
    ):
        with transaction(database, principal.workspace) as conn:
            row, fence, status = claim(conn, "billing_checkouts", identifier, adapter)
        if fence is None:
            response.status_code = status
            return public(row)
        value, status = perform(row, principal.workspace, adapter, fence)
        response.status_code = status
        return value
