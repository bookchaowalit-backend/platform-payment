import hashlib
import json
from uuid import UUID, uuid4
from fastapi.responses import JSONResponse
from fastapi import Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool
from platform_backend_core import create_app as core_app, require_scope
from .adapters import PaymentProvider
from .storage import transaction


class Payment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    amount: int = Field(ge=1, le=999999999)
    currency: str

    @field_validator("currency")
    @classmethod
    def supported(cls, value):
        # Explicit supported settlement currencies, integer units (JPY is zero-decimal).
        if value not in {"usd", "eur", "gbp", "thb", "jpy", "aud", "cad", "sgd"}:
            raise ValueError("unsupported lowercase currency")
        return value


def public(row):
    return {
        key: row[key]
        for key in (
            "id",
            "amount",
            "currency",
            "operation",
            "status",
            "provider_object_id",
            "checkout_url",
            "created_at",
        )
    }


def create_app(
    database, verifier, provider: PaymentProvider | None = None, *, plan_catalog=None
):
    if provider is None:
        from .config import configured_provider

        provider = configured_provider()
    from .catalog import load_catalog

    catalog = load_catalog(plan_catalog)
    app = core_app("platform-payment", database, verifier)
    app.state.provider = provider

    app.router.routes[:] = [
        r for r in app.router.routes if getattr(r, "path", None) != "/readyz"
    ]

    @app.get("/readyz", include_in_schema=False)
    def readiness():
        try:
            if provider is None:
                raise ValueError("provider unavailable")
            from .migrations import MIGRATIONS

            with transaction(database, UUID(int=0)) as conn:
                history = conn.execute(
                    "SELECT version,checksum FROM platform_schema_migrations WHERE namespace=%s",
                    ("platform_payment",),
                ).fetchall()
                if {r["version"]: r["checksum"] for r in history} != {
                    m.version: m.checksum for m in MIGRATIONS
                }:
                    raise ValueError("migration mismatch")
                rows = conn.execute(
                    "SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=ANY(%s::regclass[])",
                    (
                        [
                            "payment_intents",
                            "payment_bindings",
                            "payment_webhook_inbox",
                            "payment_effects",
                            "billing_checkouts",
                            "billing_subscriptions",
                            "billing_invoices",
                            "payment_refunds",
                            "payment_refund_effects",
                            "payment_pending_webhooks",
                        ],
                    ),
                ).fetchall()
                if len(rows) != 10 or not all(
                    r["relrowsecurity"] and r["relforcerowsecurity"] for r in rows
                ):
                    raise ValueError("RLS unavailable")
            return {"status": "ready"}
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)

    def configured():
        if provider is None:
            raise HTTPException(503, "payment provider not configured")
        return provider

    def get(conn, identifier, lock=False):
        row = conn.execute(
            "SELECT * FROM payment_intents WHERE id=%s"
            + (" FOR UPDATE" if lock else ""),
            (identifier,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "payment not found")
        return row

    def perform(row, workspace, adapter, fence):
        # Intent identity and provider idempotency key are already committed.
        try:
            result = adapter.create(
                amount=row["amount"],
                currency=row["currency"],
                reference=str(row["id"]),
                idempotency_key=row["provider_key"],
                checkout=row["operation"] == "checkout",
            )
            if (
                not isinstance(result.get("id"), str)
                or not 1 <= len(result["id"]) <= 255
            ):
                raise ValueError("invalid provider response")
        except Exception:
            with transaction(database, workspace) as conn:
                conn.execute(
                    "UPDATE payment_intents SET status='unknown',updated_at=now(),lease_until=NULL,fence=NULL WHERE id=%s AND fence=%s",
                    (row["id"], fence),
                )
                return public(get(conn, row["id"])), 202
        with transaction(database, workspace) as conn:
            current = get(conn, row["id"], True)
            if current["fence"] != fence:
                return public(current), 202
            # Durable provider object binding is committed with pending state.
            conn.execute(
                "INSERT INTO payment_bindings(provider,provider_object_id,workspace_id,intent_id,resource_id) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (adapter.name, result["id"], workspace, row["id"], row["id"]),
            )
            binding = conn.execute(
                "SELECT intent_id FROM payment_bindings WHERE provider=%s AND provider_object_id=%s",
                (adapter.name, result["id"]),
            ).fetchone()
            if binding is None or binding["intent_id"] != row["id"]:
                raise HTTPException(409, "provider object binding conflict")
            value = conn.execute(
                "UPDATE payment_intents SET provider_object_id=%s,checkout_url=%s,status='pending',lease_until=NULL,fence=NULL,updated_at=now() WHERE id=%s RETURNING *",
                (result["id"], result.get("checkout_url"), row["id"]),
            ).fetchone()
            return public(value), 200

    def create_payment(body, response, principal, key, adapter, operation):
        fingerprint = hashlib.sha256(
            json.dumps(
                body.model_dump(), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        identifier = uuid4()
        fence = uuid4()
        with transaction(database, principal.workspace) as conn:
            inserted = conn.execute(
                "INSERT INTO payment_intents(id,workspace_id,actor_id,operation,idempotency_key,fingerprint,amount,currency,provider,provider_key,status,fence,lease_until) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'creating',%s,now()+interval '60 seconds') ON CONFLICT(workspace_id,operation,idempotency_key) DO NOTHING RETURNING *",
                (
                    identifier,
                    principal.workspace,
                    principal.actor,
                    operation,
                    key,
                    fingerprint,
                    body.amount,
                    body.currency,
                    adapter.name,
                    "platform-" + str(identifier),
                    fence,
                ),
            ).fetchone()
            row = conn.execute(
                "SELECT * FROM payment_intents WHERE workspace_id=%s AND operation=%s AND idempotency_key=%s",
                (principal.workspace, operation, key),
            ).fetchone()
            if row["fingerprint"] != fingerprint:
                raise HTTPException(409, "idempotency key payload conflict")
            if inserted is None:
                response.status_code = (
                    202 if row["status"] in {"creating", "unknown"} else 200
                )
                return public(row)
        value, status = perform(row, principal.workspace, adapter, fence)
        response.status_code = 201 if status == 200 else status
        return value

    @app.post("/v1/payment-intents")
    def create(
        body: Payment,
        response: Response,
        principal=Depends(require_scope("payments:write")),
        key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
        adapter=Depends(configured),
    ):
        return create_payment(body, response, principal, key, adapter, "intent")

    @app.post("/v1/checkout")
    def checkout(
        body: Payment,
        response: Response,
        principal=Depends(require_scope("payments:write")),
        key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
        adapter=Depends(configured),
    ):
        return create_payment(body, response, principal, key, adapter, "checkout")

    @app.post("/v1/payment-intents/{identifier}/reconcile")
    def reconcile(
        identifier: UUID,
        response: Response,
        principal=Depends(require_scope("payments:write")),
        adapter=Depends(configured),
    ):
        fence = uuid4()
        with transaction(database, principal.workspace) as conn:
            row = get(conn, identifier, True)
            if row["provider"] != adapter.name:
                raise HTTPException(409, "provider configuration changed")
            if row["status"] not in {"creating", "unknown"}:
                return public(row)
            timing = conn.execute(
                "SELECT %s > now() AS leased,%s < now()-interval '20 hours' AS stale",
                (row["lease_until"], row["created_at"]),
            ).fetchone()
            if timing["stale"]:
                raise HTTPException(
                    409,
                    "provider idempotency window elapsed; manual reconciliation required",
                )
            if timing["leased"]:
                response.status_code = 202
                return public(row)
            conn.execute(
                "UPDATE payment_intents SET fence=%s,lease_until=now()+interval '60 seconds' WHERE id=%s",
                (fence, identifier),
            )
        value, status = perform(row, principal.workspace, adapter, fence)
        response.status_code = status
        return value

    @app.get("/v1/payment-intents")
    def listing(
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=10000),
        principal=Depends(require_scope("payments:read")),
    ):
        with transaction(database, principal.workspace) as conn:
            return [
                public(row)
                for row in conn.execute(
                    "SELECT * FROM payment_intents ORDER BY created_at DESC,id LIMIT %s OFFSET %s",
                    (limit, offset),
                ).fetchall()
            ]

    @app.get("/v1/payment-intents/{identifier}")
    def detail(identifier: UUID, principal=Depends(require_scope("payments:read"))):
        with transaction(database, principal.workspace) as conn:
            return public(get(conn, identifier))

    @app.post("/v1/webhooks/provider")
    async def webhook(
        request: Request,
        signature: str | None = Header(default=None, alias="Stripe-Signature"),
        adapter=Depends(configured),
    ):
        payload = await request.body()
        return await run_in_threadpool(process_event, payload, signature, adapter)

    def process_event(payload, signature, adapter):
        try:
            event = adapter.verify_event(payload, signature)
            if event.get("ignore"):
                return {"accepted": True, "ignored": True}
            if any(
                not isinstance(event.get(k), str) or not 1 <= len(event[k]) <= 255
                for k in ("id", "provider_object_id")
            ):
                raise ValueError()
            if event.get("kind") not in {
                "subscription_checkout",
                "subscription",
                "invoice",
                "refund",
            } and (
                type(event.get("amount")) is not int
                or event.get("status")
                not in {
                    "pending",
                    "failed",
                    "succeeded",
                }
            ):
                raise ValueError()
        except Exception:
            raise HTTPException(400, "invalid provider event or signature") from None
        fingerprint = hashlib.sha256(payload).hexdigest()
        from .event_router import apply_verified

        return apply_verified(database, adapter, event, fingerprint)

    from .billing_checkouts import register as register_checkouts
    from .subscriptions import register as register_subscriptions
    from .refunds import register as register_refunds

    register_checkouts(app, database, provider, catalog)
    register_subscriptions(app, database, provider, catalog)
    register_refunds(app, database, provider)
    return app
