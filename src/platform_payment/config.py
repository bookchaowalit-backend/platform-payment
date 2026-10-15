import os
from .adapters import StripeProvider


def configured_provider():
    names = (
        "PAYMENT_STRIPE_API_KEY",
        "PAYMENT_STRIPE_WEBHOOK_SECRET",
        "PAYMENT_SUCCESS_URL",
        "PAYMENT_CANCEL_URL",
    )
    if not all(os.environ.get(n) for n in names):
        return None
    return StripeProvider(
        api_key=os.environ[names[0]],
        webhook_secret=os.environ[names[1]],
        success_url=os.environ[names[2]],
        cancel_url=os.environ[names[3]],
    )
