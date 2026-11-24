# Verify recipe (platform-payment)

The `verify` skill follows this file. The checks are the ones CI runs.

## Checks

```bash
set -e
bash scripts/check.sh            # contract job: check.py, check_schema_pin.py, unittest
uv sync --frozen --all-extras --python 3.12
uvx ruff@0.17.1 check .          # CI has format-check: false; no `ruff format --check`
uv run --no-sync mypy
# PLATFORM_TEST_DSN: your own disposable Postgres 16 *fixture admin* DSN (CREATE DATABASE and
# CREATE ROLE; docs/runtime.md "fixture admin"). A plain app role errors in the fixture.
: "${PLATFORM_TEST_DSN:?export a disposable fixture-admin DSN first}"
uv run --no-sync python -m pytest -q --cov --cov-report=
uv run --no-sync python -m coverage report --precision=2 --fail-under=87   # CI gate, not rounded
uv build --wheel --out-dir dist .
```

No disposable DSN: run `uv run --no-sync python -m pytest -q` without the coverage gate (the
database tests skip, coverage drops) and say so: the money paths were not tested. Stripe calls go
through an intercepted transport; no network.

`Dockerfile` or dependency changes: also run what `.github/workflows/shared-runtime.yml` runs
(`docker build --pull --tag platform-payment:ci .`, the import check, `id -u` != 0, the CLI and
live-mode refusal checks), or say the change waits for that job.

## End to end

Local database only, two DSNs as in `docs/runtime.md` "Install, migrations and configuration":
`DATABASE_MIGRATION_DSN` (schema owner) for `uv run --no-sync python -m platform_payment migrate`,
then grant the runtime role the list under "Effective runtime grants"; `DATABASE_URL` for `serve`
is that runtime role (NOSUPERUSER NOBYPASSRLS: the service refuses anything else). Set `JWT_ISSUER`,
`JWT_AUDIENCE`, `JWT_PUBLIC_KEY` (a throwaway key pair), Stripe **test** `PAYMENT_STRIPE_API_KEY` and
`PAYMENT_STRIPE_WEBHOOK_SECRET` (`/readyz` is 503 without a provider), the two `https` return URLs.
Start `uv run --no-sync python -m platform_payment serve &`, keep its PID, wait for the port, then
`curl -fsS localhost:8080/healthz` and `/readyz`, and stop it. For webhook changes, replay a signed
test event twice and show it is applied once.

## Never

- `migrate`, `replay-webhooks` or `relay-outbox` against production (`payment-relay` publishes to
  Kafka `commerce.events.v1`).
- A live Stripe key anywhere, or registering this service as a live webhook endpoint.
