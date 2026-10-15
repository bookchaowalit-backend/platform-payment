# Payment API runtime (implemented-local)

Correlation: `SHARED-INFRA-2026-10-15`. This pilot provides payment intent and
hosted checkout creation, durable idempotency, verified webhook inbox and one
immutable payment-success effect. The bounded subscription/invoice/refund extension is documented in
[billing-runtime.md](billing-runtime.md). It does not implement a financial ledger,
tax filing, disputes, entitlement delivery or financial reconciliation. Legacy consumers remain unchanged; no live payment mode or
production cutover is enabled.

## Install, migrations and configuration

Python >=3.12, PostgreSQL >=16. Install an immutable reviewed
`platform-backend-core` wheel/revision, then `pip install -e '.[test]'`.
Only local development may install `../platform-backend-core` editable.
Production release dependencies must use pinned artifacts.

`python -m platform_payment migrate` explicitly applies exported `MIGRATIONS`
with namespace `platform_payment`, using `DATABASE_MIGRATION_DSN`. Application
startup never migrates. A separate schema-owning migrator should grant the
runtime role schema USAGE; SELECT on `platform_schema_migrations`;
SELECT/INSERT/UPDATE on `payment_intents`; SELECT/INSERT on `payment_bindings`,
`payment_webhook_inbox`, `payment_effects`. Never grant table TRUNCATE or owner
capabilities to the deployed runtime. All tenant tables ENABLE and FORCE RLS.
The database wrapper rejects SUPERUSER and BYPASSRLS roles.

`python -m platform_payment serve` listens on `PORT` (default 8080).

| Name | Purpose |
|---|---|
| `DATABASE_URL` | Runtime DSN injected from the deployment secret store |
| `DATABASE_POOL_MAX` | Bound, default 8; budget across replicas |
| `JWT_ISSUER`, `JWT_AUDIENCE` | Exact trusted workspace issuer and audience |
| `JWT_PUBLIC_KEY` or `JWT_JWKS_URL` | Explicit PEM or HTTPS JWKS trust |
| `PAYMENT_STRIPE_API_KEY` | Injected sandbox key; prefer restricted `rk_test_` with least permissions |
| `PAYMENT_STRIPE_WEBHOOK_SECRET` | Injected sandbox signing secret |
| `PAYMENT_SUCCESS_URL`, `PAYMENT_CANCEL_URL` | Server-owned HTTPS checkout return destinations |

No credential-file discovery occurs. Missing provider config disables creation
and webhook processing with 503. Invalid complete config fails startup. All live
keys and live events are rejected; this pilot has no live-mode switch. Use a
separate Stripe sandbox for development and CI. No actual Stripe account,
credentials or charges were used in verification.

`/healthz` is process health. `/readyz` requires a configured adapter, exact
migration history and FORCE RLS on all service tables; missing schema/config
returns 503. It does not contact Stripe or certify credentials. `/metrics` uses
bounded route templates/status labels; secure it through ingress. API request
bodies are bounded at 1 MiB and requests at 30 seconds. Uvicorn access logging is
disabled to avoid signed checkout URL/header/body exposure.

Factory: `platform_payment.create_app(database, verifier, provider=None)`;
protocol `PaymentProvider` in `platform_payment.adapters`. Omitted adapters load
only the explicit environment names above. No hub or peer-service table is used.

## API, idempotency and ambiguous outcomes

JWT `ws` is the only tenant authority. `payments:write` permits creation and
reconciliation, `payments:read` permits GET. Wrong/missing JWT returns 401,
missing scope 403, other-workspace IDs 404. Unknown body fields return 422.

`POST /v1/payment-intents` (off-session/custom intent) and `POST /v1/checkout`
(hosted on-session flow) require `Idempotency-Key` of 1..128 characters:

```json
{"amount":1500,"currency":"usd"}
```

Amount is a strict integer in minor units, 1..999,999,999; booleans/floats are
rejected. Supported lowercase currencies are usd/eur/gbp/thb/jpy/aud/cad/sgd.
JPY uses zero-decimal units. Provider-specific minimum/maximum amounts may be
stricter and require staging validation. Tax calculation is not enabled.

A new record is committed as `creating` with a stable server-generated UUID,
provider idempotency key and 60-second fenced creation lease **before** calling
the SDK. The binding and `pending` response are then committed. New resolved
creation returns 201; identical replay returns the same ID (200 or 202 while
unresolved). Idempotency uniqueness is workspace + operation + caller key.
A changed canonical payload returns 409. Different operations have distinct
key namespaces. Keys and fingerprints remain durable across API replicas.

The API never treats a provider response or browser return page as settlement.
Only a verified bound webhook records success. Provider timeout/error returns
202 with the same intent and `status=unknown`; do not retry with a new caller
key. Replaying the original key reads durable state and does not start another
provider call.

`POST /v1/payment-intents/{id}/reconcile` resumes unknown or expired creation
leases using the same persisted provider idempotency key and parameters. The
60-second lease/fence serializes competing reconcilers. Creation retries are
refused after 20 hours, before Stripe may prune its >=24-hour idempotency record:
return 409 and perform manual provider-side reconciliation before any further
creation. Never create a replacement automatically when the outcome is unknown.
A response lost after provider success remains recoverable with that stable key.
A webhook arriving before object binding returns 409 for provider retry after
reconciliation. Unknown object bindings do not select a tenant.

`GET /v1/payment-intents/{id}` returns safe state. `GET /v1/payment-intents`
uses limit 1..100 (default 50), offset 0..10000, descending creation/id ordering.
No card data, provider API keys or client secrets are accepted or returned.

