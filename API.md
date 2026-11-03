# API Surface

Contract: `book-payment-platform` v1 (REST over HTTP, JSON errors). The
machine-readable interface list is `interfaces` in [`contract.json`](contract.json);
[`docs/CONTRACT-SURFACE.md`](docs/CONTRACT-SURFACE.md) documents each one, and
[`docs/runtime.md`](docs/runtime.md) and [`docs/billing-runtime.md`](docs/billing-runtime.md)
hold the full request/response bounds.

Hosted (2026-11-03, test mode only): `https://payment-api-production-3a91.up.railway.app`,
and through the edge gateway at `https://gateway.bookchaowalit.com/v1/platform-payment`.
Live Stripe keys (`sk_live_`/`rk_live_`) refuse startup, the CLI and readiness.

## Authentication

Tenant routes need `Authorization: Bearer <RS256 token>` issued by platform
identity (`https://identity-api-production-fad1.up.railway.app`, JWKS
`/.well-known/jwks.json`) with audience `payment-api`. The workspace comes only
from the token's `ws` claim. Missing or invalid token returns 401, missing scope
403, another workspace's ID 404. Unknown body fields return 422.

| Scope | Permission |
|---|---|
| `payments:read` | `payment.payment:read` |
| `payments:write` | `payment.payment:write` |
| `payments:refund` | `payment.refund:write` |
| `billing:read` | `payment.billing:read` |
| `billing:write` | `payment.billing:write` |

## Routes

| Interface | Method | Path | Scope | Behavior |
|---|---|---|---|---|
| - | `GET` | `/healthz` | Public | Process liveness |
| - | `GET` | `/readyz` | Public | 200 only with a test-mode provider, database grants and RLS in place; otherwise 503 |
| `payment.http.v1-runtime` | `POST` | `/v1/payment-intents` | `payments:write` | Off-session intent; `Idempotency-Key` required |
| `payment.http.v1-runtime` | `POST` | `/v1/checkout` | `payments:write` | Hosted checkout; `Idempotency-Key` required |
| `payment.http.v1-runtime` | `POST` | `/v1/payment-intents/{id}/reconcile` | `payments:write` | Resume an unknown creation with the saved provider key |
| `payment.http.v1-runtime` | `GET` | `/v1/payment-intents` | `payments:read` | List (limit 1..100, offset 0..10000) |
| `payment.http.v1-runtime` | `GET` | `/v1/payment-intents/{id}` | `payments:read` | Saved state |
| `payment.http.v1-runtime` | `POST` | `/v1/webhooks/provider` | Provider signature over the raw body | Only a verified, bound webhook records settlement |
| `payment.http.billing` | `POST` | `/v1/billing/checkouts` | `billing:write` | `{plan,quantity}`; `Idempotency-Key` required |
| `payment.http.billing` | `GET` | `/v1/billing/checkouts/{id}` | `billing:read` | Saved state, plan snapshot, checkout URL |
| `payment.http.billing` | `POST` | `/v1/billing/checkouts/{id}/reconcile` | `billing:write` | Retry unresolved creation |
| `payment.http.billing` | `GET` | `/v1/subscriptions`, `/v1/subscriptions/{id}` | `billing:read` | Plan, quantity, status, period end |
| `payment.http.billing` | `POST` | `/v1/subscriptions/{id}/cancel` | `billing:write` | Cancel at period end only; `Idempotency-Key` required |
| `payment.http.billing` | `POST` | `/v1/subscriptions/{id}/cancel/reconcile` | `billing:write` | Reconcile an ambiguous cancellation |
| `payment.http.billing` | `GET` | `/v1/invoices`, `/v1/invoices/{id}` | `billing:read` | Amount due/paid, currency, status, periods |
| `payment.http.refunds` | `POST` | `/v1/payment-intents/{id}/refunds` | `payments:refund` | `{amount}`; `Idempotency-Key` required; paid one-time intents only |
| `payment.http.refunds` | `GET` | `/v1/payment-intents/{id}/refunds` | `payments:read` | Refunds of that intent |
| `payment.http.refunds` | `GET` | `/v1/refunds/{id}` | `payments:read` | Saved refund state |
| `payment.http.refunds` | `POST` | `/v1/refunds/{id}/reconcile` | `payments:refund` | Retry an unresolved refund |

Operator CLI: `python -m platform_payment replay-webhooks --limit 50`
(`payment.cli.webhook-replay`).

## Responses and idempotency

Idempotency keys are 1..128 characters. A changed payload under an existing key
returns 409. Responses are 201 for a newly bound operation, 200 for a resolved
replay and 202 while the provider outcome is creating or unknown; replaying the
original key never starts another provider call. Reconciliation is refused after
20 hours (409) so it stays within the provider's idempotency retention. Amounts
are integers in minor units, 1..999,999,999. No card data, provider keys or
client secrets are accepted or returned.

## Callers

The remote MCP hub (`platform-payment` cell, identity client `mcp-hub`) holds
only `payments:read` and `billing:read`, so it can read but never create, refund
or cancel.
