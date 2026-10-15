# Local verification: 2026-10-15

Correlation: `SHARED-INFRA-2026-10-15`. Stage **implemented-local**; contract
status `pilot`. No push, deploy, production cutover, live provider call, charge,
external message or credential discovery was performed.

Environment: Python 3.14.4, PostgreSQL 18, FastAPI 0.143.1, psycopg 3.3.6,
psycopg-pool 3.3.3, pytest 9.1.1, HTTPX 0.28.1. Provider libraries: boto3
1.43.114 and Stripe 14.4.1. Tests use a dedicated service database and a
NOSUPERUSER NOBYPASSRLS role. Every DB test asserts real FORCE RLS behavior;
workspaces are randomly generated. The shared core was loaded from its local
checkout for this development run; release artifact pinning belongs to the
parent integration/release step.

Commands run from this repository (interpreter in the isolated shared-infra
venv, `PYTHONPATH` containing this `src` and the sibling core `src`):

```bash
bash scripts/check.sh
python -m pytest -q
python -m pip wheel --no-deps --wheel-dir <disposable-wheel-output> .
git diff --check
```

`PLATFORM_TEST_DSN` was supplied explicitly for the fixture-admin disposable
DB; no secret-bearing local file was read. Absence of the DSN skips DB tests
and must not be represented as isolation/durability evidence.

## Results

- Original `bash scripts/check.sh`: **36 passed**, no skips.
- Complete `python -m pytest -q`: **50 passed**, no skips.
  Fourteen new runtime tests passed: real PostgreSQL workspace isolation and
  FORCE RLS; durable scoped idempotency/changed-key conflict; concurrent creation
  and webhook inbox/effects; stored provider-object routing; signature and
  amount/currency checks; stale/duplicate events; timeout reconciliation;
  concurrent reconcilers; 20-hour retry cutoff; immutable success effects;
  real Stripe SDK request payload/idempotency options and raw-body signatures;
  two independent API pools; slow webhook work leaves health responsive.
- Real Stripe SDK requests were intercepted locally, proving Checkout Sessions
  and PaymentIntent payloads, dynamic payment-method defaults, API version,
  provider idempotency and server-owned return URLs without network or charges.
- Signed sandbox webhook fixtures cover unpaid/paid checkout, stale signature,
  tampered raw bytes and live-event rejection. Live API keys are rejected.
- Wheel build and `git diff --check` passed.

## Red / green record

Tests were written before the runtime existed; first run reported missing
packages. The missing-config readiness assertion later failed with HTTP 200
before the service probe, then passed with HTTP 503. The real Stripe transport
check failed because the synchronous HTTPX client was absent; enabling bounded
synchronous calls made the same test pass.

The slow-webhook behavioral test was run **before** offloading synchronous
signature/database work: the concurrent health request exceeded its 0.5-second
fixture bound and raised `TimeoutError`. Moving processing into the bounded
FastAPI threadpool made that exact test pass, then the complete suite passed.

During test development the fixture initially reused one event ID with random
metadata across unrelated test payments; the production fingerprint correctly
rejected these changed bodies. The fixture now uses stable repeated payloads
and provider-object-specific event IDs. This was a fixture correction, not a
relaxation of durable event binding.

## Explicit limitations

One Starlette deprecation warning recommends HTTPX2 for future testclient
compatibility; current tests use HTTPX 0.28.1. No tests skipped in the recorded
DSN-enabled run. No real Stripe sandbox or Docker container was executed;
provider/API-version parity is an approved staging gate. No live mode exists.
The SDK version tested is 14.4.1, with API version 2026-08-26.dahlia configured;
release dependency pinning and sandbox acceptance remain parent-owned.

The success-effect marker is not an accounting ledger. Billing, subscriptions,
refunds/disputes, entitlement delivery and financial reconciliation are out of
scope. Old ambiguous creations beyond 20 hours require manual reconciliation.
Webhook-before-binding returns 409 for provider retry; this must be included in
provider retry/runbook configuration. Production cutover, backup/restore and
cross-service load evidence remain parent release gates.

The program-standard `PLATFORM_TEST_DSN` is an explicitly supplied disposable
**fixture admin** DSN with CREATE DATABASE/ROLE privileges. Each runtime fixture
uses `platform_backend_core.testing.temporary_database` to create a unique
database and NOSUPERUSER NOBYPASSRLS role, applies migrations as its admin, grants
the runtime schema, then connects the application only as that restricted role.
The fixture drops its database and role at teardown. No manual database/role
preprovisioning is required. A second API pool uses that same restricted DSN.

## Fixture correction and bounded billing extension

The fixture correction is a separate commit. The original 50-test one-time
suite passed using the program-standard disposable admin DSN after fixtures
were changed to create/grant/drop their own isolated databases and restricted
runtime roles. No manually provisioned database is required.

The final extended `python -m pytest -q` run with that same admin configuration
passed **66 tests, no skips**, including 16 added billing/refund tests. The
original `bash scripts/check.sh` separately passed all 36 tests with the extended
nine-interface contract and unchanged schema pin. Wheel build passed after the
extension; runtime files remain below 350 lines each. No provider network calls
or live charges were made. Starlette's existing HTTPX2 warning remains.

Behavioral RED before billing implementation: missing subscription checkout and
refund routes returned 404 instead of the specified 201/202/409 responses.
Durable-before-binding RED: the pending event table did not exist; the same test
then proved a signed unbound subscription event remains invisible to tenant
queries and operator replay applies it after Checkout association.
An additional invoice regression first showed a same-second late draft replacing
an open invoice; the guarded phase transition made the exact test pass.
A strict refund-event test first accepted JSON boolean true as integer one;
explicit integer validation changed that response from 200 to 400 without
releasing/applying the reservation. All full-suite results above are after fixes.

Migration001 checksum is asserted unchanged, and a real upgrade test applies
001, writes old intent/binding/inbox rows, applies only 002 and validates preserved
bindings plus zero refund balances. Other new evidence covers tampered catalog
inputs, saved plan snapshots, concurrent refunds that cannot exceed paid balance,
duplicate/out-of-order lifecycle/invoice/refund events, concrete scopes,
provider timeout with stable-key reconciliation and a 20-hour retry cutoff.
Actual Stripe subscription Checkout/cancel/refund requests and signed event
normalization are verified using locally intercepted HTTP transport.

Current scope and staged limitations are in [billing-runtime.md](billing-runtime.md).
The earlier out-of-scope statement for subscriptions/refunds describes the initial
one-time commit and is superseded by this bounded extension. Accounting ledger,
tax filing, disputes, entitlement delivery and out-of-band financial reconciliation
remain outside the implemented slice. Same-second conflicting subscription
snapshots stay pending for fresh provider evidence/manual review; multi-item,
shared-customer and plan-change flows fail closed. Live Stripe remains disabled.
Migration002 requires a compatible rollback image; do not restore a pre-002 image
or drop dedupe/balance state as a rollback shortcut.
