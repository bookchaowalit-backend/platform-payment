from platform_backend_core import Migration

BILLING_MIGRATION = Migration(
    "002-billing-refunds",
    """
ALTER TABLE payment_intents ADD COLUMN refunded_minor bigint NOT NULL DEFAULT 0;
ALTER TABLE payment_intents ADD COLUMN reserved_refund_minor bigint NOT NULL DEFAULT 0;
ALTER TABLE payment_intents ADD CONSTRAINT payment_refund_balance CHECK
 (refunded_minor>=0 AND reserved_refund_minor>=0 AND refunded_minor+reserved_refund_minor<=amount);
ALTER TABLE payment_bindings ALTER COLUMN intent_id DROP NOT NULL;
ALTER TABLE payment_bindings ADD COLUMN resource_kind text NOT NULL DEFAULT 'payment';
ALTER TABLE payment_bindings ADD COLUMN resource_id uuid;
UPDATE payment_bindings SET resource_id=intent_id;
ALTER TABLE payment_bindings ALTER COLUMN resource_id SET NOT NULL;
ALTER TABLE payment_bindings ADD CONSTRAINT payment_binding_kind CHECK
 (resource_kind IN ('payment','subscription_checkout','subscription','customer','invoice','refund'));
ALTER TABLE payment_webhook_inbox ALTER COLUMN intent_id DROP NOT NULL;
ALTER TABLE payment_webhook_inbox ADD COLUMN resource_kind text NOT NULL DEFAULT 'payment';
ALTER TABLE payment_webhook_inbox ADD COLUMN resource_id uuid;
UPDATE payment_webhook_inbox SET resource_id=intent_id;
ALTER TABLE payment_webhook_inbox ALTER COLUMN resource_id SET NOT NULL;
CREATE TABLE billing_checkouts (
 id uuid PRIMARY KEY,workspace_id uuid NOT NULL,actor_id uuid NOT NULL,
 idempotency_key text NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 128),fingerprint text NOT NULL,
 plan text NOT NULL,price_id text NOT NULL,unit_amount bigint NOT NULL CHECK(unit_amount BETWEEN 1 AND 999999999),
 currency text NOT NULL,quantity integer NOT NULL CHECK(quantity BETWEEN 1 AND 100),
 interval text NOT NULL CHECK(interval IN ('month','year')),provider text NOT NULL,provider_key text NOT NULL UNIQUE,
 provider_object_id text,checkout_url text,status text NOT NULL CHECK(status IN ('creating','unknown','pending','complete','expired')),
 fence uuid,lease_until timestamptz,created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
 CHECK(unit_amount*quantity<=999999999),UNIQUE(workspace_id,idempotency_key),UNIQUE(workspace_id,id)
);
CREATE INDEX billing_checkout_workspace ON billing_checkouts(workspace_id,created_at DESC,id);
CREATE TABLE billing_subscriptions (
 id uuid PRIMARY KEY,workspace_id uuid NOT NULL,checkout_id uuid NOT NULL,provider text NOT NULL,
 provider_object_id text NOT NULL,customer_id text NOT NULL,plan text NOT NULL,price_id text NOT NULL,
 unit_amount bigint NOT NULL,quantity integer NOT NULL,currency text NOT NULL,interval text NOT NULL,
 status text NOT NULL CHECK(status IN ('incomplete','trialing','active','past_due','unpaid','paused','canceled','incomplete_expired')),
 cancel_at_period_end boolean NOT NULL DEFAULT false,current_period_end timestamptz,
 event_created bigint NOT NULL DEFAULT 0,event_id text NOT NULL DEFAULT '',
 cancel_status text NOT NULL DEFAULT 'none' CHECK(cancel_status IN ('none','creating','unknown','pending','confirmed')),
 cancel_idempotency_key text,cancel_provider_key text,cancel_fence uuid,cancel_lease_until timestamptz,cancel_created_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(workspace_id,id),UNIQUE(workspace_id,checkout_id),UNIQUE(provider,provider_object_id),
 FOREIGN KEY(workspace_id,checkout_id) REFERENCES billing_checkouts(workspace_id,id)
);
CREATE INDEX billing_subscription_workspace ON billing_subscriptions(workspace_id,created_at DESC,id);
CREATE TABLE billing_invoices (
 id uuid PRIMARY KEY,workspace_id uuid NOT NULL,subscription_id uuid NOT NULL,provider text NOT NULL,
 provider_object_id text NOT NULL,customer_id text NOT NULL,amount_due bigint NOT NULL CHECK(amount_due BETWEEN 0 AND 999999999),
 amount_paid bigint NOT NULL CHECK(amount_paid BETWEEN 0 AND 999999999),currency text NOT NULL,
 status text NOT NULL CHECK(status IN ('draft','open','paid','void','uncollectible')),
 period_start timestamptz NOT NULL,period_end timestamptz NOT NULL,CHECK(period_end>=period_start),
 event_created bigint NOT NULL,event_id text NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(provider,provider_object_id),UNIQUE(workspace_id,id),
 FOREIGN KEY(workspace_id,subscription_id) REFERENCES billing_subscriptions(workspace_id,id)
);
CREATE INDEX billing_invoice_workspace ON billing_invoices(workspace_id,created_at DESC,id);
CREATE TABLE payment_refunds (
 id uuid PRIMARY KEY,workspace_id uuid NOT NULL,actor_id uuid NOT NULL,intent_id uuid NOT NULL,
 idempotency_key text NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 128),fingerprint text NOT NULL,
 amount bigint NOT NULL CHECK(amount BETWEEN 1 AND 999999999),currency text NOT NULL,
 provider text NOT NULL,provider_key text NOT NULL UNIQUE,payment_object_id text NOT NULL,
 provider_object_id text,provider_payment_id text,status text NOT NULL CHECK(status IN ('creating','unknown','pending','succeeded','failed')),
 fence uuid,lease_until timestamptz,created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(workspace_id,intent_id,idempotency_key),UNIQUE(workspace_id,id),
 FOREIGN KEY(workspace_id,intent_id) REFERENCES payment_intents(workspace_id,id)
);
CREATE INDEX payment_refund_workspace ON payment_refunds(workspace_id,created_at DESC,id);
CREATE TABLE payment_refund_effects (
 workspace_id uuid NOT NULL,refund_id uuid NOT NULL,intent_id uuid NOT NULL,amount bigint NOT NULL,currency text NOT NULL,
 applied_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(workspace_id,refund_id),
 FOREIGN KEY(workspace_id,refund_id) REFERENCES payment_refunds(workspace_id,id),
 FOREIGN KEY(workspace_id,intent_id) REFERENCES payment_intents(workspace_id,id)
);
CREATE TRIGGER payment_refund_effect_no_change BEFORE UPDATE OR DELETE ON payment_refund_effects
 FOR EACH ROW EXECUTE FUNCTION payment_effect_immutable();
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['billing_checkouts','billing_subscriptions','billing_invoices','payment_refunds','payment_refund_effects'] LOOP
 EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
 EXECUTE format($policy$CREATE POLICY workspace_isolation ON %I USING(workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid) WITH CHECK(workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid)$policy$,t);
 END LOOP;
END $$;

CREATE TABLE payment_pending_webhooks (
 provider text NOT NULL,event_id text NOT NULL,provider_object_id text NOT NULL,fingerprint text NOT NULL,
 normalized jsonb NOT NULL CHECK(pg_column_size(normalized)<=65536),
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','applied')),
 attempts integer NOT NULL DEFAULT 1 CHECK(attempts BETWEEN 1 AND 100),
 received_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(provider,event_id)
);
CREATE INDEX payment_pending_provider ON payment_pending_webhooks(provider,state,received_at);
ALTER TABLE payment_pending_webhooks ENABLE ROW LEVEL SECURITY;
ALTER TABLE payment_pending_webhooks FORCE ROW LEVEL SECURITY;
CREATE POLICY verified_provider_event ON payment_pending_webhooks
 USING(provider=current_setting('app.payment_provider',true) AND
 (event_id=current_setting('app.payment_event',true) OR current_setting('app.payment_replay',true)='true'))
 WITH CHECK(provider=current_setting('app.payment_provider',true) AND
 event_id=current_setting('app.payment_event',true));
""",
)
