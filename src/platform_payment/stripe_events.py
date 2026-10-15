"""Normalize only supported signed sandbox billing snapshots; metadata is never authority."""


def normalize(event):
    kind = event["type"]
    obj = event["data"]["object"]
    base = {
        "id": event["id"],
        "provider_object_id": obj["id"],
        "created": event.get("created"),
    }
    if obj.get("mode") == "subscription" and kind in {
        "checkout.session.completed",
        "checkout.session.expired",
        "checkout.session.async_payment_succeeded",
        "checkout.session.async_payment_failed",
    }:
        return {
            **base,
            "kind": "subscription_checkout",
            "status": (
                "expired"
                if kind
                in {"checkout.session.expired", "checkout.session.async_payment_failed"}
                else "complete"
            ),
            "subscription_id": obj.get("subscription"),
            "customer_id": obj.get("customer"),
            "amount": obj.get("amount_total"),
            "currency": obj.get("currency"),
        }
    if kind in {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }:
        items = obj.get("items", {}).get("data", [])
        if len(items) != 1:
            raise ValueError("one configured subscription item required")
        item = items[0]
        price = item["price"]
        recurring = price.get("recurring", {})
        if recurring.get("interval_count", 1) != 1:
            raise ValueError("unsupported recurring interval")
        return {
            **base,
            "kind": "subscription",
            "status": obj["status"],
            "customer_id": obj.get("customer"),
            "price_id": price["id"],
            "unit_amount": price.get("unit_amount"),
            "currency": price.get("currency"),
            "quantity": item.get("quantity"),
            "interval": recurring.get("interval"),
            "cancel_at_period_end": obj.get("cancel_at_period_end"),
            "current_period_end": item.get(
                "current_period_end", obj.get("current_period_end")
            ),
        }
    if kind in {
        "invoice.created",
        "invoice.finalized",
        "invoice.updated",
        "invoice.paid",
        "invoice.payment_succeeded",
        "invoice.payment_failed",
        "invoice.voided",
        "invoice.marked_uncollectible",
    }:
        sub = obj.get("subscription") or (obj.get("parent") or {}).get(
            "subscription_details", {}
        ).get("subscription")
        return {
            **base,
            "kind": "invoice",
            "subscription_id": sub,
            "customer_id": obj.get("customer"),
            "amount_due": obj.get("amount_due"),
            "amount_paid": obj.get("amount_paid"),
            "currency": obj.get("currency"),
            "status": obj.get("status"),
            "period_start": obj.get("period_start"),
            "period_end": obj.get("period_end"),
        }
    if kind in {"refund.created", "refund.updated", "refund.failed"}:
        status = obj.get("status")
        return {
            **base,
            "kind": "refund",
            "status": (
                "failed"
                if status in {"failed", "canceled"}
                else ("succeeded" if status == "succeeded" else "pending")
            ),
            "amount": obj.get("amount"),
            "currency": obj.get("currency"),
            "payment_id": obj.get("payment_intent"),
        }
    return None
