# 06 — C4 Container

```mermaid
C4Container
  title platform-payment containers
  System_Ext(gw, "Edge API gateway", "Cloudflare proxy plus gateway service")
  System_Ext(stripe, "Stripe sandbox", "API and webhooks")
  System_Ext(idp, "platform-identity", "JWKS")
  System_Ext(sentry, "Sentry", "platform-payment project")
  Container_Boundary(rw, "Railway project platform-payment, asia-southeast1, sleep on") {
    Container(api, "payment-api", "Python 3.12, FastAPI, uvicorn, platform-backend-core 21d5f44", "python -m platform_payment serve, port 8080, UID 65532")
  }
  Container(cli, "Operator CLI", "Same image, command override", "migrate, replay-webhooks")
  ContainerDb(db, "Neon PostgreSQL", "PostgreSQL, project platform-payment", "11 FORCE RLS tables plus platform_schema_migrations")
  Rel(gw, api, "HTTPS")
  Rel(stripe, api, "POST /v1/webhooks/provider")
  Rel(api, stripe, "HTTPS via HTTPXClient, 5 s, 1 retry")
  Rel(api, idp, "HTTPS JWKS, refresh at most every 60 s")
  Rel(api, db, "psycopg pool max 8, runtime role")
  Rel(cli, db, "migrate as owner DSN, replay as runtime DSN")
  Rel(api, sentry, "HTTPS events")
```

Sources: `Dockerfile`, `src/platform_payment/__main__.py`, `adapters.py`, `migrations.py`, `billing_migrations.py`, `ledger_migrations.py`, `_sentry.py`, platform-backend-core `21d5f44` `auth.py` (`_JWKS`) + `db.py`, Railway API read 2026-11-03 (service `payment-api`: Dockerfile builder, 1 replica `asia-southeast1-eqsg3a`, `sleepApplication` true, healthcheck `/healthz`), ops record: book-platforms-status (2026-11-03) (Neon `platform-payment`, role `payment_runtime`)

## Open questions

- ยังไม่มี Railway cron service สำหรับ `replay-webhooks`; CLI ในภาพคือการรันด้วยมือ
- `migrate` รันจากที่ใดสำหรับ hosted DB (local `git archive` หรือ `railway run`) ไม่มีบันทึกใน repo
