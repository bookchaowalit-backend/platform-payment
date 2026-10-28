"""Synthetic Stripe-shaped fixture keys, assembled at runtime.

No literal in this repository is shaped like a provider credential, so secret
scanners never see one. The parts are not secrets: they spell out "fixture".
"""


def fixture_key(kind: str = "rk", mode: str = "test") -> str:
    return f"{kind}_{mode}_synthetic_fixture"
