# ADR-0002 — PostgreSQL ของ service เอง, FORCE RLS ต่อ workspace, runtime role แบบจำกัด, migrate แยก

- **Status:** accepted (retro-documented)
- **วันที่:** migration `001-payments` (PR #1), `002-billing-refunds`, `003-ledger` (`f9a3796`, 2026-10-29)

## Context

ข้อมูลการจ่ายเงินต้องแยก tenant อย่างแน่นอน แม้โค้ดมี bug; service ต้องไม่เป็นเจ้าของ schema ตอนรัน

## Decision

- ทุกตารางธุรกิจ ENABLE + FORCE ROW LEVEL SECURITY, policy `workspace_id = current_setting('app.workspace_id')`; core ตั้งค่านี้แบบ transaction-local จาก JWT `ws` เท่านั้น
- webhook routing ใช้ policy พิเศษ `payment_binding_route` (SELECT เฉพาะ provider + object id ที่ verify แล้ว) และ `verified_provider_event` บน spool
- core ปฏิเสธ role ที่เป็น SUPERUSER หรือ BYPASSRLS; runtime role ได้ grant เท่าที่ `READINESS_PERMISSIONS` กำหนด ไม่มี DELETE/TRUNCATE
- startup ไม่ migrate; `python -m platform_payment migrate` ใช้ `DATABASE_MIGRATION_DSN` แยก
- hosted: Neon project `platform-payment` ของตัวเอง, runtime role `payment_runtime` (ops record)

## Consequences

- `/readyz` ตรวจ grant ทีละสิทธิ์ + migration checksum + FORCE RLS ก่อนตอบ 200
- migration 003 ต้องยก FORCE RLS ชั่วคราวภายใน transaction เพื่อ backfill (owner role); ยังไม่ได้พิสูจน์กับ owner ที่ไม่ใช่ superuser (`docs/runtime.md`)

## Evidence

`src/platform_payment/migrations.py`, `billing_migrations.py`, `ledger_migrations.py`, `storage.py`, `app.py` (`READINESS_PERMISSIONS`, `readiness`), `__main__.py`, platform-backend-core `21d5f44` `db.py`, `runtime_tests/test_readiness_grants.py`, ops record: book-platforms-status (2026-11-03)
