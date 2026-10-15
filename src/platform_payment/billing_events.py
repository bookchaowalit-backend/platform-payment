"""Apply signed billing events using only previously stored provider-object bindings."""

from uuid import UUID, uuid4
from fastapi import HTTPException
from .storage import transaction, fetch, bind, inbox

KINDS = {"subscription_checkout", "subscription", "invoice", "refund"}


def identity(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 255 or "\x00" in value:
        raise HTTPException(400, "invalid provider identity")
    return value


def integer(value, low=0, high=4102444800):
    if type(value) is not int or not low <= value <= high:
        raise HTTPException(400, "invalid provider number")
    return value


def lookup(database, provider, object_id):
    with transaction(database, UUID(int=0)) as conn:
        conn.execute(
            "SELECT set_config('app.payment_provider',%s,true),set_config('app.payment_object',%s,true)",
            (provider, identity(object_id)),
        )
        return conn.execute(
            "SELECT workspace_id,resource_kind,resource_id FROM payment_bindings WHERE provider=%s AND provider_object_id=%s",
            (provider, object_id),
        ).fetchone()


def handle(database, provider, event, fingerprint):
    identity(event["id"])
    identity(event["provider_object_id"])
    integer(event.get("created"))
    kind = event["kind"]
    binding = lookup(database, provider.name, event["provider_object_id"])
    if kind == "invoice" and binding is None:
        # New invoice IDs route through an already-bound subscription/customer.
        route = event.get("subscription_id") or event.get("customer_id")
        if route:
            binding = lookup(database, provider.name, route)
    expected = {
        "subscription_checkout": {"subscription_checkout"},
        "subscription": {"subscription"},
        "invoice": {"invoice", "subscription", "customer"},
        "refund": {"refund"},
    }
    if binding is None or binding["resource_kind"] not in expected[kind]:
        raise HTTPException(
            409, "provider object not bound; retry after reconciliation"
        )
    with transaction(database, binding["workspace_id"]) as conn:
        if kind == "subscription_checkout":
            duplicate = checkout(conn, provider, event, fingerprint, binding)
        elif kind == "subscription":
            duplicate = subscription(conn, provider, event, fingerprint, binding)
        elif kind == "invoice":
            duplicate = invoice(conn, provider, event, fingerprint, binding)
        else:
            from .refunds import process

            duplicate = process(conn, provider, event, fingerprint, binding)
        from .pending_webhooks import applied

        applied(conn, provider.name, event["id"])
    return {"accepted": True, "duplicate": duplicate}


def checkout(conn, provider, event, fingerprint, binding):
    row = fetch(conn, "billing_checkouts", binding["resource_id"], True)
    if event.get("status") not in {"complete", "expired"}:
        raise HTTPException(400, "unsupported checkout event")
    if event["status"] == "complete":
        if (
            type(event.get("amount")) is not int
            or event["amount"] != row["unit_amount"] * row["quantity"]
            or event.get("currency") != row["currency"]
        ):
            raise HTTPException(
                409, "subscription checkout amount or currency mismatch"
            )
        subscription_id = identity(event.get("subscription_id"))
        customer_id = identity(event.get("customer_id"))
    if not inbox(
        conn,
        provider.name,
        event,
        fingerprint,
        row["workspace_id"],
        "subscription_checkout",
        row["id"],
    ):
        return True
    if event["status"] == "expired":
        if row["status"] != "complete":
            conn.execute(
                "UPDATE billing_checkouts SET status='expired',updated_at=now() WHERE id=%s",
                (row["id"],),
            )
        return False
    sub = conn.execute(
        "INSERT INTO billing_subscriptions(id,workspace_id,checkout_id,provider,provider_object_id,customer_id,plan,price_id,unit_amount,quantity,currency,interval,status) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'incomplete') ON CONFLICT(workspace_id,checkout_id) DO NOTHING RETURNING *",
        (
            row["id"],
            row["workspace_id"],
            row["id"],
            provider.name,
            subscription_id,
            customer_id,
            row["plan"],
            row["price_id"],
            row["unit_amount"],
            row["quantity"],
            row["currency"],
            row["interval"],
        ),
    ).fetchone()
    if sub is None:
        sub = conn.execute(
            "SELECT * FROM billing_subscriptions WHERE checkout_id=%s", (row["id"],)
        ).fetchone()
    if (
        sub["provider_object_id"] != subscription_id
        or sub["customer_id"] != customer_id
    ):
        raise HTTPException(409, "subscription checkout binding changed")
    bind(
        conn,
        provider.name,
        subscription_id,
        row["workspace_id"],
        "subscription",
        sub["id"],
    )
    bind(conn, provider.name, customer_id, row["workspace_id"], "customer", sub["id"])
    conn.execute(
        "UPDATE billing_checkouts SET status='complete',updated_at=now() WHERE id=%s",
        (row["id"],),
    )
    return False


def subscription(conn, provider, event, fingerprint, binding):
    row = fetch(conn, "billing_subscriptions", binding["resource_id"], True)
    expected = {
        "customer_id": row["customer_id"],
        "price_id": row["price_id"],
        "unit_amount": row["unit_amount"],
        "quantity": row["quantity"],
        "currency": row["currency"],
        "interval": row["interval"],
    }
    if (
        any(event.get(k) != v for k, v in expected.items())
        or type(event.get("quantity")) is not int
        or type(event.get("unit_amount")) is not int
    ):
        raise HTTPException(409, "subscription plan or customer binding mismatch")
    if (
        event.get("status")
        not in {
            "incomplete",
            "trialing",
            "active",
            "past_due",
            "unpaid",
            "paused",
            "canceled",
            "incomplete_expired",
        }
        or type(event.get("cancel_at_period_end")) is not bool
    ):
        raise HTTPException(400, "invalid subscription state")
    period = integer(event.get("current_period_end"))
    if not inbox(
        conn,
        provider.name,
        event,
        fingerprint,
        row["workspace_id"],
        "subscription",
        row["id"],
    ):
        return True
    if event["created"] < row["event_created"] or row["status"] in {
        "canceled",
        "incomplete_expired",
    }:
        return False
    if (
        event["created"] == row["event_created"]
        and row["event_id"]
        and event["status"] not in {"canceled", "incomplete_expired"}
    ):
        # Same-second conflicting snapshots are ambiguous. Retry/manual reconcile
        # instead of choosing an arbitrary event-ID ordering.
        same = (
            row["status"] == event["status"]
            and row["cancel_at_period_end"] == event["cancel_at_period_end"]
        )
        if not same:
            raise HTTPException(
                409,
                "subscription event order ambiguous; retry a fresh provider snapshot",
            )
    conn.execute(
        "UPDATE billing_subscriptions SET status=%s,cancel_at_period_end=%s,current_period_end=to_timestamp(%s),event_created=%s,event_id=%s,cancel_status=CASE WHEN %s OR %s IN ('canceled','incomplete_expired') THEN 'confirmed' ELSE cancel_status END,cancel_fence=CASE WHEN %s OR %s IN ('canceled','incomplete_expired') THEN NULL ELSE cancel_fence END,updated_at=now() WHERE id=%s",
        (
            event["status"],
            event["cancel_at_period_end"],
            period,
            event["created"],
            event["id"],
            event["cancel_at_period_end"],
            event["status"],
            event["cancel_at_period_end"],
            event["status"],
            row["id"],
        ),
    )
    return False


def invoice(conn, provider, event, fingerprint, binding):
    if binding["resource_kind"] == "invoice":
        prior = fetch(conn, "billing_invoices", binding["resource_id"])
        sub = fetch(conn, "billing_subscriptions", prior["subscription_id"], True)
    else:
        sub = fetch(conn, "billing_subscriptions", binding["resource_id"], True)
    if (
        event.get("customer_id") != sub["customer_id"]
        or (
            event.get("subscription_id")
            and event["subscription_id"] != sub["provider_object_id"]
        )
        or event.get("currency") != sub["currency"]
    ):
        raise HTTPException(
            409, "invoice subscription/customer/currency binding mismatch"
        )
    due = integer(event.get("amount_due"), 0, 999999999)
    paid = integer(event.get("amount_paid"), 0, 999999999)
    start = integer(event.get("period_start"))
    end = integer(event.get("period_end"))
    if (
        event.get("status") not in {"draft", "open", "paid", "void", "uncollectible"}
        or end < start
        or paid > due
        or (event["status"] == "paid" and paid != due)
    ):
        raise HTTPException(400, "unsupported invoice state")
    row = conn.execute(
        "SELECT * FROM billing_invoices WHERE provider=%s AND provider_object_id=%s FOR UPDATE",
        (provider.name, event["provider_object_id"]),
    ).fetchone()
    identifier = row["id"] if row else uuid4()
    if not inbox(
        conn,
        provider.name,
        event,
        fingerprint,
        sub["workspace_id"],
        "invoice",
        identifier,
    ):
        return True
    if row and (
        event["created"] < row["event_created"]
        or row["status"] in {"paid", "void", "uncollectible"}
    ):
        return False
    if (
        row
        and event["created"] == row["event_created"]
        and row["status"] == "open"
        and event["status"] == "draft"
    ):
        return False
    conn.execute(
        "INSERT INTO billing_invoices(id,workspace_id,subscription_id,provider,provider_object_id,customer_id,amount_due,amount_paid,currency,status,period_start,period_end,event_created,event_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s),to_timestamp(%s),%s,%s) ON CONFLICT(provider,provider_object_id) DO UPDATE SET amount_due=excluded.amount_due,amount_paid=excluded.amount_paid,status=excluded.status,period_start=excluded.period_start,period_end=excluded.period_end,event_created=excluded.event_created,event_id=excluded.event_id,updated_at=now()",
        (
            identifier,
            sub["workspace_id"],
            sub["id"],
            provider.name,
            event["provider_object_id"],
            sub["customer_id"],
            due,
            paid,
            event["currency"],
            event["status"],
            start,
            end,
            event["created"],
            event["id"],
        ),
    )
    bind(
        conn,
        provider.name,
        event["provider_object_id"],
        sub["workspace_id"],
        "invoice",
        identifier,
    )
    return False
