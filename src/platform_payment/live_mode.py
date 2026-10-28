"""Test-mode-only guard: this runtime must never run against live Stripe.

Live activation, and any move of webhook ownership away from
bookchaowalit/platform-webhooks-worker, needs a separate owner decision (D8).
Until then any live secret or restricted key in the environment, or a provider
that reports live mode, refuses startup, the CLI commands and readiness.
Errors name the offending variables, never their values.
"""

import os
from collections.abc import Mapping

LIVE_KEY_PREFIXES = ("sk_live_", "rk_live_")


class LiveModeRefused(ValueError):
    """Raised when a live Stripe credential or live provider is configured."""


def live_key_names(environ: Mapping[str, str]) -> list[str]:
    """Names of variables holding a live Stripe key (values are never returned)."""

    return sorted(
        name
        for name, value in environ.items()
        if isinstance(value, str) and value.strip().startswith(LIVE_KEY_PREFIXES)
    )


def refuse_live_mode(environ: Mapping[str, str] | None = None, provider=None) -> None:
    names = live_key_names(os.environ if environ is None else environ)
    if names:
        raise LiveModeRefused(
            "live Stripe mode refused: this runtime is test-mode only; "
            "remove the live key from " + ", ".join(names)
        )
    if provider is not None and getattr(provider, "livemode", False) is not False:
        raise LiveModeRefused(
            "live Stripe mode refused: the configured provider reports live mode"
        )
