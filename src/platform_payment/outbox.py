"""ADR-0008 outbox: domain events written with the business row, published to Kafka later.

Copied from architecture-docs/messaging (relay.py), adapted to this cell's RLS: the relay
sets `app.outbox_relay` for its own transaction only. Events carry reference ids and
amounts, never personal data.
"""

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from psycopg.types.json import Jsonb

from .storage import transaction

TOPIC = "commerce.events.v1"
PRODUCER = "payment-api"

TRY_LOCK = "SELECT pg_try_advisory_xact_lock(hashtext('adr0008-payment-outbox-relay')) AS locked"
# A key whose oldest unsent row has failed sends only that row; a failed row waits for its
# backoff, so rows that keep failing cannot fill every batch.
SELECT_UNSENT = (
    "SELECT o.id, o.topic, o.message_key, o.envelope FROM payment_outbox o "
    "WHERE o.sent_at IS NULL "
    "AND (o.next_attempt_at IS NULL OR o.next_attempt_at <= now()) "
    "AND NOT EXISTS ("
    "SELECT 1 FROM payment_outbox f WHERE f.topic = o.topic AND f.message_key = o.message_key "
    "AND f.sent_at IS NULL AND f.attempts > 0 AND f.seq < o.seq) "
    "ORDER BY o.seq LIMIT %s"
)
MARK_SENT = "UPDATE payment_outbox SET sent_at=now(), attempts=attempts+1, last_error=NULL WHERE id=%s"
# Backoff 1, 2, 4 … 64 minutes from the failure itself (clock_timestamp, not the
# transaction start); the exponent is capped before the interval is built.
MARK_FAILED = (
    "UPDATE payment_outbox SET attempts=attempts+1, last_error=%s, "
    "next_attempt_at=clock_timestamp() + interval '1 minute' * power(2, least(attempts, 6)) WHERE id=%s"
)
PUBLISH_TIMEOUT_SECONDS = 15.0
# The pool closes connections idle in a transaction after 10 s; each publish may wait
# PUBLISH_TIMEOUT_SECONDS, so the relay transaction allows more.
RELAX_IDLE_TIMEOUT = "SELECT set_config('idle_in_transaction_session_timeout','60s',true)"


def payment_succeeded(conn, *, workspace_id: UUID, intent_id: UUID, amount: int, currency: str, provider: str) -> None:
    """Record `payment.intent.succeeded` in the caller's settlement transaction (once per intent)."""
    event_id = f"payment.intent.succeeded:{intent_id}"
    envelope = {
        "id": event_id,
        "type": "payment.intent.succeeded",
        "version": 1,
        "occurred_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "producer": PRODUCER,
        "correlation_id": str(intent_id),
        "causation_id": None,
        "data": {
            "workspace_id": str(workspace_id),
            "intent_id": str(intent_id),
            "amount": amount,
            "currency": currency,
            "provider": provider,
        },
    }
    conn.execute(
        "INSERT INTO payment_outbox(id,workspace_id,topic,message_key,envelope) VALUES(%s,%s,%s,%s,%s)"
        " ON CONFLICT (id) DO NOTHING",
        (event_id, workspace_id, TOPIC, str(intent_id), Jsonb(envelope)),
    )


def refund_succeeded(conn, *, workspace_id: UUID, intent_id: UUID, refund_id: UUID, amount: int, currency: str) -> None:
    """Record `payment.refund.succeeded` in the refund's settlement transaction (once per refund).

    The message key is the intent, like its payment, so the broker keeps the refund after its sale.
    """
    event_id = f"payment.refund.succeeded:{refund_id}"
    envelope = {
        "id": event_id,
        "type": "payment.refund.succeeded",
        "version": 1,
        "occurred_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "producer": PRODUCER,
        "correlation_id": str(intent_id),
        "causation_id": f"payment.intent.succeeded:{intent_id}",
        "data": {
            "workspace_id": str(workspace_id),
            "intent_id": str(intent_id),
            "refund_id": str(refund_id),
            "amount": amount,
            "currency": currency,
        },
    }
    conn.execute(
        "INSERT INTO payment_outbox(id,workspace_id,topic,message_key,envelope) VALUES(%s,%s,%s,%s,%s)"
        " ON CONFLICT (id) DO NOTHING",
        (event_id, workspace_id, TOPIC, str(intent_id), Jsonb(envelope)),
    )


class Publisher(Protocol):
    def publish(self, topic: str, key: str, value: dict[str, Any]) -> None:
        """Return once the broker acknowledged the message; raise otherwise."""


@dataclass
class RelayResult:
    sent: int
    failed: int
    skipped: bool = False


def relay_once(database, publisher: Publisher, batch_size: int = 100) -> RelayResult:
    sent = failed = 0
    with transaction(database, UUID(int=0)) as conn:
        conn.execute("SELECT set_config('app.outbox_relay','on',true)")
        conn.execute(RELAX_IDLE_TIMEOUT)
        if not conn.execute(TRY_LOCK).fetchone()["locked"]:
            return RelayResult(0, 0, skipped=True)
        rows = conn.execute(SELECT_UNSENT, (batch_size,)).fetchall()
        blocked: set[tuple[str, str]] = set()
        for row in rows:
            key = (row["topic"], row["message_key"])
            if key in blocked:
                continue  # an earlier event for this key failed in this run; keep the order
            try:
                publisher.publish(row["topic"], row["message_key"], row["envelope"])
            except Exception as exc:  # noqa: BLE001 - recorded on the row (type only), retried after backoff
                conn.execute(MARK_FAILED, (type(exc).__name__, row["id"]))
                blocked.add(key)
                failed += 1
                continue
            conn.execute(MARK_SENT, (row["id"],))
            sent += 1
    return RelayResult(sent, failed)


class KafkaPublisher:
    """confluent-kafka producer (kafka-python-ng cannot authenticate to Kafka 4.x brokers)."""

    def __init__(self, config: dict[str, str], timeout: float = PUBLISH_TIMEOUT_SECONDS):
        from confluent_kafka import Producer

        self.timeout = timeout
        self.producer = Producer({**config, "acks": "all", "enable.idempotence": True})

    def publish(self, topic, key, value):
        import json

        result: dict[str, Any] = {}
        self.producer.produce(
            topic,
            key=key.encode(),
            value=json.dumps(value, separators=(",", ":")).encode(),
            on_delivery=lambda err, _msg: result.setdefault("err", err),
        )
        deadline = time.monotonic() + self.timeout
        while "err" not in result and time.monotonic() < deadline:
            self.producer.poll(0.2)
        if "err" not in result:
            raise TimeoutError("broker did not acknowledge")
        if result["err"] is not None:
            raise RuntimeError(result["err"].name())


def kafka_config_from_env(environ) -> dict[str, str]:
    """SASL_SSL settings from PAYMENT_KAFKA_* variables (Infisical /aiven: payment-producer)."""
    import tempfile

    ca = tempfile.NamedTemporaryFile("w", delete=False, suffix=".pem")
    ca.write(environ["PAYMENT_KAFKA_CA_PEM"])
    ca.close()
    return {
        "bootstrap.servers": environ["PAYMENT_KAFKA_BOOTSTRAP"],
        "security.protocol": "SASL_SSL",
        "sasl.mechanism": environ.get("PAYMENT_KAFKA_SASL_MECHANISM", "SCRAM-SHA-256"),
        "sasl.username": environ["PAYMENT_KAFKA_USERNAME"],
        "sasl.password": environ["PAYMENT_KAFKA_PASSWORD"],
        "ssl.ca.location": ca.name,
    }
