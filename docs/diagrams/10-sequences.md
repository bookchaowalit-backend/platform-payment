# 10 — Sequence diagrams

## S1 — Create payment intent (idempotent, ambiguity-safe)

```mermaid
sequenceDiagram
  autonumber
  participant C as Caller
  participant A as payment-api
  participant V as TokenVerifier
  participant D as PostgreSQL
  participant S as Stripe sandbox
  C->>A: POST /v1/payment-intents, Bearer, Idempotency-Key, amount and currency
  A->>V: verify RS256, iss, aud, ws, scope payments:write
  V-->>A: Principal actor, workspace
  A->>D: BEGIN, set app.workspace_id, INSERT intent creating with fence and lease 60 s, ON CONFLICT DO NOTHING
  D-->>A: row, compare fingerprint
  alt key exists, same payload
    A-->>C: 200 saved state, or 202 if creating or unknown
  else key exists, changed payload
    A-->>C: 409
  else new row
    A->>S: payment_intents.create, idempotency_key platform-id
    alt success
      A->>D: lock row, check fence, INSERT binding, UPDATE status pending
      A-->>C: 201 pending
    else timeout or error
      A->>D: UPDATE status unknown where fence matches
      A-->>C: 202 unknown
    end
  end
```

Sources: `src/platform_payment/app.py` (`create_payment`, `perform`), `adapters.py` (`StripeProvider.create`), `storage.py`, platform-backend-core `21d5f44` `auth.py`, `db.py`

## S2 — Verified webhook to settlement and ledger

```mermaid
sequenceDiagram
  autonumber
  participant S as Stripe sandbox
  participant A as payment-api
  participant D as PostgreSQL
  S->>A: POST /v1/webhooks/provider, raw body, Stripe-Signature
  A->>A: construct_event, tolerance 300 s, livemode must be false, normalize
  alt bad signature or payload
    A-->>S: 400
  end
  A->>D: tx 1: set provider and event context, UPSERT payment_pending_webhooks attempts plus 1
  A->>D: tx 2: binding lookup via payment_binding_route policy
  alt not bound
    A-->>S: 409, retry later, or ignored after 3 deliveries for payment_intent events
  end
  A->>D: tx 3 in bound workspace: lock intent, check amount and currency
  A->>D: INSERT inbox, ON CONFLICT compare fingerprint
  A->>D: UPDATE status, INSERT payment_effects, INSERT 2 ledger legs
  D->>D: statement trigger checks balance
  A->>D: mark pending event applied, COMMIT
  A-->>S: 200 accepted, duplicate flag
```

Sources: `src/platform_payment/app.py` (`webhook`, `process_event`), `adapters.py` (`verify_event`), `event_router.py`, `pending_webhooks.py`, `payment_events.py`, `billing_events.py` (`lookup`), `ledger.py`, `ledger_migrations.py`

## S3 — Refund with reservation

```mermaid
sequenceDiagram
  autonumber
  participant C as Caller
  participant A as payment-api
  participant D as PostgreSQL
  participant S as Stripe sandbox
  C->>A: POST /v1/payment-intents/id/refunds, payments:refund, Idempotency-Key, amount
  A->>D: lock intent FOR UPDATE, check succeeded and available balance
  A->>D: reserved_refund_minor plus amount, INSERT refund creating with fence
  A->>S: if checkout, retrieve session for payment_intent, then refunds.create with idempotency key
  alt ok
    A->>D: bind refund object, status pending
    A-->>C: 201 pending
  else error
    A->>D: status unknown, reservation kept
    A-->>C: 202 unknown
  end
  S->>A: refund.updated signed webhook
  A->>D: succeeded: reserved to refunded, INSERT refund effect, ledger journal
  A->>D: failed: release reservation
```

Sources: `src/platform_payment/refunds.py`, `provider_operations.py`, `stripe_billing.py` (`create_refund`), `stripe_events.py`, `ledger.py`

## S4 — Readiness probe

```mermaid
sequenceDiagram
  participant P as Probe
  participant A as payment-api
  participant D as PostgreSQL
  P->>A: GET /readyz
  A->>A: provider configured and refuse_live_mode
  A->>D: has_schema_privilege USAGE, has_table_privilege per grant
  A->>D: migration history equals MIGRATIONS checksums
  A->>D: pg_class relrowsecurity and relforcerowsecurity for 11 tables
  alt all pass
    A-->>P: 200 ready
  else any failure
    A-->>P: 503 unavailable, no detail
  end
```

Sources: `src/platform_payment/app.py` (`readiness`, `READINESS_PERMISSIONS`)
