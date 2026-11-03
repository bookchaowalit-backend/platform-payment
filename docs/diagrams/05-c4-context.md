# 05 — C4 Context

```mermaid
C4Context
  title platform-payment system context (2026-11-03)
  Person(owner, "Owner", "Person token from platform identity")
  System_Ext(hub, "Remote MCP hub", "Cell platform-payment, read-only scopes")
  System_Ext(gw, "Edge API gateway", "gateway.bookchaowalit.com, route platform-payment")
  System(pay, "platform-payment", "payment-api: intents, checkout, billing, refunds, ledger; test mode only")
  System_Ext(idp, "platform-identity", "RS256 issuer, JWKS, audience payment-api")
  System_Ext(stripe, "Stripe sandbox", "PaymentIntents, Checkout, Subscriptions, Refunds, signed webhooks")
  SystemDb_Ext(neon, "Neon PostgreSQL", "Project platform-payment")
  System_Ext(sentry, "Sentry", "Project platform-payment, errors only")
  System_Ext(legacy, "Legacy payment path", "solo-empire infra/api and platform-webhooks-worker, live Stripe")
  Rel(owner, gw, "HTTPS, Bearer")
  Rel(hub, gw, "HTTPS, client_credentials token")
  Rel(gw, pay, "HTTPS, forwards Bearer and Idempotency-Key")
  Rel(pay, idp, "Fetch JWKS")
  Rel(pay, stripe, "Stripe SDK 14.4.1")
  Rel(stripe, pay, "Signed webhook")
  Rel(pay, neon, "SQL as restricted role")
  Rel(pay, sentry, "Error events, no free text")
```

Sources: `API.md`, `src/platform_payment/__main__.py`, `adapters.py`, `stripe_billing.py`, `_sentry.py`, `docs/runtime.md` §Test-mode only, `docs/CONTRACT-SURFACE.md`, `pyproject.toml`, ops record: book-platforms-status (2026-11-03)

หมายเหตุ: `legacy` ไม่มีเส้นเชื่อมกับ platform-payment เลย (ไม่มี import หรือ call ระหว่างกัน) เป็นระบบที่ live อยู่และจะถูกแทนเมื่อ cutover; Stripe webhook ยังไม่ได้ลงทะเบียนชี้มา service นี้

## Open questions

- service ไหนจะเรียก payment-api แบบ write เป็นรายแรก (commerce? offers cell?) ไม่พบใน repo
