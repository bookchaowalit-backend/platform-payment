from platform_backend_core import Migration

# Additive: a pre-003 image keeps settling but would not post journals, so it is
# not a rollback target (see docs/runtime.md, "Ledger").
LEDGER_MIGRATION = Migration(
    "003-ledger",
    """
CREATE TABLE payment_ledger_entries (
 workspace_id uuid NOT NULL,
 source_kind text NOT NULL CHECK(source_kind IN ('payment_succeeded','refund_succeeded')),
 source_id uuid NOT NULL,intent_id uuid NOT NULL,
 account text NOT NULL CHECK(account IN ('provider_clearing','payment_revenue','payment_refunds')),
 direction text NOT NULL CHECK(direction IN ('debit','credit')),
 amount bigint NOT NULL CHECK(amount BETWEEN 1 AND 999999999),
 currency text NOT NULL CHECK(currency ~ '^[a-z]{3}$'),
 posted_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(workspace_id,source_kind,source_id,account,direction),
 FOREIGN KEY(workspace_id,intent_id) REFERENCES payment_intents(workspace_id,id)
);
CREATE INDEX payment_ledger_intent ON payment_ledger_entries(workspace_id,intent_id);
-- Balance is checked per INSERT statement over the transition table, so every
-- journal must be posted whole; RLS never hides the rows being checked.
CREATE FUNCTION payment_ledger_check() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS (SELECT 1 FROM posted GROUP BY workspace_id,source_kind,source_id
  HAVING count(*)<2 OR count(DISTINCT currency)<>1 OR count(DISTINCT intent_id)<>1
  OR sum(CASE direction WHEN 'debit' THEN amount ELSE -amount END)<>0) THEN
  RAISE EXCEPTION 'unbalanced ledger journal' USING ERRCODE='check_violation';
 END IF;
 IF EXISTS (SELECT 1 FROM payment_ledger_entries e
  WHERE (e.workspace_id,e.intent_id) IN (SELECT workspace_id,intent_id FROM posted)
  GROUP BY e.workspace_id,e.intent_id
  HAVING sum(CASE WHEN e.account='payment_refunds' AND e.direction='debit' THEN e.amount ELSE 0 END)
   > sum(CASE WHEN e.account='payment_revenue' AND e.direction='credit' THEN e.amount ELSE 0 END)) THEN
  RAISE EXCEPTION 'ledger refunds exceed the captured amount' USING ERRCODE='check_violation';
 END IF;
 RETURN NULL;
END $$;
CREATE TRIGGER payment_ledger_balanced AFTER INSERT ON payment_ledger_entries
 REFERENCING NEW TABLE AS posted FOR EACH STATEMENT EXECUTE FUNCTION payment_ledger_check();
CREATE TRIGGER payment_ledger_no_change BEFORE UPDATE OR DELETE ON payment_ledger_entries
 FOR EACH ROW EXECUTE FUNCTION payment_effect_immutable();
CREATE TRIGGER payment_ledger_no_truncate BEFORE TRUNCATE ON payment_ledger_entries
 FOR EACH STATEMENT EXECUTE FUNCTION payment_effect_immutable();
-- Backfill journals for effects settled before this migration. The schema owner
-- is exempt from RLS only while FORCE is lifted inside this transaction.
ALTER TABLE payment_effects NO FORCE ROW LEVEL SECURITY;
ALTER TABLE payment_refund_effects NO FORCE ROW LEVEL SECURITY;
INSERT INTO payment_ledger_entries(workspace_id,source_kind,source_id,intent_id,account,direction,amount,currency)
 SELECT e.workspace_id,'payment_succeeded',e.intent_id,e.intent_id,l.account,l.direction,e.amount,e.currency
 FROM payment_effects e CROSS JOIN (VALUES('provider_clearing','debit'),('payment_revenue','credit')) l(account,direction);
INSERT INTO payment_ledger_entries(workspace_id,source_kind,source_id,intent_id,account,direction,amount,currency)
 SELECT e.workspace_id,'refund_succeeded',e.refund_id,e.intent_id,l.account,l.direction,e.amount,e.currency
 FROM payment_refund_effects e CROSS JOIN (VALUES('payment_refunds','debit'),('provider_clearing','credit')) l(account,direction);
ALTER TABLE payment_effects FORCE ROW LEVEL SECURITY;
ALTER TABLE payment_refund_effects FORCE ROW LEVEL SECURITY;
ALTER TABLE payment_ledger_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE payment_ledger_entries FORCE ROW LEVEL SECURITY;
CREATE POLICY workspace_isolation ON payment_ledger_entries
 USING(workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid)
 WITH CHECK(workspace_id=nullif(current_setting('app.workspace_id',true),'')::uuid);
""",
)
