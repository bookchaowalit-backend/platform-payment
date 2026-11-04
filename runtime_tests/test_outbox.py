"""ADR-0008 outbox: a settled payment writes one domain event in the same transaction,
tenant reads stay isolated, and the relay publishes in order with backoff on failure."""

from uuid import UUID, uuid4

import pytest

from platform_payment.outbox import TOPIC, relay_once

from .billing_support import OWN, event, paid


class FakePublisher:
    def __init__(self, fail=()):
        self.fail = set(fail)
        self.sent = []

    def publish(self, topic, key, value):
        if value["data"]["intent_id"] in self.fail:
            raise TimeoutError("broker down")
        self.sent.append((topic, key, value["id"]))


NO_WORKSPACE = UUID(int=0)


class SlowPublisher(FakePublisher):
    def __init__(self, seconds):
        super().__init__()
        self.seconds = seconds

    def publish(self, topic, key, value):
        import time

        time.sleep(self.seconds)
        super().publish(topic, key, value)


def outbox(db, ws=NO_WORKSPACE, relay=False):
    with db.transaction(ws) as conn:
        if relay:
            conn.execute("SELECT set_config('app.outbox_relay','on',true)")
        return conn.execute(
            "SELECT id,workspace_id,topic,message_key,envelope,attempts,sent_at,next_attempt_at"
            " FROM payment_outbox ORDER BY seq"
        ).fetchall()


def test_a_settled_payment_writes_one_event_without_personal_data(billing):
    c, db, ws, _p, _ = billing
    item = paid(c)
    rows = outbox(db, ws)
    assert len(rows) == 1
    row_id, workspace, topic, key, envelope, attempts, sent_at, _ = rows[0]
    assert (workspace, topic, key, attempts, sent_at) == (ws, TOPIC, item["id"], 0, None)
    assert envelope["type"] == "payment.intent.succeeded"
    assert envelope["id"] == row_id == f"payment.intent.succeeded:{item['id']}"
    assert envelope["data"] == {
        "workspace_id": str(ws), "intent_id": item["id"], "amount": 1500, "currency": "usd", "provider": "fixture",
    }
    assert set(envelope) == {"id", "type", "version", "occurred_at", "producer", "correlation_id", "causation_id", "data"}
    # A redelivered webhook settles nothing new and writes no second event.
    assert event(c, item["provider_object_id"]).json()["duplicate"] is True
    assert len(outbox(db, ws)) == 1


def test_tenants_cannot_read_each_others_events_and_need_the_relay_flag(billing):
    c, db, ws, _p, _ = billing
    paid(c)
    assert outbox(db, uuid4()) == []
    assert outbox(db) == []  # no workspace and no relay flag: nothing visible
    assert len(outbox(db, relay=True)) == 1


def test_relay_flag_cannot_insert_events_for_another_workspace(billing):
    _c, db, _ws, _p, _ = billing
    with pytest.raises(Exception, match="row-level security"):
        with db.transaction(NO_WORKSPACE) as conn:
            conn.execute("SELECT set_config('app.outbox_relay','on',true)")
            conn.execute(
                "INSERT INTO payment_outbox(id,workspace_id,topic,message_key,envelope) VALUES('x',%s,'t','k','{}')",
                (uuid4(),),
            )


def test_backoff_starts_at_the_failure_not_the_transaction_start():
    from platform_payment.outbox import MARK_FAILED, RELAX_IDLE_TIMEOUT

    assert "clock_timestamp()" in MARK_FAILED and "now()" not in MARK_FAILED
    assert "idle_in_transaction_session_timeout" in RELAX_IDLE_TIMEOUT


def test_relay_publishes_marks_sent_and_backs_off_a_failure(billing):
    c, db, ws, _p, _ = billing
    first = paid(c)
    second_body = {"amount": 2500, "currency": "usd"}
    second = c.post("/v1/payment-intents", json=second_body, headers={**OWN, "Idempotency-Key": "second"}).json()
    assert event(c, second["provider_object_id"], amount=2500).status_code == 200
    publisher = FakePublisher(fail={first["id"]})
    result = relay_once(db, publisher)
    assert (result.sent, result.failed) == (1, 1)
    assert publisher.sent == [(TOPIC, second["id"], f"payment.intent.succeeded:{second['id']}")]
    rows = {r[3]: r for r in outbox(db, relay=True)}
    assert rows[second["id"]][6] is not None  # sent_at
    assert rows[first["id"]][5] == 1 and rows[first["id"]][6] is None and rows[first["id"]][7] is not None
    # The failed row waits for its backoff: an immediate rerun sends nothing.
    again = relay_once(db, FakePublisher())
    assert (again.sent, again.failed) == (0, 0)
    with db.transaction(ws) as conn:
        conn.execute("UPDATE payment_outbox SET next_attempt_at=now()-interval '1 second' WHERE message_key=%s", (first["id"],))
    late = relay_once(db, SlowPublisher(11))
    assert (late.sent, late.failed) == (1, 0), "a publish slower than the pool's 10 s idle timeout still commits"
    assert all(r[6] is not None for r in outbox(db, relay=True))



