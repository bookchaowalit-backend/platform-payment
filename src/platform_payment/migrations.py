from platform_backend_core import Migration

MIGRATIONS = [
    Migration(
        "001-payments",
        """
CREATE TABLE payment_intents (
 id uuid PRIMARY KEY, workspace_id uuid NOT NULL, actor_id uuid NOT NULL,
 operation text NOT NULL CHECK (operation IN ('intent','checkout')),
 idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 128),
 fingerprint text NOT NULL, amount bigint NOT NULL CHECK (amount BETWEEN 1 AND 999999999),
 currency text NOT NULL CHECK (currency ~ '^[a-z]{3}$'), provider text NOT NULL,
 provider_key text NOT NULL UNIQUE, provider_object_id text, checkout_url text,
 status text NOT NULL CHECK (status IN ('creating','unknown','pending','failed','succeeded')),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 lease_until timestamptz, fence uuid, UNIQUE(workspace_id,operation,idempotency_key),
 UNIQUE(workspace_id,id)
);
CREATE INDEX payment_workspace_created ON payment_intents(workspace_id,created_at DESC,id);
CREATE TABLE payment_bindings (
 provider text NOT NULL, provider_object_id text NOT NULL, workspace_id uuid NOT NULL,
 intent_id uuid NOT NULL, PRIMARY KEY(provider,provider_object_id),
 FOREIGN KEY(workspace_id,intent_id) REFERENCES payment_intents(workspace_id,id)
);
CREATE INDEX payment_binding_workspace ON payment_bindings(workspace_id,intent_id);
CREATE TABLE payment_webhook_inbox (
 provider text NOT NULL, event_id text NOT NULL, workspace_id uuid NOT NULL,
 intent_id uuid NOT NULL, fingerprint text NOT NULL, received_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(provider,event_id), FOREIGN KEY(workspace_id,intent_id) REFERENCES payment_intents(workspace_id,id)
);
CREATE INDEX payment_inbox_workspace ON payment_webhook_inbox(workspace_id,intent_id);
CREATE TABLE payment_effects (
 workspace_id uuid NOT NULL, intent_id uuid NOT NULL, kind text NOT NULL CHECK(kind='payment_succeeded'),
 amount bigint NOT NULL, currency text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(workspace_id,intent_id,kind),
 FOREIGN KEY(workspace_id,intent_id) REFERENCES payment_intents(workspace_id,id)
);
CREATE FUNCTION payment_effect_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'payment effects are immutable'; END $$;
CREATE TRIGGER payment_effect_no_change BEFORE UPDATE OR DELETE ON payment_effects
 FOR EACH ROW EXECUTE FUNCTION payment_effect_immutable();
ALTER TABLE payment_intents ENABLE ROW LEVEL SECURITY;
ALTER TABLE payment_intents FORCE ROW LEVEL SECURITY;
CREATE POLICY payment_intent_workspace ON payment_intents
 USING(workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid)
 WITH CHECK(workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid);
ALTER TABLE payment_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE payment_bindings FORCE ROW LEVEL SECURITY;
CREATE POLICY payment_binding_workspace ON payment_bindings
 USING(workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid)
 WITH CHECK(workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid);
-- Only the authenticated webhook code sets these transaction-local lookup values.
-- This permits routing by an exact verified provider ID, never webhook metadata.
CREATE POLICY payment_binding_route ON payment_bindings FOR SELECT
 USING(provider=current_setting('app.payment_provider',true)
 AND provider_object_id=current_setting('app.payment_object',true));
ALTER TABLE payment_webhook_inbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE payment_webhook_inbox FORCE ROW LEVEL SECURITY;
CREATE POLICY payment_inbox_workspace ON payment_webhook_inbox
 USING(workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid)
 WITH CHECK(workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid);
ALTER TABLE payment_effects ENABLE ROW LEVEL SECURITY;
ALTER TABLE payment_effects FORCE ROW LEVEL SECURITY;
CREATE POLICY payment_effect_workspace ON payment_effects
 USING(workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid)
 WITH CHECK(workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid);
""",
    )
]

from .billing_migrations import BILLING_MIGRATION

MIGRATIONS.append(BILLING_MIGRATION)
