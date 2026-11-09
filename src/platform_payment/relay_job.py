"""Hourly outbox relay job (Railway cron `payment-relay`, ADR-0008 M3).

One `relay-outbox` pass, then a heartbeat to the uptime Worker (`payment-relay`).
Output and heartbeat carry counts and a failure label only: never configuration,
event values or exception text (exception messages can hold connection details).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from typing import Any

from .outbox import RelayResult

HEARTBEAT_URL = "https://monitor.bookchaowalit.com/heartbeat/payment-relay"


def _relay(environ: Mapping[str, str]) -> RelayResult:
    from platform_backend_core import Database

    from .outbox import KafkaPublisher, kafka_config_from_env, relay_once

    publisher = KafkaPublisher(kafka_config_from_env(environ))
    database = Database(environ["DATABASE_URL"], max_size=2)
    try:
        return relay_once(database, publisher)
    finally:
        database.close()


def _post(url: str, token: str, body: dict[str, Any]) -> None:
    import httpx

    httpx.post(
        url,
        json=body,
        headers={"Authorization": f"Bearer {token}", "User-Agent": "payment-relay/1"},
        timeout=15,
    ).raise_for_status()


def run(
    environ: Mapping[str, str],
    *,
    relay: Callable[[Mapping[str, str]], RelayResult] = _relay,
    post: Callable[[str, str, dict[str, Any]], None] = _post,
) -> int:
    if not environ.get("DATABASE_URL"):
        raise SystemExit("DATABASE_URL required")
    failed: list[str] = []
    sent = 0
    try:
        result = relay(environ)
    except Exception as exc:  # noqa: BLE001 - reported as a class only
        failed.append("relay")
        print(json.dumps({"event": "relay", "status": "error", "error": type(exc).__name__}))
    else:
        sent = result.sent
        if result.failed:
            failed.append("publish")
        print(json.dumps({"event": "relay", "sent": result.sent, "failed": result.failed, "skipped": result.skipped}))
    token = environ.get("HEARTBEAT_TOKEN", "")
    if token:
        try:
            post(HEARTBEAT_URL, token, {"ok": not failed, "failed": failed, "sent": sent})
        except Exception as exc:  # noqa: BLE001 - a missed heartbeat alerts by age
            print(json.dumps({"event": "heartbeat", "status": "error", "error": type(exc).__name__}))
    return 1 if failed else 0


def main() -> None:
    sys.exit(run(os.environ))


if __name__ == "__main__":
    main()
