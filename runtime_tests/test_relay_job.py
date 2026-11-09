"""Hourly outbox relay job: one relay pass, then a counts-only heartbeat (ADR-0008 M3)."""

from __future__ import annotations

import json

import pytest

from platform_payment import relay_job
from platform_payment.outbox import RelayResult

ENV = {
    "DATABASE_URL": "postgresql://relay@example.invalid/db",
    "HEARTBEAT_TOKEN": "MARKER_HEARTBEAT",
    "PAYMENT_KAFKA_BOOTSTRAP": "b", "PAYMENT_KAFKA_USERNAME": "u",
    "PAYMENT_KAFKA_PASSWORD": "p", "PAYMENT_KAFKA_CA_PEM": "c",
}


def _run(result=None, error=None, env=ENV):
    posts = []

    def relay(environ):
        if error:
            raise error
        return result

    code = relay_job.run(env, relay=relay, post=lambda url, token, body: posts.append((url, token, body)))
    return code, posts


def test_sent_rows_report_ok():
    code, posts = _run(RelayResult(sent=2, failed=0))
    assert code == 0
    assert posts == [(relay_job.HEARTBEAT_URL, "MARKER_HEARTBEAT", {"ok": True, "failed": [], "sent": 2})]


def test_failed_publishes_report_not_ok_and_exit_1():
    code, posts = _run(RelayResult(sent=1, failed=3))
    assert code == 1
    assert posts[0][2] == {"ok": False, "failed": ["publish"], "sent": 1}


def test_a_crash_reports_the_error_class_only():
    code, posts = _run(error=RuntimeError("password=hunter2 at broker"))
    assert code == 1
    assert posts[0][2] == {"ok": False, "failed": ["relay"], "sent": 0}
    assert "hunter2" not in json.dumps(posts)


def test_skipped_run_is_ok():
    code, posts = _run(RelayResult(sent=0, failed=0, skipped=True))
    assert code == 0 and posts[0][2]["ok"] is True


def test_no_heartbeat_token_still_relays_and_exits_by_result():
    env = {k: v for k, v in ENV.items() if k != "HEARTBEAT_TOKEN"}
    code, posts = _run(RelayResult(sent=0, failed=0), env=env)
    assert code == 0 and posts == []


def test_missing_database_url_fails_closed():
    env = {k: v for k, v in ENV.items() if k != "DATABASE_URL"}
    with pytest.raises(SystemExit):
        relay_job.run(env, relay=lambda e: RelayResult(0, 0), post=lambda *a: None)


def test_a_live_stripe_key_refuses_before_any_relay_work():
    calls = []
    env = {**ENV, "PAYMENT_STRIPE_API_KEY": "sk_live_" + "MARKER"}
    posts = []
    def relay(environ):
        calls.append(environ)
        return RelayResult(0, 0)

    code = relay_job.run(
        env,
        relay=relay,
        post=lambda url, token, body: posts.append(body),
    )
    assert code == 2
    assert calls == []
    assert posts == [{"ok": False, "failed": ["live_mode"], "sent": 0}]
    assert "MARKER" not in json.dumps(posts)
