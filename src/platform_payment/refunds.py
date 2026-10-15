import hashlib
from uuid import UUID, uuid4
from fastapi import Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from platform_backend_core import require_scope
from .storage import transaction, fetch, inbox
from .provider_operations import invoke, claim


class Refund(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    amount: int = Field(ge=1, le=999999999)


def public(row):
    return {
        k: row[k]
        for k in (
            "id",
            "intent_id",
            "amount",
            "currency",
            "status",
            "provider_object_id",
            "created_at",
        )
    }


def register(app, database, provider):
    def enabled():
        if provider is None or not callable(getattr(provider, "create_refund", None)):
            raise HTTPException(503, "refund provider not configured")
        return provider

    def perform(row, workspace, adapter, fence):
        value, status = invoke(
            database,
            workspace,
            "payment_refunds",
            row,
            adapter,
            fence,
            lambda: adapter.create_refund(
                payment_id=row["payment_object_id"],
                amount=row["amount"],
                currency=row["currency"],
                reference=str(row["id"]),
                idempotency_key=row["provider_key"],
            ),
        )
        return public(value), status

    @app.post("/v1/payment-intents/{identifier}/refunds")
    def create(
        identifier: UUID,
        body: Refund,
        response: Response,
        principal=Depends(require_scope("payments:refund")),
        key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
        adapter=Depends(enabled),
    ):
        fingerprint = hashlib.sha256(str(body.amount).encode()).hexdigest()
        refund_id = uuid4()
        fence = uuid4()
        with transaction(database, principal.workspace) as conn:
            payment = fetch(conn, "payment_intents", identifier, True)
            prior = conn.execute(
                "SELECT * FROM payment_refunds WHERE workspace_id=%s AND intent_id=%s AND idempotency_key=%s",
                (principal.workspace, identifier, key),
            ).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise HTTPException(409, "idempotency key payload conflict")
                response.status_code = (
                    202 if prior["status"] in {"creating", "unknown"} else 200
                )
                return public(prior)
            if (
                payment["status"] != "succeeded"
                or payment["provider"] != adapter.name
                or not payment["provider_object_id"]
            ):
                raise HTTPException(409, "payment is not refundable")
            if (
                body.amount
                > payment["amount"]
                - payment["refunded_minor"]
                - payment["reserved_refund_minor"]
            ):
                raise HTTPException(409, "refund exceeds available paid balance")
            conn.execute(
                "UPDATE payment_intents SET reserved_refund_minor=reserved_refund_minor+%s WHERE id=%s",
                (body.amount, identifier),
            )
            row = conn.execute(
                "INSERT INTO payment_refunds(id,workspace_id,actor_id,intent_id,idempotency_key,fingerprint,amount,currency,provider,provider_key,payment_object_id,status,fence,lease_until) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'creating',%s,now()+interval '60 seconds') RETURNING *",
                (
                    refund_id,
                    principal.workspace,
                    principal.actor,
                    identifier,
                    key,
                    fingerprint,
                    body.amount,
                    payment["currency"],
                    adapter.name,
                    "platform-refund-" + str(refund_id),
                    payment["provider_object_id"],
                    fence,
                ),
            ).fetchone()
        value, status = perform(row, principal.workspace, adapter, fence)
        response.status_code = 201 if status == 200 else status
        return value

    @app.get("/v1/payment-intents/{identifier}/refunds")
    def listing(
        identifier: UUID,
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=10000),
        principal=Depends(require_scope("payments:read")),
    ):
        with transaction(database, principal.workspace) as conn:
            fetch(conn, "payment_intents", identifier)
            return [
                public(r)
                for r in conn.execute(
                    "SELECT * FROM payment_refunds WHERE intent_id=%s ORDER BY created_at DESC,id LIMIT %s OFFSET %s",
                    (identifier, limit, offset),
                ).fetchall()
            ]

    @app.get("/v1/refunds/{identifier}")
    def detail(identifier: UUID, principal=Depends(require_scope("payments:read"))):
        with transaction(database, principal.workspace) as conn:
            return public(fetch(conn, "payment_refunds", identifier))

    @app.post("/v1/refunds/{identifier}/reconcile")
    def reconcile(
        identifier: UUID,
        response: Response,
        principal=Depends(require_scope("payments:refund")),
        adapter=Depends(enabled),
    ):
        with transaction(database, principal.workspace) as conn:
            row, fence, status = claim(conn, "payment_refunds", identifier, adapter)
        if fence is None:
            response.status_code = status
            return public(row)
        value, status = perform(row, principal.workspace, adapter, fence)
        response.status_code = status
        return value


def process(conn, provider, event, fingerprint, binding):
    row = fetch(conn, "payment_refunds", binding["resource_id"], True)
    if type(event.get("amount")) is not int:
        raise HTTPException(400, "invalid refund amount")
    if row["amount"] != event.get("amount") or row["currency"] != event.get("currency"):
        raise HTTPException(409, "refund amount or currency mismatch")
    if (
        event.get("payment_id")
        and row["provider_payment_id"]
        and event["payment_id"] != row["provider_payment_id"]
    ):
        raise HTTPException(409, "refund payment binding mismatch")
    if event.get("status") not in {"pending", "succeeded", "failed"}:
        raise HTTPException(400, "unsupported refund status")
    if not inbox(
        conn,
        provider.name,
        event,
        fingerprint,
        binding["workspace_id"],
        "refund",
        row["id"],
    ):
        return True
    if row["status"] in {"succeeded", "failed"} or event["status"] == "pending":
        return False
    # Parent balance is locked by this UPDATE; reservations include unknown/pending work.
    if event["status"] == "succeeded":
        conn.execute(
            "UPDATE payment_intents SET reserved_refund_minor=reserved_refund_minor-%s,refunded_minor=refunded_minor+%s WHERE id=%s",
            (row["amount"], row["amount"], row["intent_id"]),
        )
        conn.execute(
            "INSERT INTO payment_refund_effects(workspace_id,refund_id,intent_id,amount,currency) VALUES(%s,%s,%s,%s,%s)",
            (
                row["workspace_id"],
                row["id"],
                row["intent_id"],
                row["amount"],
                row["currency"],
            ),
        )
    else:
        conn.execute(
            "UPDATE payment_intents SET reserved_refund_minor=reserved_refund_minor-%s WHERE id=%s",
            (row["amount"], row["intent_id"]),
        )
    conn.execute(
        "UPDATE payment_refunds SET status=%s,updated_at=now() WHERE id=%s",
        (event["status"], row["id"]),
    )
    return False
