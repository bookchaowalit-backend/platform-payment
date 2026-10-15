# platform-payment

`Payment` platform boundary for the Book Platform portfolio.

## Scope

This repository owns the `payment` capability: payment intent, checkout, billing, ledger, reconciliation.
This repository now owns a separately packaged FastAPI/PostgreSQL payment API,
a sandbox-only Stripe adapter and durable webhook inbox. Runtime stage is
implemented-local. Existing legacy consumers are unchanged; no production
activation or live payment is authorized by this pilot.

## Boundary

- Owner: `bookchaowalit-backend`
- Repository: `platform-payment`
- Target remote: `https://github.com/bookchaowalit-backend/platform-payment.git` (published; runtime not activated)
- Contract: `book-platform.contract.v1`
- Status: `pilot`
- Data owner: the platform boundary identified in `contract.json`

The platform communicates through versioned API or event contracts. Consumers
must not import another platform's database, migration, or private runtime
module. `solo-empire` remains the control plane and compatibility adapter until
parity and rollback evidence permit a cutover.

## Local verification

Run the original contract checks from this repository:

```bash
bash scripts/check.sh
```

The check validates `contract.json` against
[`schema/book-platform.contract.v1.schema.json`](schema/book-platform.contract.v1.schema.json),
confirms that the README and [`docs/CONTRACT-SURFACE.md`](docs/CONTRACT-SURFACE.md)
agree with the contract, and runs the unit tests in `tests/`. GitHub Actions
runs the same command on every push and pull request
(`.github/workflows/check.yml`).

`scripts/check_schema_pin.py` fails when the vendored schema no longer
matches the sha256 pinned in `schema/book-platform.contract.v1.schema.json.sha256`
(the canonical copy lives in `bookchaowalit-backend-core/contracts/`; change it
there first, then copy the schema and its pin here). Pass
`--canonical ../platform-backend-core` to also compare with a local
checkout. `python3 scripts/check_registry_alignment.py --solo-empire ../solo-empire`
compares this contract with the parent platform registry (local only; read-only).

The original check validates repository shape and contract metadata. It does not
claim deployment, provider connectivity, data migration, or production
readiness.

## Contract surface

Dependencies: `identity`, `security`, `observability`. The interfaces this boundary owns or consumes, with
their auth, idempotency and error behaviour, are listed in
[`docs/CONTRACT-SURFACE.md`](docs/CONTRACT-SURFACE.md). Planned work is tracked
in [`docs/UPGRADE-PLAN.md`](docs/UPGRADE-PLAN.md).

## Migration gate

Before activating a remote or changing a consumer, add sanitized fixtures for
success, duplicate delivery, timeout and provider failure; prove tenant/privacy
isolation; compare the old and new contract; and rehearse rollback on a
disposable state store. Record the evidence in the parent platform registry.

## Shared API runtime

Stage: **implemented-local**, correlation `SHARED-INFRA-2026-10-15`.
The HTTP API and explicit migrations use the separately installed
`platform-backend-core` distribution. The hub is not a runtime dependency.
See [API, configuration and release runbook](docs/runtime.md) and
[local verification evidence](docs/verification-2026-10-15.md).

Install a reviewed core wheel/revision first, then `pip install -e '.[test]'`.
Run `python -m platform_payment migrate` explicitly with `DATABASE_MIGRATION_DSN`,
then `python -m platform_payment serve` with `DATABASE_URL` and JWT configuration.
Run `python -m pytest` with a disposable fixture-admin `PLATFORM_TEST_DSN`
to exercise the complete original and new suite. Database tests explicitly skip
when that DSN is absent. These skips do not establish durability or isolation.

The program-standard `PLATFORM_TEST_DSN` is an explicitly supplied disposable
**fixture admin** DSN with CREATE DATABASE/ROLE privileges. Each runtime fixture
uses `platform_backend_core.testing.temporary_database` to create a unique
database and NOSUPERUSER NOBYPASSRLS role, applies migrations as its admin, grants
the runtime schema, then connects the application only as that restricted role.
The fixture drops its database and role at teardown. No manual database/role
preprovisioning is required. A second API pool uses that same restricted DSN.
