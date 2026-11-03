# 09 — ERD

จาก migrations `001-payments`, `002-billing-refunds`, `003-ledger` (namespace `platform_payment`, ประวัติใน `platform_schema_migrations` ของ core)
FK ทุกตัวเป็นแบบ composite `(workspace_id, id)` เพื่อไม่ให้อ้างข้าม workspace ได้

```mermaid
erDiagram
  payment_intents {
    uuid id PK
    uuid workspace_id
    uuid actor_id
    text operation "intent or checkout"
    text idempotency_key
    text fingerprint
    bigint amount "1..999999999 minor units"
    text currency
    text provider
    text provider_key UK
    text provider_object_id
    text checkout_url
    text status "creating unknown pending failed succeeded"
    timestamptz lease_until
    uuid fence
    bigint refunded_minor "002"
    bigint reserved_refund_minor "002"
    timestamptz created_at
    timestamptz updated_at
  }
  payment_bindings {
    text provider PK
    text provider_object_id PK
    uuid workspace_id
    uuid intent_id "nullable since 002"
    text resource_kind "payment subscription_checkout subscription customer invoice refund"
    uuid resource_id
  }
  payment_webhook_inbox {
    text provider PK
    text event_id PK
    uuid workspace_id
    uuid intent_id "nullable since 002"
    text resource_kind
    uuid resource_id
    text fingerprint "sha256 of raw body"
    timestamptz received_at
  }
  payment_effects {
    uuid workspace_id PK
    uuid intent_id PK
    text kind PK "payment_succeeded"
    bigint amount
    text currency
    timestamptz applied_at
  }
  billing_checkouts {
    uuid id PK
    uuid workspace_id
    uuid actor_id
    text idempotency_key
    text plan
    text price_id
    bigint unit_amount
    text currency
    int quantity "1..100"
    text interval "month or year"
    text provider_key UK
    text provider_object_id
    text checkout_url
    text status "creating unknown pending complete expired"
  }
  billing_subscriptions {
    uuid id PK
    uuid workspace_id
    uuid checkout_id
    text provider_object_id
    text customer_id
    text price_id
    bigint unit_amount
    int quantity
    text status "incomplete trialing active past_due unpaid paused canceled incomplete_expired"
    bool cancel_at_period_end
    timestamptz current_period_end
    bigint event_created
    text cancel_status "none creating unknown pending confirmed"
    text cancel_provider_key
  }
  billing_invoices {
    uuid id PK
    uuid workspace_id
    uuid subscription_id
    text provider_object_id
    bigint amount_due
    bigint amount_paid
    text status "draft open paid void uncollectible"
    timestamptz period_start
    timestamptz period_end
    bigint event_created
  }
  payment_refunds {
    uuid id PK
    uuid workspace_id
    uuid actor_id
    uuid intent_id
    text idempotency_key
    bigint amount
    text currency
    text provider_key UK
    text payment_object_id
    text provider_object_id
    text provider_payment_id
    text status "creating unknown pending succeeded failed"
  }
  payment_refund_effects {
    uuid workspace_id PK
    uuid refund_id PK
    uuid intent_id
    bigint amount
    text currency
    timestamptz applied_at
  }
  payment_pending_webhooks {
    text provider PK
    text event_id PK
    text provider_object_id
    text fingerprint
    jsonb normalized "allowlisted fields, at most 64 KiB"
    text state "pending or applied"
    int attempts "1..100"
  }
  payment_ledger_entries {
    uuid workspace_id PK
    text source_kind PK "payment_succeeded refund_succeeded"
    uuid source_id PK
    text account PK "provider_clearing payment_revenue payment_refunds"
    text direction PK "debit or credit"
    uuid intent_id
    bigint amount
    text currency
    timestamptz posted_at
  }
  payment_intents ||--o{ payment_bindings : "bound as payment"
  payment_intents ||--o{ payment_webhook_inbox : "events"
  payment_intents ||--o| payment_effects : "success marker"
  payment_intents ||--o{ payment_refunds : "refunds"
  payment_refunds ||--o| payment_refund_effects : "success marker"
  payment_intents ||--o{ payment_refund_effects : "intent"
  payment_intents ||--o{ payment_ledger_entries : "journals"
  billing_checkouts ||--o| billing_subscriptions : "creates"
  billing_subscriptions ||--o{ billing_invoices : "invoices"
```

Sources: `src/platform_payment/migrations.py`, `billing_migrations.py`, `ledger_migrations.py`

## Constraints, triggers และ RLS

| ตาราง | Unique / check สำคัญ | Trigger | RLS policy |
|---|---|---|---|
| `payment_intents` | `UNIQUE(workspace_id, operation, idempotency_key)`; `payment_refund_balance`: refunded + reserved ≤ amount | — | `payment_intent_workspace` |
| `payment_bindings` | PK `(provider, provider_object_id)` | — | `payment_binding_workspace` + `payment_binding_route` (SELECT ด้วย `app.payment_provider` + `app.payment_object`) |
| `payment_webhook_inbox` | PK `(provider, event_id)` | — | `payment_inbox_workspace` |
| `payment_effects` | PK `(workspace_id, intent_id, kind)` | `payment_effect_no_change` (UPDATE/DELETE raise) | `payment_effect_workspace` |
| `billing_checkouts` | `UNIQUE(workspace_id, idempotency_key)`; `unit_amount*quantity ≤ 999999999` | — | `workspace_isolation` |
| `billing_subscriptions` | `UNIQUE(workspace_id, checkout_id)`, `UNIQUE(provider, provider_object_id)` | — | `workspace_isolation` |
| `billing_invoices` | `UNIQUE(provider, provider_object_id)`; `period_end ≥ period_start` | — | `workspace_isolation` |
| `payment_refunds` | `UNIQUE(workspace_id, intent_id, idempotency_key)` | — | `workspace_isolation` |
| `payment_refund_effects` | PK `(workspace_id, refund_id)` | `payment_refund_effect_no_change` | `workspace_isolation` |
| `payment_pending_webhooks` | PK `(provider, event_id)`; `pg_column_size(normalized) ≤ 65536` | — | `verified_provider_event`: provider + exact event id, หรือ `app.payment_replay='true'` (อ่าน) |
| `payment_ledger_entries` | PK 5 คอลัมน์ | `payment_ledger_balanced` (statement, balance + refund ≤ revenue), `payment_ledger_no_change`, `payment_ledger_no_truncate` | `workspace_isolation` |

ทั้ง 11 ตาราง ENABLE + FORCE RLS; runtime role ต้องเป็น NOSUPERUSER NOBYPASSRLS (core `Database._configure`) และได้ grant ตาม `READINESS_PERMISSIONS` ใน `app.py` (ไม่มี DELETE/TRUNCATE)

Sources: `src/platform_payment/migrations.py`, `billing_migrations.py`, `ledger_migrations.py`, `app.py`, platform-backend-core `21d5f44` `db.py`, `docs/runtime.md` §Effective runtime grants

## Open questions

- ไม่มี retention/pruning ของ `payment_pending_webhooks` และ `payment_webhook_inbox` (`docs/billing-runtime.md`: "no destructive pruning is enabled by this pilot")
- hosted Neon apply migration ครบ 001–003 แล้วหรือยัง: `/readyz` 503 ไม่บอกเหตุผล (ตั้งใจไม่บอก) และ ops record บอกแค่ว่า local `/readyz` 200 ด้วย test-mode provider
