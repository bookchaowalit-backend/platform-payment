# Product

<!-- impeccable:product-schema 1 -->

## Platform

contract (Book Platform boundary, `book-platform.contract.v1`)

## Users

- The owner and the agents that change the Book Platform: they read this boundary before
  touching the `payment` capability anywhere else.
- Other platform repositories and cells, which may depend on this boundary only through its
  versioned API or event contracts, never its database or private modules.

## Product Purpose

Own the `payment` capability (payment intent, checkout, billing, ledger, reconciliation) as a written, checked contract, so the capability can move
out of the `solo-empire` control plane later without consumers noticing.

Success: the contract stays valid against the pinned shared schema, the registry entry matches
it, and every gate in `fixtures/gates/` passes in CI.

## Positioning

A boundary contract with offline evidence plus a **test-mode-only** v1 runtime pilot
(`src/platform_payment`): payment intents, hosted checkout, subscriptions, bounded refunds
and a balanced settlement ledger against a Stripe sandbox. Not deployed; live Stripe keys
refuse startup and readiness.

## Operating Context

- Status: `pilot` (contract); DOCPACK verdict: `template`. The runtime is not activated or
  deployed; the legacy implementation is `infra/api/stripe.ts` in the parent registry and
  live Stripe webhooks stay with `bookchaowalit/platform-webhooks-worker`. Live activation
  and any webhook-ownership move need a separate owner decision (D8); see
  `docs/runtime.md`.
- CI: shared `ci-python` (Ruff and `scripts/check.sh` on Python 3.11 and 3.12) and the shared
  security workflow, on GitHub-hosted runners (public repository).
- Sentry does not apply: nothing runs in production. Add it in the change that activates a
  runtime.
- Runtime CI: `shared-runtime.yml` runs the runtime suite against PostgreSQL 16.

## Capabilities and Constraints

- `scripts/check.py`: validates `contract.json` against the vendored schema;
  `scripts/check_schema_pin.py`: the vendored schema matches the pinned canonical hash.
- No live provider integration, production database, credential or customer payload; the
  runtime's Stripe adapter accepts only test-mode keys.
- Out of scope: running the capability; it stays in its current implementation until parity
  and rollback evidence permit a cutover.

## Measures

- `scripts/check.sh` green on `main` (target: always).
- Drift between `contract.json` and the registry entry (target: 0).
- [ต้องยืนยัน] The cutover criteria for activating the runtime.
