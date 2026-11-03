# ADR-0007 — เชื่อ token RS256 จาก platform identity ผ่าน JWKS, audience `payment-api`, workspace จาก `ws`

- **Status:** accepted (retro-documented)
- **วันที่:** owner decision 2026-10-29 (services trust platform-identity issuer; ops record); identity activated 2026-11-03

## Context

service ต้องรู้ actor, workspace และสิทธิ์ โดยไม่เก็บ user เอง

## Decision

- `serve` ต้องมี `JWT_ISSUER`, `JWT_AUDIENCE` และ `JWT_PUBLIC_KEY` หรือ `JWT_JWKS_URL` (hosted ตั้ง `JWT_JWKS_URL`)
- core `TokenVerifier`: RS256 เท่านั้น, ต้องมี `sub ws iat exp jti iss aud`, อายุ token ≤ 3600 s, ห้าม `del`/`obo`; JWKS ผ่าน HTTPS ไม่ follow redirect, refresh ไม่เกินครั้งละ 60 s
- `ws` เป็น tenant authority เดียว; scope 5 ตัว (`payments:read|write|refund`, `billing:read|write`)

## Consequences

- revocation ทำไม่ได้ใน service (core comment: "Shared services cannot enforce delegation status or revocation"); พึ่งอายุ token สั้น
- JWKS ล่มขณะ refresh → cache ว่างจนครบ 60 s → 401 ทุก request ในช่วงนั้น

## Evidence

`src/platform_payment/__main__.py`, platform-backend-core `21d5f44` `src/platform_backend_core/auth.py`, `API.md` §Authentication, Railway variable names (2026-11-03: `JWT_ISSUER`, `JWT_AUDIENCE`, `JWT_JWKS_URL`), ops record: book-platforms-status (2026-11-03)
