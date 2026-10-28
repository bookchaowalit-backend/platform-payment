# Payment API runtime (implemented-local)

Correlation: `SHARED-INFRA-2026-10-15`. This pilot provides payment intent and
hosted checkout creation, durable idempotency, verified webhook inbox and one
immutable payment-success effect, plus a balanced double-entry settlement
ledger (migration 003, see [Ledger](#ledger)). The bounded
subscription/invoice/refund extension is documented in
[billing-runtime.md](billing-runtime.md). It does not implement tax filing,
disputes, entitlement delivery, general accounting or financial reconciliation.
Legacy consumers remain unchanged; no live payment mode or production cutover is
enabled.

## Test-mode only: the live-mode boundary

This runtime is **test-mode only** and cannot run against live Stripe. PR #1 was
closed under owner decision D8 because it overlaps the live Stripe path that
`bookchaowalit/platform-webhooks-worker` serves today and live activation was
unreviewed. `platform_payment.live_mode` enforces the boundary:

- Startup (`create_app`) raises `LiveModeRefused` when **any** environment
  variable holds a value starting with `sk_live_` or `rk_live_`, even with an
  injected provider or an otherwise incomplete provider configuration (which
  would otherwise start with the provider disabled), or when the provider
  reports `livemode` other than `False`.
- Every CLI command (`serve`, `migrate`, `replay-webhooks`) exits with status 2
  before opening a database connection.
- `/readyz` returns 503 if a live key appears in the environment, or the
  provider reports live mode, after startup.
- `StripeProvider` accepts only `sk_test_`/`rk_test_` keys and reports
  `livemode = False`; verified webhook events with `livemode` other than
  `false` are rejected with 400.

Errors name the offending variables, never their values. There is no switch,
flag or environment variable that enables live mode, and none may be added
here. **Live activation, and any move of webhook ownership away from
`bookchaowalit/platform-webhooks-worker`, each need a separate owner decision**
with independent payment/security review; until then that worker stays the
only live Stripe webhook receiver and this service must not be registered as a
live Stripe endpoint.

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
`payment_webhook_inbox`, `payment_effects`, `payment_ledger_entries` (full list
under [Effective runtime grants](#effective-runtime-grants)). Never grant table TRUNCATE or owner
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
boundary for future fulfillment; the money movement itself is posted to the
[ledger](#ledger) in the same transaction.

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

Build the image with `docker build .` (see "Container image" below; the core
commit is fixed in the `Dockerfile`, there is no build argument). Run non-root, one process per container, concurrency 64, pool max 8
and request timeout 30s. Bound all replicas to the database connection budget.
Inject credentials via the deployment secret store. Egress allows approved
Stripe/JWKS endpoints; ingress protects metrics and has a separate signed webhook
route. Keep proxy logs free of bodies, bearer headers and signed URLs.

Before release: a separate owner decision on live activation and webhook
ownership (see the live-mode boundary above); independent payment/security review; approved Stripe sandbox
parity with actual signatures, hosted checkout and asynchronous methods;
restart/concurrency/backup-restore evidence; legacy consumer parity and rollback.
Back up the service DB before explicit migration. Roll back only to a reviewed image compatible with migration003, preserving
intents, bindings, inbox, pending events, refund reservations, effects and ledger journals. Do not drop dedupe/history or
reset provider keys. Manual reconciliation is required for old unresolved items.
No destructive migration or production activation is authorized here.

### Container image

`docker build .` works from a clean checkout; no pre-staged `wheelhouse/` is
needed. The `Dockerfile` is two stages:

1. `build` (`python:3.12-slim` plus `git`): `pip wheel` builds this service's wheel
   and `platform-backend-core` from the same reviewed commit CI installs
   (`d00e0119c2e47da3942d9c7b4b88828953f8bc5a`), with all transitive dependencies.
2. `runtime` (`python:3.12-slim`, no git, no build tools): installs only those
   wheels with `--no-index`, runs as UID/GID `65532`, `PORT=8080`, and keeps
   `CMD ["python", "-m", "platform_payment", "serve"]`. `migrate` (with
   `DATABASE_MIGRATION_DSN`) and `replay-webhooks` run from the same image with a
   command override; every command still refuses live Stripe mode.

`.dockerignore` keeps `.git`, `.venv`, build output and `.env*` out of the build
context. CI job `image` in `shared-runtime.yml` runs `docker build` on every push
and pull request, then checks that the image imports the service and runs as a
non-root user. It also
runs `python -m platform_payment --help` (the `serve`, `migrate` and
`replay-webhooks` commands) and starts `serve` with a probe variable built inside
the container from `LIVE_KEY_PREFIXES`, which must exit 2 with the live-mode
refusal naming the variable but not its value; no Stripe-shaped literal is
written into the workflow. It never pushes or deploys. `tests/test_build_hygiene.py`
(part of `scripts/check.sh`) fails if the Dockerfile copies a git-ignored path,
stops being multi-stage, pins a core commit other than the workflow's, runs the
final stage as root or changes the CMD, if CI stops building the image, or if any
workflow `uses:` is not pinned to a 40-hex commit SHA (tags go in a trailing
comment). Upgrading the core means changing the commit in the workflow, the
Dockerfile and `[tool.uv.sources]` together; the tests fail on any mismatch.

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
- `payment_effects`: SELECT, INSERT (`INSERT ... RETURNING` reads the returned column).
- `billing_checkouts`: SELECT, INSERT, UPDATE.
- `billing_subscriptions`: SELECT, INSERT, UPDATE.
- `billing_invoices`: SELECT, INSERT, UPDATE.
- `payment_refunds`: SELECT, INSERT, UPDATE.
- `payment_refund_effects`: INSERT.
- `payment_pending_webhooks`: SELECT, INSERT, UPDATE.
- `payment_ledger_entries`: SELECT, INSERT (the balance trigger reads the
  journal back as the runtime role).

No DELETE/TRUNCATE grant is required by this readiness gate. Append-only effect
tables require only INSERT; bindings and webhook inbox require SELECT/INSERT.
Real PostgreSQL LOGIN-role regressions revoke and restore each required grant
and verify that the minimal runtime grant set remains ready.

## Ledger

Migration `003-ledger` adds `payment_ledger_entries`, a double-entry journal in
integer minor units (`bigint`, 1..999,999,999, lowercase ISO currency). Each
verified settlement fact posts exactly one journal, keyed by
`(workspace_id, source_kind, source_id, account, direction)`:

| Fact | Debit | Credit |
|---|---|---|
| `payment_succeeded` (intent or checkout paid) | `provider_clearing` | `payment_revenue` |
| `refund_succeeded` | `payment_refunds` | `provider_clearing` |

Invariants enforced in PostgreSQL, not only in Python:

- **Balanced:** an `AFTER INSERT ... FOR EACH STATEMENT` trigger over the
  transition table rejects (SQLSTATE 23514) any journal in the statement with
  fewer than two legs, debits != credits, more than one currency, or legs for
  different intents. All legs of a journal are written in one INSERT, so
  balance per statement implies balance overall; RLS never hides the rows being
  checked.
- **Refunds bounded by the captured amount:** the same trigger rejects a
  posting when an intent's `payment_refunds` debits would exceed its
  `payment_revenue` credits. This backs up the API-level reservation check and
  the `payment_refund_balance` constraint on `payment_intents`.
- **Append-only:** UPDATE, DELETE and TRUNCATE raise; corrections are new
  compensating journals (a refund is one), never edits.
- **Idempotent:** a journal is posted only when its effect row is newly
  inserted, and the primary key rejects a second journal for the same source.
- **Tenant isolation:** ENABLE + FORCE RLS on `workspace_id`; `/readyz` checks it.

### Ledger rollback (migration gate)

The inbox insert, state transition, effect row and journal commit in one
transaction. If posting fails (a bug, an unbalanced journal, an over-refund)
the whole settlement rolls back: no inbox row, no effect, no journal, the
intent keeps its prior status and a refund keeps its reservation. The webhook
answers a redacted 500, the provider redelivers, and the redelivery applies
exactly once. `runtime_tests/test_ledger.py` proves this for payments and
refunds by forcing a one-legged journal, then redelivering after the fault is
removed.

Migration 003 backfills journals from existing `payment_effects` and
`payment_refund_effects`. The schema owner lifts FORCE RLS on those two tables
inside the migration transaction only, and restores it before commit. Images
before migration003 settle without posting journals, so they are not rollback
targets; roll back to a reviewed migration003-compatible image, or restore the
pre-migration backup and re-apply 003 (its backfill rebuilds journals from the
effect rows).

Subscription invoices are provider metadata and are **not** posted to this
ledger; out-of-band provider refunds and disputes are not posted either.

## Verification 2026-10-29 (branch `payment-runtime`)

PR #1 (`codex/shared-infrastructure-20261015`, 7 commits) rebased onto `main`
`99c17c4` without conflicts. `main`'s contract checks and CI (shared CI v1.1.1:
Ruff, `scripts/check.sh` on Python 3.11 and 3.12, security scan incl.
gitleaks) are kept; `shared-runtime.yml` runs the runtime suite against a
`postgres:16` service.

Changes beyond the rebase:

- Ruff 0.17.1 (the CI pin) clean: FastAPI markers and `require_scope` in
  `extend-immutable-calls`, sorted imports, unused names removed, deliberate
  blind excepts carry a `noqa: BLE001` reason, two signature asserts narrowed
  to `stripe.SignatureVerificationError`. `[tool.uv.sources]` pins
  `platform-backend-core` to the reviewed commit (the name is not on PyPI).
- No Stripe-shaped literal remains in tests: `runtime_tests/stripe_keys.py`
  assembles synthetic fixture keys at runtime.
- `contract.json`: auth declared for `payment.http.billing` and
  `payment.http.refunds` (`main`'s boundary test failed on both after the
  rebase), and the new `payment.table.ledger` interface.
- Live-mode refusal (above) and the ledger (above).

Environment: Python 3.12, PostgreSQL 16.2 (local `pgserver`, no docker),
`platform-backend-core` `d00e0119`. No network calls, no Stripe account, no
credentials; every key in the suite is a synthetic `*_test_*`/`*_live_*`
fixture string assembled at runtime and never sent anywhere.

RED (tests written first, run before the implementation):

- `runtime_tests/test_live_mode.py`: 16 failed. Startup with a live key (6
  variants) and with a live-reporting provider: `DID NOT RAISE ValueError`;
  incomplete config plus live key started quietly; CLI `serve`, `migrate`,
  `replay-webhooks` exited 1 with generic messages instead of 2 with the
  refusal; `/readyz` stayed 200 after a live key appeared; `StripeProvider` had
  no `livemode`.
- `runtime_tests/test_ledger.py` + the readiness-grant test extended with the
  ledger table: 7 failed, 29 errors (`UndefinedTable:
  payment_ledger_entries`, `ImportError: ledger`).
- `tests/test_payment_boundary.py` (from `main`): 2 subtests failed, billing and
  refund HTTP interfaces lacked `auth`.

GREEN:

- `ruff check .` (0.17.1): all checks passed.
- `bash scripts/check.sh`: contract + schema pin OK, 42 unittest tests OK.
- `python -m pytest -q` with `PLATFORM_TEST_DSN` on the local server: 128
  passed (86 runtime tests against real PostgreSQL + the 42 contract tests).
- `test_migration002_upgrades_existing_binding_and_inbox_without_drift` now
  expects migrations 002 and 003 to apply on top of 001.

Not proven here: the backfill was exercised with a superuser fixture admin,
which bypasses RLS anyway; the NO FORCE path for a non-superuser schema owner
is a staging check.

## Staging plan (not executed)

Nothing below has been run; no environment, secret or Stripe endpoint was
created or changed.

1. Owner decision first: D8 keeps live Stripe on
   `bookchaowalit/platform-webhooks-worker`. Staging stays on a Stripe
   **sandbox** account only; this service is never registered as a live
   endpoint.
2. Provision a disposable staging PostgreSQL 16 database with a schema-owning
   migrator role (NOSUPERUSER) and a NOSUPERUSER NOBYPASSRLS runtime role
   holding exactly the grants above.
3. Back up, then `python -m platform_payment migrate` with
   `DATABASE_MIGRATION_DSN`; confirm migrations 001-003 and FORCE RLS on all
   eleven tables, and that the 003 backfill ran as the non-superuser owner.
4. Inject sandbox-only secrets (`rk_test_` restricted key, sandbox webhook
   signing secret, HTTPS return URLs) from the secret store; start one replica
   from the `docker build .` image (see "Container image"); `/readyz` must be 200.
5. Negative check: set a live-shaped key on the staging service and confirm the
   process refuses to start (exit 2) and `/readyz` is 503; remove it.
6. Sandbox provider parity: real signed events for intents, hosted checkout,
   asynchronous methods, refunds and subscriptions; duplicate and out-of-order
   delivery; compare with the legacy path's recorded behaviour.
7. Ledger: reconcile per intent that `provider_clearing` equals
   `amount - refunded_minor`, debits equal credits overall, and a forced
   posting failure in staging leaves no partial rows and recovers on
   redelivery.
8. Rollback rehearsal: restore the pre-migration backup into a scratch DB,
   re-apply 003 and compare journals.
9. Record the evidence in the parent registry; any cutover or live activation
   is a separate owner decision.

## Review fixes — 2026-10-29

Independent review (changes requested) found one medium and two low issues; the
new tests failed first (3 of 3) and pass now.

- The documented least-privilege role could pass `/readyz` yet fail every payment
  settlement: `INSERT ... RETURNING` on `payment_effects` needs SELECT. Readiness,
  the grant list above and the readiness test now require SELECT and INSERT, and a
  test records an effect as that exact role (it failed with permission denied first).
- An injected provider must declare `livemode = False`; one that does not say is
  refused at startup and in readiness (fail closed).
- A `payment_intent.*` event whose object is still unbound after 3 deliveries (for
  example the intent under a Checkout session) is acknowledged as ignored instead of
  failing with 409 until Stripe disables the endpoint. The first two deliveries still
  return 409, which covers the short window before a new binding commits.

Full suite: 132 passed against PostgreSQL 16.2, Python 3.12.

## Build hygiene — 2026-10-29 (branch `build-hygiene`)

Ported from `platform-developer` `8047108`: the `Dockerfile` is now the
multi-stage clean-checkout build under "Container image", every workflow action
is pinned to a commit SHA (`11d5960a...` = `actions/checkout` v4,
`a26af69b...` = `actions/setup-python` v5, tag in a trailing comment), and the
new CI job `image` builds the image without pushing it. Error handling is
unchanged.

RED (test written first, run against the old files): `python3 -m unittest
tests.test_build_hygiene`: 5 of 8 failed (unpinned `actions/checkout@v4` / `actions/setup-python@v5` in `shared-runtime.yml`; one `FROM`; no core commit in the Dockerfile, which took a `CORE_REQUIREMENT` build argument; no `docker build` step; no image check of the CLI and live-mode refusal). The checker self-test, the clean-checkout check (the old file copied only tracked paths) and the non-root `USER` (then UID 10001) passed.

GREEN: `bash scripts/check.sh` 50 unit tests OK; `ruff check .` clean.
The database tests run in the CI `runtime` job against PostgreSQL 16; the image
build is verified only by the CI `image` job (no local Docker). Nothing was
pushed to a registry or deployed.