## Required provider webhook and state transitions

Configure the sandbox webhook to `POST /v1/webhooks/provider`. Stripe raw-body
signature verification (`Stripe-Signature`) occurs before JSON interpretation,
with 300-second tolerance. The sandbox adapter handles payment_intent.succeeded,
payment_failed, canceled, processing and checkout.session.completed,
async_payment_succeeded, async_payment_failed, expired. Unrelated event types
are acknowledged/ignored. Checkout completion remains pending while payment_status
is unpaid; only paid sessions produce a success effect. Configure event selection
for the flows used; unbound objects return 409 and require investigation.

After signature verification the provider object ID performs an exact stored
binding lookup. A transaction-local RLS SELECT policy permits this routing
lookup only for that provider/object pair, then all mutation occurs in the
resolved workspace transaction. Event workspace/tenant metadata never provides
authority. This policy is internal; no API accepts its context settings.

Amount and currency must equal the stored intent before any inbox/effect commit;
unknown binding or mismatch returns 409. Provider event IDs dedupe globally per
provider. The raw-body SHA256 fingerprint also binds the event payload; a changed
payload under the same event ID returns 409. Raw webhook bodies are not stored.
The inbox insert, payment transition and success-effect insert commit atomically.
Concurrent or repeated success events cannot duplicate the effect. A database
trigger prevents updating/deleting effect records. This marker is an integration
boundary for future fulfillment, not an accounting ledger.

Success is terminal. Late pending/failed events cannot undo success. Pending
cannot undo a recorded failure; a later verified success can supersede failure.
Refund settlement uses the separate reserved-balance extension; dispute/accounting
interpretation is not inferred from these states.

## Real Stripe adapter

Supports **Stripe Python SDK 14.4.1 exactly**, pinned in the distribution.
Uses `StripeClient`, explicit API version `2026-08-26.dahlia`, bounded HTTPX
synchronous transport (5 seconds, one network retry), provider idempotency
options and Checkout Sessions for on-session checkout. Dynamic payment methods
remain enabled; no card-only `payment_method_types` restriction is sent. Hosted
checkout uses server-owned return URLs and `integration_identifier`.
Never fulfill on the return URL. No automatic tax is enabled.

## Deployment and rollback gates

Build `Dockerfile` with immutable `CORE_REQUIREMENT`; there is no default core
artifact. Run non-root, one process per container, concurrency 64, pool max 8
and request timeout 30s. Bound all replicas to the database connection budget.
Inject credentials via the deployment secret store. Egress allows approved
Stripe/JWKS endpoints; ingress protects metrics and has a separate signed webhook
route. Keep proxy logs free of bodies, bearer headers and signed URLs.

Before release: independent payment/security review; approved Stripe sandbox
parity with actual signatures, hosted checkout and asynchronous methods;
restart/concurrency/backup-restore evidence; legacy consumer parity and rollback.
Back up the service DB before explicit migration. Roll back only to a reviewed image compatible with migration002, preserving
intents, bindings, inbox, pending events, refund reservations and effects. Do not drop dedupe/history or
reset provider keys. Manual reconciliation is required for old unresolved items.
No destructive migration or production activation is authorized here.

## Verification

Run the original `bash scripts/check.sh`, then complete `python -m pytest` with
a disposable fixture-admin `PLATFORM_TEST_DSN`. The new suite uses real
PostgreSQL and an injected idempotent fixture provider; SDK request/signature
coverage uses the real Stripe library with intercepted local HTTP transport.
No provider requests, charges, secrets or external messages occur. Tests skip DB
cases when DSN is absent; such a run does not prove durability. See
[recorded local evidence](verification-2026-10-15.md).

The program-standard `PLATFORM_TEST_DSN` is an explicitly supplied disposable
**fixture admin** DSN with CREATE DATABASE/ROLE privileges. Each runtime fixture
uses `platform_backend_core.testing.temporary_database` to create a unique
database and NOSUPERUSER NOBYPASSRLS role, applies migrations as its admin, grants
the runtime schema, then connects the application only as that restricted role.
The fixture drops its database and role at teardown. No manual database/role
preprovisioning is required. A second API pool uses that same restricted DSN.

Stripe SDK 15.x changes Event objects from the dictionary interface used by the
verified adapter and is not supported by this release. Upgrade only after a
separate compatibility review and full raw-body signature/lifecycle acceptance.

## Effective runtime grants

Readiness checks schema USAGE and migration-history SELECT, then checks every
required business-table privilege individually before reporting ready. Missing
grants return 503 without changing provider, migration or FORCE RLS gates.

- `payment_intents`: SELECT, INSERT, UPDATE.
- `payment_bindings`: SELECT, INSERT.
- `payment_webhook_inbox`: SELECT, INSERT.
- `payment_effects`: INSERT.
- `billing_checkouts`: SELECT, INSERT, UPDATE.
- `billing_subscriptions`: SELECT, INSERT, UPDATE.
- `billing_invoices`: SELECT, INSERT, UPDATE.
- `payment_refunds`: SELECT, INSERT, UPDATE.
- `payment_refund_effects`: INSERT.
- `payment_pending_webhooks`: SELECT, INSERT, UPDATE.

No DELETE/TRUNCATE grant is required by this readiness gate. Append-only effect
tables require only INSERT; bindings and webhook inbox require SELECT/INSERT.
Real PostgreSQL LOGIN-role regressions revoke and restore each required grant
and verify that the minimal runtime grant set remains ready.
