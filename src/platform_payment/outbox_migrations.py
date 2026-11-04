from platform_backend_core import Migration

# ADR-0008 transactional outbox: a settled payment writes its domain event in the same
# transaction; `relay-outbox` publishes it to Kafka later. Additive: older images ignore it.
OUTBOX_MIGRATION = Migration(
    "004-outbox",
    """
CREATE TABLE payment_outbox (
 id text PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 200),
 seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
 workspace_id uuid NOT NULL,
 topic text NOT NULL CHECK (topic ~ '^[a-z0-9_.-]{1,120}$'),
 message_key text NOT NULL CHECK (length(message_key) BETWEEN 1 AND 200),
 envelope jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 attempts integer NOT NULL DEFAULT 0,
 last_error text,
 next_attempt_at timestamptz,
 sent_at timestamptz
);
CREATE INDEX payment_outbox_unsent ON payment_outbox(seq) WHERE sent_at IS NULL;
CREATE INDEX payment_outbox_unsent_key ON payment_outbox(topic,message_key,seq) WHERE sent_at IS NULL;
ALTER TABLE payment_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE payment_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY payment_outbox_workspace ON payment_outbox
 USING(workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid)
 WITH CHECK(workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid);
-- Only the relay command sets this transaction-local value; it reads and marks rows of
-- every workspace but never inserts (inserts still need the workspace policy).
CREATE POLICY payment_outbox_relay_read ON payment_outbox FOR SELECT
 USING(current_setting('app.outbox_relay',true)='on');
CREATE POLICY payment_outbox_relay_mark ON payment_outbox FOR UPDATE
 USING(current_setting('app.outbox_relay',true)='on')
 WITH CHECK(current_setting('app.outbox_relay',true)='on');
""",
)
