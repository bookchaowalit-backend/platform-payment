# Subscription billing and refunds (implemented-local)

Correlation `SHARED-INFRA-2026-10-15`. This staged extension adds subscription
Checkout, subscription lifecycle, invoice records and refunds. It uses provider
Billing APIs for recurring charging; no application renewal loop creates raw
PaymentIntents. No live mode, actual provider call or production acceptance is
included. Invoice records are provider metadata, not an accounting ledger or
tax filing system.

## Compatibility and migration

Existing factory calls and one-time routes remain valid:
`create_app(database, verifier, provider=None, *, plan_catalog=None)`.
The optional `BillingProvider` protocol extends the one-time provider with
`create_subscription_checkout`, `cancel_subscription`, `create_refund`.
Providers without these methods continue serving one-time payments; unsupported
writes return 503. No fake success is returned.

Migration `002-billing-refunds` extends the existing provider binding directory
and durable inbox, adds tenant-owned billing/refund tables and reserves/refunded
balances on payment intents. Migration `001-payments` is unchanged, SHA256
`99fd787b2ed865bf64e8b418c9ec926ba0307b939aa5035ab5536f154fcd173f`.
A real upgrade test preserves old intent, binding and inbox rows and checks the
new resource IDs and zero balances. Apply with the explicit migration command;
startup never migrates. Readiness checks both migrations and FORCE RLS tables.

Use a separate schema-owning migrator. The restricted runtime needs schema
USAGE; SELECT on migration history; SELECT/INSERT/UPDATE on payment intents,
binding directory, inbox, billing checkouts/subscriptions/invoices, refunds and
pending verified events; SELECT/INSERT on payment/refund effect tables. Do not
grant TRUNCATE, schema ownership or RLS-bypass capabilities. Immutable triggers
reject UPDATE/DELETE on effect markers. All tenant tables ENABLE and FORCE RLS.

A pre-002 image is not a safe rollback target: new resource-ID columns and
migration checks must be understood by the rollback image. Stage and retain a
reviewed 002-compatible image, or forward-fix. Never drop bindings, inbox rows,
refund reservations or provider keys to make an old image start.

## Server-owned catalog

The new non-secret configuration `PAYMENT_PLAN_CATALOG_JSON` contains at most
100 allowlisted plan entries. A factory may instead inject a mapping:

```json
{"basic":{"price_id":"price_CONFIGURED","unit_amount":1500,"currency":"usd","interval":"month"}}
```

Each entry supplies an actual configured Stripe recurring Price ID, integer
minor-unit price, supported lowercase currency and month/year interval. Price
interval count is one; a distinct Product per plan with Prices for that plan's
billing variants is recommended. Validate actual sandbox Price configuration
against the stored catalog before release. No caller chooses prices, amounts,
provider endpoints or checkout return URLs. Existing server-owned
`PAYMENT_SUCCESS_URL`/`PAYMENT_CANCEL_URL` remain in use.

Catalog values are validated at startup and copied into immutable plan objects.
Each checkout snapshots the selected price/amount/currency/interval/quantity.
Replaying an existing request uses its saved snapshot even if the deployment's
catalog later changes. Unknown plans/extra fields are rejected. An absent
catalog disables new subscription checkouts and cancellation writes with 503;
existing lifecycle webhooks and read APIs use stored bindings/snapshots.
Refunds require the provider capability, and do not require a plan catalog.

Tax note: automatic tax is deliberately disabled. Decide tax registrations and
approved Stripe Tax setup before production billing; this implementation does
not calculate or file tax. Do not enable automatic_tax without an active
registration and separate acceptance evidence.

## Routes and scopes

Every tenant route gets workspace exclusively from the verified JWT `ws`.

| Route | Scope | Behavior |
|---|---|---|
| POST `/v1/billing/checkouts` | `billing:write` | `{plan,quantity}` plus Idempotency-Key; quantity strict integer 1..100, default 1; total <=999,999,999 |
| GET `/v1/billing/checkouts/{id}` | `billing:read` | Saved state, plan snapshot and checkout URL |
| POST `/v1/billing/checkouts/{id}/reconcile` | `billing:write` | Retry unresolved provider creation using saved identity/key |
| GET `/v1/subscriptions` and `/{id}` | `billing:read` | Saved plan, quantity, status, period end and cancellation state |
| POST `/v1/subscriptions/{id}/cancel` | `billing:write` | Idempotency-Key; requests cancel_at_period_end=true only |
| POST `/v1/subscriptions/{id}/cancel/reconcile` | `billing:write` | Reconcile ambiguous cancellation with its stable provider key |
| GET `/v1/invoices` and `/{id}` | `billing:read` | Provider invoice amount due/paid, currency, status and periods |
| POST `/v1/payment-intents/{id}/refunds` | `payments:refund` | `{amount}` plus Idempotency-Key; only a stored paid one-time intent/checkout |
| GET `/v1/payment-intents/{id}/refunds` | `payments:read` | Refund list for that intent |
| GET `/v1/refunds/{id}` | `payments:read` | Durable refund state |
| POST `/v1/refunds/{id}/reconcile` | `payments:refund` | Retry unresolved refund with its stable provider key |

List APIs use limit 1..100 (default 50), offset 0..10000. Foreign-workspace IDs
return 404; missing scope 403. Unknown body fields and booleans/floats in integer
amount/quantity fields return 422. Idempotency keys are 1..128 characters.
Checkout keys are workspace-scoped; refund keys are workspace + intent + key;
cancellation reserves one fixed at-period-end operation per subscription.
Changed request payload under an existing key returns 409.