class FakeProducer:
    """Stands in for confluent_kafka.Producer: records produce() and reports delivery on poll()."""

    instances: list = []

    def __init__(self, config):
        self.config = config
        self.produced = []
        self.error = None
        self.silent = False
        self._pending = []
        FakeProducer.instances.append(self)

    def produce(self, topic, key, value, on_delivery):
        self.produced.append((topic, key, value))
        self._pending.append(on_delivery)

    def poll(self, _timeout):
        if self.silent:
            return 0
        while self._pending:
            self._pending.pop(0)(self.error, None)
        return 0


class FakeError:
    def name(self):
        return "TOPIC_AUTHORIZATION_FAILED"


@pytest.fixture
def fake_kafka(monkeypatch):
    import sys
    import types

    FakeProducer.instances.clear()
    monkeypatch.setitem(sys.modules, "confluent_kafka", types.SimpleNamespace(Producer=FakeProducer))
    return FakeProducer


KAFKA_ENV = {
    "PAYMENT_KAFKA_BOOTSTRAP": "broker.example:23922",
    "PAYMENT_KAFKA_USERNAME": "payment-producer",
    "PAYMENT_KAFKA_PASSWORD": "fixture-value",
    "PAYMENT_KAFKA_CA_PEM": "-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n",
}


def test_kafka_config_uses_sasl_ssl_and_writes_the_ca_to_a_file():
    import pathlib

    from platform_payment.outbox import kafka_config_from_env

    config = kafka_config_from_env(KAFKA_ENV)
    assert config["security.protocol"] == "SASL_SSL"
    assert config["sasl.mechanism"] == "SCRAM-SHA-256"
    assert config["sasl.username"] == "payment-producer"
    assert pathlib.Path(config["ssl.ca.location"]).read_text() == KAFKA_ENV["PAYMENT_KAFKA_CA_PEM"]


def test_publisher_waits_for_the_ack_and_raises_on_errors_and_timeouts(fake_kafka):
    from platform_payment.outbox import KafkaPublisher

    publisher = KafkaPublisher({"bootstrap.servers": "x"}, timeout=0.5)
    producer = fake_kafka.instances[0]
    assert producer.config["acks"] == "all" and producer.config["enable.idempotence"] is True
    publisher.publish(TOPIC, "intent-1", {"id": "e1"})
    assert producer.produced == [(TOPIC, b"intent-1", b'{"id":"e1"}')]
    producer.error = FakeError()
    with pytest.raises(RuntimeError, match="TOPIC_AUTHORIZATION_FAILED"):
        publisher.publish(TOPIC, "intent-1", {"id": "e2"})
    producer.error, producer.silent = None, True
    with pytest.raises(TimeoutError):
        publisher.publish(TOPIC, "intent-1", {"id": "e3"})


def test_relay_outbox_command_publishes_and_prints_counts(billing, fake_kafka, monkeypatch, capsys):
    import sys

    from platform_payment import __main__ as cli

    c, _db, _ws, _p, runtime_dsn = billing
    paid(c)
    for key, value in {**KAFKA_ENV, "DATABASE_URL": runtime_dsn}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["platform-payment", "relay-outbox", "--limit", "10"])
    cli.main()
    assert '"sent": 1' in capsys.readouterr().out
    assert fake_kafka.instances[0].produced[0][0] == TOPIC


def test_relay_outbox_command_fails_without_leaking_configuration(fake_kafka, monkeypatch, capsys):
    import sys

    from platform_payment import __main__ as cli

    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture-user:fixture-secret@127.0.0.1:1/none")
    for key in KAFKA_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(sys, "argv", ["platform-payment", "relay-outbox"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert "outbox relay failed" in err and "fixture-secret" not in err
