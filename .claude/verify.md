# Verify recipe (platform-payment)

The `verify` skill follows this file. The checks are the ones CI runs.

## Checks

```bash
bash scripts/check.sh            # check.py, check_schema_pin.py, unittest
uv sync --frozen --all-extras
uvx ruff@0.17.1 check . && uvx ruff@0.17.1 format --check .
uv run mypy
# database tests need a throwaway Postgres 16 you started yourself:
PLATFORM_TEST_DSN=postgresql://localhost:5433/payment_scratch \
  uv run python -m pytest -q --cov --cov-report=term --cov-fail-under=87
uv build --wheel --out-dir dist .
```

Without `PLATFORM_TEST_DSN` the database tests skip: then say so, the money paths were not tested.
Stripe calls go through an intercepted transport; no network.

## End to end

`python -m platform_payment migrate` and `serve` against the same local database, Stripe **test**
keys only, then `curl -fsS localhost:8080/healthz` and `/readyz`. For webhook changes, replay a
signed test event twice and show it is applied once.

## Never

- `migrate`, `replay-webhooks` or `relay-outbox` against production (`payment-relay` publishes to
  Kafka `commerce.events.v1`).
- A live Stripe key anywhere, or registering this service as a live webhook endpoint.