Creation and refund rows are committed before provider calls, with a stable
provider key and 60-second fenced lease. API responses are 201 for a newly bound
operation, 200 for resolved replay, or 202 while creating/unknown. Normal replay
never blindly repeats an unknown call; the explicit reconcile route resumes
with the exact saved key/parameters. Reconciliation refuses external retries
past 20 hours to stay within provider idempotency retention. Old ambiguous
operations need manual provider-side investigation, never replacement creation.

Cancellation changes only cancel_at_period_end, never immediate deletion. A
provider acknowledgement leaves cancellation pending; a signed lifecycle
snapshot confirms the schedule/canceled state. The original operation's key
is retained. External plan swaps, cancellation reversals and shared-provider
customer changes are outside this bounded slice and need manual review.

## Subscription and invoice events

Configure the required signed sandbox webhook at `/v1/webhooks/provider` for
checkout.session.completed/expired/async_payment_succeeded/async_payment_failed,
customer.subscription.created/updated/deleted, invoice.created/finalized/updated/
paid/payment_succeeded/payment_failed/voided/marked_uncollectible and
refund.created/updated/failed, plus existing one-time events.

The raw-body Stripe signature is verified with 300-second tolerance before
normalization. Live events are rejected. Checkout completion associates only
its stored checkout with the signed Subscription and Customer IDs; the local
subscription starts incomplete, never implicitly active/paid. A signed lifecycle
snapshot must match stored customer, Price ID, unit amount, quantity, currency
and interval. This pilot uses one recurring item and one dedicated Customer
per checkout; multi-item/shared-customer/plan-change flows require a new design.
Subscription activation/renewal/failure/cancellation comes from lifecycle events,
not the browser return page.

Invoices route through their bound invoice ID or an already known Subscription
ID (Customer ID when no subscription association is supplied). A provided
subscription association must be known and match the stored customer. Workspace
metadata is ignored. Currency must match the saved subscription. Bounded due/
paid amounts and period timestamps are mirrored; unsupported partial/overpaid
invoice shapes fail closed for operator handling. Invoices do not grant access
or create ledger revenue entries.

The inbox insert, record update, effect marker and pending-event acknowledgement
are atomic. Duplicate events never reapply effects; changed raw payload under
the same provider event ID returns 409. Older lifecycle snapshots are recorded
but cannot reverse newer state; canceled/incomplete_expired subscriptions are
terminal. Same-second conflicting subscription snapshots return 409 and remain
pending for fresh provider evidence/manual reconciliation instead of choosing
an arbitrary event-ID order. Paid/void/uncollectible invoices are terminal;
a same-second late draft cannot replace an open invoice.

## Durable events that precede bindings

Every recognized signed event is first stored in a normalized verified-event
spool with its raw-body fingerprint. Only an explicit allowlist of bounded
fields is stored, never the raw body, signature, bearer token or tenant metadata.
A missing binding returns 409, keeping the event pending for provider retry and
operator replay. The existing tenant inbox remains the effect dedupe boundary.

The spool has no workspace until an object binding resolves authority. It uses
ENABLE/FORCE RLS with server-set provider + exact event-ID context. A separate
transaction-local replay context is used only by the operator command; no tenant
HTTP endpoint can set it, list or inspect the unbound queue. Binding lookup and
replay context are reset at transaction exit. The worker role is trusted service
infrastructure; do not expose its database credentials to applications.

Run the bounded operator command, with explicit runtime DB/provider configuration:

```bash
python -m platform_payment replay-webhooks --limit 50
```

It replays at most 100 previously verified records without making provider calls,
counts applied/pending results, and stops automatic replay after 20 attempts per
record. Concurrent replays are safe through row locks and the unique inbox.
Schedule with backoff through the owning deployment; monitor pending age/count
and manually investigate exhausted/unresolvable records. Provider retries may
continue; counters saturate at 100 rather than overflowing. Preserve this queue
and the inbox across backup/restore. Establish an approved audit/retention policy
before production; no destructive pruning is enabled by this pilot.

## Refund balance and settlement

Creating a refund locks the paid intent and atomically reserves the requested
amount against `amount - refunded_minor - reserved_refund_minor`. Concurrent
requests cannot over-reserve, and an idempotent replay does not reserve twice.
Unknown or pending refunds retain their reservation; a timeout never releases
money or invents provider failure. Stripe refunds use the stored PaymentIntent
binding; a paid one-time Checkout resolves its PaymentIntent through the stored
Session ID. Subscription invoices are not refunded through this route.

Only a signed bound refund event with exact amount/currency (and the stored
payment reference when supplied) changes settlement. Succeeded events move the
reservation to refunded balance and insert one immutable refund-success marker.
Failed/canceled refunds release their reservation. Success/failure are terminal;
late pending or contradictory terminal events cannot apply another effect.
A database CHECK enforces nonnegative balances and total <= paid amount.
The original payment-success marker is unchanged; no double-entry/accounting
ledger claim is made. Out-of-band provider refunds/disputes are not automatically
reconciled into this balance and must be investigated before additional refunds.

## Release gates

Tests use real disposable PostgreSQL with restricted roles and local fake
providers, plus actual Stripe SDK/signature handling with intercepted transport.
No charges or provider network requests occurred. Before production: verify
Price/catalog parity, lifecycle/async events and refund behavior in an approved
sandbox; settle same-second ambiguities/out-of-band changes; verify credentials,
audit retention, backup/restore and deployment limits; complete independent
security review and owner approval. Live mode remains unavailable in this code.
