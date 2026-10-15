# Contract surface: book-payment-platform

This page documents the API, event, CLI, storage and configuration surface that
`book-payment-platform` owns or consumes. The machine-readable list is `interfaces` in
[`contract.json`](../contract.json); `scripts/check.py` fails when an interface
listed there is missing from this page.

- Platform: `payment` (contract `book-platform.contract.v1`, API `v1`)
- Status: `pilot`; source status: `current-implementation-in-solo-empire`
- Current implementation: `solo-empire` at `infra/api/stripe.ts`, `infra/api/server.ts`
- Depends on: `identity`, `security`, `observability`

Interfaces marked `current-in-solo-empire` are served by the `solo-empire`
control plane today. This repository does not serve them yet; they are the
compatibility surface a migration must preserve. The `source` column is a path
in the implementing repository.

## Interfaces

| ID | Kind | Direction | Interface | Contract | Auth | Source | Status |
|---|---|---|---|---|---|---|---|
| `payment.http.stripe-webhook` | http | provides | `POST /api/webhook/stripe` | - | Stripe-Signature v1 HMAC-SHA256 over `t.raw_body`, 300 s tolerance | `infra/api/server.ts` | current-in-solo-empire |
| `payment.table.webhook-log` | table | provides | webhook_log rows with source='stripe', external_id=<event id>, processed=0 | - | - | `infra/api/server.ts` | current-in-solo-empire |
| `payment.http.revenue-webhook` | http | provides | `POST /api/webhook/revenue (ledger candidate)` | - | Bearer token with scope api:write plus Idempotency-Key header | `infra/api/server.ts` | current-in-solo-empire |
| `payment.consumes.identity-bearer` | http | consumes | Identity bearer-token check (`requireAuth`, scope `api:write`) | - | Bearer token with scope api:write | `infra/api/auth.ts` | current-in-solo-empire |

## Behaviour notes

Derived from the current source; re-check the source before changing a
consumer.

- `POST /api/webhook/stripe` verifies the raw body before JSON parsing. Missing
  `STRIPE_WEBHOOK_SECRET` returns 503; a bad signature or timestamp outside the
  300 s tolerance returns 400; the event must carry non-empty `id` and `type`.
- Duplicate delivery is idempotent on the Stripe event id: the response reports
  `accepted_events: 0, duplicate_events: 1` and nothing new is written.
- Accepted events are stored as pending (`processed = 0`); the route never
  creates charges, refunds or ledger entries.
- `POST /api/webhook/revenue` requires `source_type` (`service`, `product`,
  `subscription`, `affiliate`, `ad`, `sponsorship`, `other`), `source_name`,
  positive `amount` (<= 1e9), `revenue_date` (`YYYY-MM-DD`) and an
  `Idempotency-Key` header (<= 255 chars); currency defaults to `THB`. It is a
  ledger candidate owned by the finance control plane today; the ledger
  migration must decide whether it moves here.

## Migration gates

- sandbox provider parity
- webhook idempotency
- ledger rollback

## Out of scope

No production activation, credentials or customer payloads are included.
The v1 API owns its PostgreSQL migrations and database writes; it does not
implement subscriptions, billing, accounting ledger or financial reconciliation. Record parity, privacy and rollback evidence
in the parent platform registry before any cutover.

## Implemented-local v1 API

These are separate versioned routes in this owning repository; existing
legacy compatibility surfaces above remain historical migration references.
No legacy hub module is imported at runtime.

- `payment.http.v1-runtime`: POST /v1/payment-intents; POST /v1/checkout; reconcile; GET intents; POST /v1/webhooks/provider; source `src/platform_payment/app.py`.
- `payment.table.workspace-state`: FORCE RLS PostgreSQL durable service-owned state; source `src/platform_payment/migrations.py`.

See [runtime contract](runtime.md) for request/response bounds, scopes, errors,
migration instructions, provider configuration and remaining release gates.
