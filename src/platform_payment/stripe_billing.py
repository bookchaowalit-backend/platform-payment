"""Sandbox Stripe billing/refund methods; all destinations/prices are server-owned."""


class StripeBillingMethods:
    def create_subscription_checkout(
        self, *, price_id, quantity, reference, idempotency_key
    ):
        obj = self.client.v1.checkout.sessions.create(
            params={
                "mode": "subscription",
                "line_items": [{"price": price_id, "quantity": quantity}],
                "client_reference_id": reference,
                "success_url": self.success_url,
                "cancel_url": self.cancel_url,
                "integration_identifier": "platform_billing_kqzmpvtn",
            },
            options={"idempotency_key": idempotency_key},
        )
        return {"id": obj.id, "checkout_url": obj.url}

    def cancel_subscription(self, *, subscription_id, idempotency_key):
        obj = self.client.v1.subscriptions.update(
            subscription_id,
            params={"cancel_at_period_end": True},
            options={"idempotency_key": idempotency_key},
        )
        return {"id": obj.id}

    def create_refund(
        self, *, payment_id, amount, currency, reference, idempotency_key
    ):
        # Checkout Sessions use their stored provider binding to resolve the charge.
        if payment_id.startswith("cs_"):
            session = self.client.v1.checkout.sessions.retrieve(payment_id)
            if (
                session.mode != "payment"
                or session.payment_status != "paid"
                or session.currency != currency
                or not session.payment_intent
            ):
                raise ValueError("checkout is not a paid matching payment")
            payment_id = session.payment_intent
        if not payment_id.startswith("pi_"):
            raise ValueError("unsupported payment binding")
        obj = self.client.v1.refunds.create(
            params={
                "payment_intent": payment_id,
                "amount": amount,
                "metadata": {"platform_reference": reference},
            },
            options={"idempotency_key": idempotency_key},
        )
        return {"id": obj.id, "payment_id": payment_id}
