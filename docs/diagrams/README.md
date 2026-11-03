# Core 22 SDLC diagram pack — platform-payment

ชุดภาพ Core 22 ตาม SDLC Diagram Catalog v2 วาดจากโค้ดและ config ใน repo นี้ (ณ `origin/main` `065db9c`, 2026-11-03)
repo นี้คือ backend platform service `payment`: FastAPI + PostgreSQL (FORCE RLS) บน `platform-backend-core` `21d5f44`, Stripe **sandbox only** (live key ปฏิเสธที่ startup, CLI และ readiness; owner decision D8)
Hosted บน Railway (`payment-api`, Singapore, sleep) ตั้งแต่ 2026-11-03 แต่ `/readyz` 503 จนกว่าจะมี sandbox key

ทุกไฟล์มี `Sources:` ใต้ภาพ ข้อมูลที่ไม่อยู่ใน repo อ้างเป็น `ops record: book-platforms-status (2026-11-03)`, Railway API read (ชื่อ variable เท่านั้น ไม่อ่านค่า secret) หรือ HTTP probe วันเดียวกัน

| # | Item | Status | File | Sources read |
|---|---|---|---|---|
| 01 | Business process As-Is / To-Be | done | [01-business-process.md](01-business-process.md) | `app.py`, `payment_events.py`, billing/refund modules, `docs/runtime.md` §Staging plan, `README.md`, `contract.json` |
| 02 | Use cases | done | [02-use-cases.md](02-use-cases.md) | route modules, core `auth.py`/`http.py`, `API.md`, ops record |
| 03 | User flows (API call flows) | done | [03-user-flows.md](03-user-flows.md) | route modules, `__main__.py`, gateway `edge.py`, ops record |
| 04 | Traceability (RTM) | done | [04-traceability.md](04-traceability.md) | `PRODUCT.md`, `contract.json`, runtime docs, all test files, workflows |
| 05 | C4 Context | done | [05-c4-context.md](05-c4-context.md) | `API.md`, adapters, `_sentry.py`, `CONTRACT-SURFACE.md`, ops record |
| 06 | C4 Container | done | [06-c4-container.md](06-c4-container.md) | `Dockerfile`, `__main__.py`, migrations, core `db.py`, Railway API |
| 07 | C4 Component | done | [07-c4-component.md](07-c4-component.md) | `src/platform_payment/*.py`, core `http.py`/`auth.py`/`db.py` |
| 08 | ADRs | done (8) | [08-adr/](08-adr/) | `git log`, code, docs, workflows per ADR |
| 09 | ERD | done | [09-erd.md](09-erd.md) | `migrations.py`, `billing_migrations.py`, `ledger_migrations.py`, `app.py` |
| 10 | Sequences | done (4) | [10-sequences.md](10-sequences.md) | create, webhook settlement, refund, readiness code paths |
| 11 | State machines | done (6) | [11-state-machines.md](11-state-machines.md) | status writes + CHECK constraints |
| 12 | API & integrations | done (reuses `API.md`) | [12-api-integration.md](12-api-integration.md) → [`API.md`](../../API.md) | route modules, core `http.py`, adapters, `stripe_events.py`, gateway `edge.py`, probes |
| 13 | Threat model | done | [13-threat-model.md](13-threat-model.md) | auth, RLS, webhook, live-mode, Sentry code, gateway, probes |
| 14 | Backup & DR | done (gap: not in backups, RTO/RPO not defined) | [14-backup-dr.md](14-backup-dr.md) | `docs/runtime.md`, `docs/billing-runtime.md`, platform-db-backup README, Railway `BACKUP_TARGETS` |
| 15 | CI/CD pipeline | done | [15-cicd-pipeline.md](15-cicd-pipeline.md) | `check.yml`, `shared-runtime.yml`, `scripts/check.sh`, `pyproject.toml` |
| 16 | Deployment | done | [16-deployment.md](16-deployment.md) | `Dockerfile`, config code, Railway API, probes, ops record |
| 17 | Test coverage matrix | done | [17-test-coverage-matrix.md](17-test-coverage-matrix.md) | `tests/*.py`, `runtime_tests/*.py`, workflows |
| 18 | Git & release workflow | done | [18-git-release-workflow.md](18-git-release-workflow.md) | `git log`, `gh pr list`, branch protection + rulesets API |
| 19 | Service dependency map | done | [19-service-dependency-map.md](19-service-dependency-map.md) | `pyproject.toml`, `uv.lock`, `Dockerfile`, `contract.json`, gateway `routes.json` |
| 20 | Observability | done | [20-observability.md](20-observability.md) | `_sentry.py`, core `http.py`, Railway vars, uptime-worker `targets.ts` |
| 21 | Incident response | done | [21-incident-response.md](21-incident-response.md) | runtime docs, `live_mode.py`, `pending_webhooks.py`, Railway healthcheck |
| 22 | DORA | done (deploy data from Railway only; lead time not computable) | [22-dora.md](22-dora.md) | `git log`, `gh run list`, deployments API, Railway API |

