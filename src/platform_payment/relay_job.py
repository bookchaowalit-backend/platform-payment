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

from .live_mode import LiveModeRefused, refuse_live_mode
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
    token = environ.get("HEARTBEAT_TOKEN", "")
    # Same test-mode-only boundary as every CLI command (live_mode.py, D8): a live
    # key anywhere in the environment stops the job before any database or Kafka work.
    try:
        refuse_live_mode(environ)
    except LiveModeRefused:
        print(json.dumps({"event": "relay", "status": "refused", "error": "LiveModeRefused"}))
        _heartbeat(post, token, {"ok": False, "failed": ["live_mode"], "sent": 0})
        return 2
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
    _heartbeat(post, token, {"ok": not failed, "failed": failed, "sent": sent})
    return 1 if failed else 0


def _heartbeat(post: Callable[[str, str, dict[str, Any]], None], token: str, body: dict[str, Any]) -> None:
    if not token:
        return
    try:
        post(HEARTBEAT_URL, token, body)
    except Exception as exc:  # noqa: BLE001 - a missed heartbeat alerts by age
        print(json.dumps({"event": "heartbeat", "status": "error", "error": type(exc).__name__}))


def main() -> None:
    sys.exit(run(os.environ))


if __name__ == "__main__":
    main()
