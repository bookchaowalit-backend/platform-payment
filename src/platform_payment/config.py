import os

from .adapters import StripeProvider
from .live_mode import refuse_live_mode


def configured_provider():
    names = (
        "PAYMENT_STRIPE_API_KEY",
        "PAYMENT_STRIPE_WEBHOOK_SECRET",
        "PAYMENT_SUCCESS_URL",
        "PAYMENT_CANCEL_URL",
    )
    # Refuse before the completeness check: a live key never means "disabled".
    refuse_live_mode()
    if not all(os.environ.get(n) for n in names):
        return None
    return StripeProvider(
        api_key=os.environ[names[0]],
        webhook_secret=os.environ[names[1]],
        success_url=os.environ[names[2]],
        cancel_url=os.environ[names[3]],
    )
