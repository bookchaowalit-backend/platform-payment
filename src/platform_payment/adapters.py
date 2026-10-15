"""Provider-neutral boundary; provider creation is always idempotent."""

from typing import Protocol
from urllib.parse import urlsplit


class PaymentProvider(Protocol):
    name: str

    def create(
        self,
        *,
        amount: int,
        currency: str,
        reference: str,
        idempotency_key: str,
        checkout: bool,
    ) -> dict: ...
    def verify_event(self, payload: bytes, signature: str | None) -> dict: ...


class StripeProvider:
    name = "stripe"

    def __init__(
        self, *, api_key: str, webhook_secret: str, success_url: str, cancel_url: str
    ):
        # Live mode deliberately has no switch in this staged integration.
        if not api_key.startswith(("rk_test_", "sk_test_")) or not webhook_secret:
            raise ValueError(
                "explicit sandbox Stripe and webhook configuration required"
            )
        if any(urlsplit(url).scheme != "https" for url in (success_url, cancel_url)):
            raise ValueError("server-owned HTTPS return URLs required")
        import stripe

        self.client = stripe.StripeClient(
            api_key,
            stripe_version="2026-08-26.dahlia",
            max_network_retries=1,
            http_client=stripe.HTTPXClient(timeout=5, allow_sync_methods=True),
        )
        self.webhook_secret = webhook_secret
        self.success_url = success_url
        self.cancel_url = cancel_url

    def create(self, *, amount, currency, reference, idempotency_key, checkout):
        options = {"idempotency_key": idempotency_key}
        if checkout:
            obj = self.client.v1.checkout.sessions.create(
                params={
                    "mode": "payment",
                    "line_items": [
                        {
                            "price_data": {
                                "currency": currency,
                                "unit_amount": amount,
                                "product_data": {"name": "Platform payment"},
                            },
                            "quantity": 1,
                        }
                    ],
                    "client_reference_id": reference,
                    "success_url": self.success_url,
                    "cancel_url": self.cancel_url,
                    "integration_identifier": "platform_checkout_kqzmpvtn",
                },
                options=options,
            )
            return {"id": obj.id, "status": "pending", "checkout_url": obj.url}
        obj = self.client.v1.payment_intents.create(
            params={
                "amount": amount,
                "currency": currency,
                "metadata": {"platform_reference": reference},
            },
            options=options,
        )
        return {"id": obj.id, "status": "pending", "checkout_url": None}

    def verify_event(self, payload, signature):
        import stripe

        event = stripe.Webhook.construct_event(
            payload, signature, self.webhook_secret, tolerance=300
        )
        if event.get("livemode") is not False:
            raise ValueError("sandbox events required")
        kind = event["type"]
        obj = event["data"]["object"]
        if kind.startswith("payment_intent."):
            statuses = {
                "payment_intent.succeeded": "succeeded",
                "payment_intent.payment_failed": "failed",
                "payment_intent.canceled": "failed",
                "payment_intent.processing": "pending",
            }
            if kind not in statuses:
                return {"ignore": True}
            amount = obj["amount"]
            if statuses[kind] == "succeeded" and obj.get("amount_received") != amount:
                raise ValueError("partial payments unsupported")
            status = statuses[kind]
        elif kind in {
            "checkout.session.completed",
            "checkout.session.async_payment_succeeded",
            "checkout.session.async_payment_failed",
            "checkout.session.expired",
        }:
            status = (
                "succeeded"
                if obj.get("payment_status") == "paid"
                else (
                    "failed"
                    if kind
                    in {
                        "checkout.session.async_payment_failed",
                        "checkout.session.expired",
                    }
                    else "pending"
                )
            )
            amount = obj["amount_total"]
        else:
            return {"ignore": True}
        return {
            "id": event["id"],
            "provider_object_id": obj["id"],
            "status": status,
            "amount": amount,
            "currency": obj["currency"],
        }