## ADR list

- [ADR-0001](08-adr/ADR-0001-fastapi-on-pinned-platform-backend-core.md) FastAPI บน platform-backend-core ที่ pin เป็น commit
- [ADR-0002](08-adr/ADR-0002-postgresql-force-rls-restricted-runtime-role.md) PostgreSQL ของ service เอง, FORCE RLS, runtime role จำกัด, migrate แยก
- [ADR-0003](08-adr/ADR-0003-stripe-test-mode-only-live-refused.md) Stripe sandbox เท่านั้น, live ถูกปฏิเสธ (D8)
- [ADR-0004](08-adr/ADR-0004-durable-idempotency-and-webhook-only-settlement.md) Durable idempotency และ settlement จาก verified webhook เท่านั้น
- [ADR-0005](08-adr/ADR-0005-append-only-double-entry-ledger-in-database.md) Ledger double-entry append-only บังคับใน PostgreSQL
- [ADR-0006](08-adr/ADR-0006-railway-serverless-docker-cli-deploy.md) Railway serverless, Dockerfile, CLI deploy; CI ไม่ deploy
- [ADR-0007](08-adr/ADR-0007-identity-rs256-jwks-trust.md) Identity RS256 ผ่าน JWKS, audience `payment-api`
- [ADR-0008](08-adr/ADR-0008-sentry-errors-only-no-free-text.md) Sentry errors only, ไม่ส่ง free text

## Existing docs reused

[`API.md`](../../API.md), [`docs/CONTRACT-SURFACE.md`](../CONTRACT-SURFACE.md), [`docs/runtime.md`](../runtime.md), [`docs/billing-runtime.md`](../billing-runtime.md), [`docs/verification-2026-10-15.md`](../verification-2026-10-15.md), [`docs/UPGRADE-PLAN.md`](../UPGRADE-PLAN.md); central: [ADR-0002 production baseline](https://github.com/bookchaowalit/architecture-docs/blob/main/docs/architecture/decisions/ADR-0002-production-baseline-monitoring-backups.md), [ATTACK-PATH-DIAGRAMS.md](https://github.com/bookchaowalit/architecture-docs/blob/main/docs/architecture/ATTACK-PATH-DIAGRAMS.md) (ไม่มีส่วนของ payment)

## Findings worth the owner's attention

- `/readyz` 503: ไม่มี `PAYMENT_STRIPE_API_KEY` (sandbox), `PAYMENT_STRIPE_WEBHOOK_SECRET`, `PAYMENT_SUCCESS_URL`, `PAYMENT_CANCEL_URL` บน Railway; `PAYMENT_PLAN_CATALOG_JSON` ก็ไม่มี (ดู 16)
- DB ของ payment ไม่อยู่ใน nightly backup (`ops-backups/db-backup` `BACKUP_TARGETS` 31 ชื่อ ไม่มี payment); RTO/RPO ไม่ได้กำหนด (ดู 14)
- ไม่อยู่ใน uptime worker; ไม่มี heartbeat หรือ alert ของ pending webhook (ดู 20, 21)
- `/metrics`, `/docs`, `/openapi.json` เปิด public บน Railway URL (ดู 12, 13)
- Stripe webhook ต้องเข้า Railway URL ตรง เพราะ gateway ต้องการ Bearer และไม่ forward `Stripe-Signature` (ดู 03, 12)
- `main` ไม่มี branch protection; deploy เป็น CLI ไม่มี commit hash → lead time วัดไม่ได้ (ดู 18, 22)
- docs ล้าหลัง: `PRODUCT.md` บอก Sentry no-op จนกว่าตั้ง DSN แต่ `SENTRY_DSN` ตั้งแล้ว; `docs/UPGRADE-PLAN.md` ยังบอก "no runtime implementation"; `PRODUCT.md` อ้าง `fixtures/gates/` ที่ไม่มีอยู่ (ดู 04, 20)
- `depends_on: security` แต่ไม่มี audit call ไป security-api (ดู 13, 19)

Validate: `node ~/work/core22/tools/validate.mjs docs/diagrams/*.md docs/diagrams/08-adr/*.md`
