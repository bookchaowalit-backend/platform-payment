# ADR-0006 — Railway serverless (sleep), image จาก Dockerfile, deploy ด้วย CLI upload; CI ไม่ deploy

- **Status:** accepted (retro-documented)
- **วันที่:** Dockerfile multi-stage `a9de897` (2026-10-29); Railway deployment แรก 2026-11-03 08:13 UTC

## Context

owner อนุมัติ serverless mode สำหรับ platform services ใหม่ทุกตัว (2026-10-29, ops record); repo เป็น public, CI บน GitHub-hosted runners

## Decision

- `Dockerfile` สอง stage, runtime `python:3.12-slim`, UID/GID 65532, `CMD python -m platform_payment serve`, `PORT=8080`
- Railway project `platform-payment`, service `payment-api`, builder DOCKERFILE, 1 replica `asia-southeast1-eqsg3a`, `sleepApplication` true, healthcheck `/healthz` (timeout 60 s), restart ON_FAILURE สูงสุด 10
- deploy ด้วย `railway up` (CLI upload, deployment meta ไม่มี commit hash); workflow `image` แค่ `docker build` ไม่ push ไม่ deploy

## Consequences

- push to main ไม่ deploy อัตโนมัติ; DORA จาก git/gh วัด deployment ไม่ได้ (ดู 22)
- cold start หลัง sleep + Neon resume (core ตั้ง pool wait 20 s, connect timeout 15 s)

## Evidence

`Dockerfile`, `.github/workflows/shared-runtime.yml`, `docs/runtime.md` §Container image, `tests/test_build_hygiene.py`, Railway API read 2026-11-03 (service instance + deployment meta), ops record: book-platforms-status (2026-11-03) (deploy recipe)
