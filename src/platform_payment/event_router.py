from .billing_events import KINDS
from .billing_events import handle as billing
from .payment_events import handle as payment
from .pending_webhooks import stage


def apply_verified(database, provider, event, fingerprint):
    stage(database, provider.name, event, fingerprint)
    if event.get("kind") in KINDS:
        return billing(database, provider, event, fingerprint)
    return payment(database, provider, event, fingerprint)
