# 21 — Incident response

ไม่มี `RUNBOOK.md` ใน repo; ขั้นตอนด้านล่างประกอบจาก signal ที่มีจริง (ดู 20) และคำสั่ง/กฎใน `docs/runtime.md` + `docs/billing-runtime.md`

```mermaid
flowchart TD
  subgraph Detect["Alert sources today"]
    A1["Sentry issue in platform-payment"]
    A2["GitHub Actions failure email on push"]
    A3["Caller sees 5xx or 202 unknown"]
    A4["Railway deploy healthcheck fails"]
  end
  A1 --> T["Triage: owner"]
  A2 --> T
  A3 --> T
  A4 --> T
  T --> Q{"Kind"}
  Q -->|"readyz 503"| R1["Check provider vars, live key present, grants, migration history, FORCE RLS"]
  Q -->|"202 unknown intent, refund, checkout, cancel"| R2["POST reconcile with same id within 20 h"]
  Q -->|"older than 20 h"| R3["Manual Stripe-side reconciliation, never create a replacement"]
  Q -->|"webhook 409 unbound"| R4["Let Stripe retry, then replay-webhooks --limit 50"]
  Q -->|"webhook 500 posting fault"| R5["Fix bug, redeploy; Stripe redelivery applies once"]
  Q -->|"live key refusal exit 2"| R6["Remove the named variable, never add a live switch"]
  Q -->|"bad deploy"| R7["Redeploy a reviewed migration003-compatible image"]
  R1 --> F["Fix and verify healthz and readyz"]
  R2 --> F
  R3 --> F
  R4 --> F
  R5 --> F
  R6 --> F
  R7 --> F
  F --> P["Postmortem: no template or location defined in repo"]
```

Sources: `docs/runtime.md` §API, idempotency and ambiguous outcomes + §Deployment and rollback gates + §Ledger rollback, `docs/billing-runtime.md` §Durable events, `src/platform_payment/app.py`, `live_mode.py`, `pending_webhooks.py`, `__main__.py`, `_sentry.py`, Railway API read 2026-11-03 (healthcheck), ops record: book-platforms-status (2026-11-03)

## Gaps

- ไม่มี Telegram alert path: service ไม่อยู่ใน uptime worker และไม่มี heartbeat
- ไม่มี runbook / postmortem template ใน repo
- ไม่มี on-call นอกจาก owner

## Open questions

- severity ของ payment incident และเวลาตอบสนองที่ต้องการ
- ใครมีสิทธิ์ทำ manual Stripe-side reconciliation และบันทึกผลที่ไหน
