# Review guide (platform-payment)

The Codex Stop-hook review reads this file from the default branch, never from the change under
review. Owner-written; keep it under 16 KiB.

## What failure looks like here

Treat these as `[P0]`/`[P1]`:

- **Live mode**: a live-mode switch added, or a live Stripe key accepted (`docs/runtime.md`,
  `live_mode.py`). Test mode only (owner decision D8).
- **Double charge or double creation**: the idempotency key and its 60 s lease, 202 `unknown` with no
  automatic replacement, the 20 h retry cutoff (`docs/runtime.md`).
- **Webhooks**: raw-body signature check, tenant routing by binding (never by event metadata), amount
  and currency match, dedupe by event id and fingerprint, success is terminal (`adapters.py`).
- **Ledger**: balanced, append-only, refund cap and atomic rollback, all enforced in DB triggers;
  refund over-reservation (`docs/billing-runtime.md`).
- **RLS**: FORCE RLS, refusing SUPERUSER/BYPASSRLS roles, the outbox relay bypass policy
  (`outbox_migrations.py`).
- **Supply chain**: the `platform-backend-core` commit must match across the workflow, Dockerfile and
  `[tool.uv.sources]`; Actions stay SHA-pinned.

## Severity

- `[P0]` money charged twice or to the wrong tenant, live mode reachable, ledger unbalanced, webhook forgery accepted.
- `[P1]` a payment, refund or webhook flow that fails or reports the wrong state.
- `[P2]` edge case, or changed behaviour without a test.
- `[P3]` nit. **At most 3 nits.**

## Skip

- `uv.lock` hashes and generated metadata. Still review changed versions or sources and the
  `platform-backend-core` commit, which must match `[tool.uv.sources]` (`tests/test_build_hygiene.py`).
- `build/`, `*.egg-info`, `__pycache__/`, `.pytest_cache/`, `DOCPACK/pdf/`.
- Vendored `schema/` changes only from the canonical copy.

## Evidence

Every finding gives `file:line` and input -> wrong result. No finding without evidence from the diff
or the code it calls. Start the summary with the count, e.g. `2 findings (1 P1, 1 P3)`.
