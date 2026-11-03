# 03 — User flows (API call flows per caller)

ไม่มี UI ใน repo นี้ "user flow" คือลำดับ API call ของแต่ละ caller ผ่าน edge gateway (`https://gateway.bookchaowalit.com/v1/platform-payment/...`) หรือ Railway URL ตรง

## Flow A — Write caller: one-time payment ถึง settlement

```mermaid
sequenceDiagram
  autonumber
  participant C as Caller with payments:write
  participant G as Edge gateway
  participant P as payment-api
  participant S as Stripe sandbox
  C->>G: POST /v1/platform-payment/v1/checkout, Bearer, Idempotency-Key
  G->>P: Forward Authorization, Idempotency-Key, body
  P->>S: Create Checkout Session, idempotency key platform-id
  S-->>P: session id and url
  P-->>C: 201 status pending, checkout_url
  Note over C,S: Browser pays on Stripe hosted page, the return URL is never treated as settlement
  S->>P: POST /v1/webhooks/provider, Stripe-Signature, direct to Railway URL
  P-->>S: 200 accepted
  C->>G: GET /v1/platform-payment/v1/payment-intents/id
  G->>P: Forward
  P-->>C: 200 status succeeded
```

ถ้าขั้น 3 timeout: ได้ 202 `status=unknown` แล้วเรียก `POST /v1/payment-intents/{id}/reconcile` (ห้ามใช้ Idempotency-Key ใหม่)

## Flow B — Refund caller

```mermaid
flowchart LR
  A["GET /v1/payment-intents/id: status succeeded"] --> B["POST /v1/payment-intents/id/refunds amount, Idempotency-Key, payments:refund"]
  B -->|"201 pending"| C["Wait for refund webhook"]
  B -->|"202 unknown"| D["POST /v1/refunds/id/reconcile"]
  D --> C
  B -->|"409"| E["Not refundable or exceeds available balance"]
  C --> F["GET /v1/refunds/id: succeeded or failed"]
```

## Flow C — Billing caller

```mermaid
flowchart LR
  A["POST /v1/billing/checkouts plan, quantity, billing:write"] --> B["Open checkout_url in browser"]
  B --> C["Webhooks create subscription incomplete, then lifecycle status"]
  C --> D["GET /v1/subscriptions and /v1/invoices, billing:read"]
  D --> E["POST /v1/subscriptions/id/cancel, Idempotency-Key"]
  E -->|"202 unknown"| F["POST /v1/subscriptions/id/cancel/reconcile"]
  E --> G["Signed subscription snapshot sets cancel_status confirmed"]
```

## Flow D — MCP hub (read-only)

```mermaid
flowchart LR
  H["MCP hub cell platform-payment"] --> T["identity client_credentials token, audience payment-api, payments:read billing:read"]
  T --> R["GET intents, refunds, subscriptions, invoices through gateway"]
  R -->|"any POST"| X["403 forbidden: scope missing"]
```

## Flow E — Operator

```mermaid
flowchart LR
  O["Operator shell with image or checkout"] --> M["python -m platform_payment migrate, DATABASE_MIGRATION_DSN"]
  O --> RP["python -m platform_payment replay-webhooks --limit 50, DATABASE_URL and provider env"]
  RP --> J["JSON: applied and pending counts"]
  O -->|"live key in env"| X["exit 2 before any DB connection"]
```

Sources: `src/platform_payment/app.py`, `refunds.py`, `billing_checkouts.py`, `subscriptions.py`, `__main__.py`, `pending_webhooks.py`, `API.md`, `docs/billing-runtime.md`, platform-api-gateway `c7b16d1` `API.md` + `src/book_api_gateway/edge.py` (`FORWARD_REQUEST` ไม่รวม `Stripe-Signature`, edge route ต้องมี Bearer), ops record: book-platforms-status (2026-11-03)

## Open questions

- Stripe webhook จะชี้ไปที่ Railway URL ตรง (gateway ต้องการ Bearer และไม่ส่ง `Stripe-Signature` ต่อ) หรือจะมี route เฉพาะ: ยังไม่ได้ลงทะเบียน endpoint ใด
- `replay-webhooks` ยังไม่มี schedule (`docs/billing-runtime.md`: "Schedule with backoff through the owning deployment") ไม่พบ cron ใน Railway project
