# ADR-0005 — Ledger แบบ double-entry, append-only, บังคับ invariant ใน PostgreSQL

- **Status:** accepted (retro-documented)
- **วันที่:** `f9a3796` (2026-10-29), migration gate "ledger rollback"

## Context

contract มี capability `ledger` และ gate "ledger rollback"; ต้องไม่ให้ bug ใน Python ทำยอดไม่สมดุลหรือ refund เกินยอดที่รับ

## Decision

- ตาราง `payment_ledger_entries` (integer minor units, PK `(workspace_id, source_kind, source_id, account, direction)`)
- `payment_succeeded`: debit `provider_clearing` / credit `payment_revenue`; `refund_succeeded`: debit `payment_refunds` / credit `provider_clearing`
- trigger `AFTER INSERT ... FOR EACH STATEMENT` ปฏิเสธ journal ที่ไม่สมดุล, หลายสกุลเงิน, น้อยกว่า 2 legs หรือ refund เกิน revenue ต่อ intent (SQLSTATE 23514)
- UPDATE/DELETE/TRUNCATE raise; post เฉพาะเมื่อ effect row ใหม่ถูก insert
- invoice ของ subscription ไม่ถูก post (เป็น provider metadata)

## Consequences

- posting fail → settlement ทั้งก้อน rollback, webhook ตอบ 500, Stripe redeliver แล้ว apply ครั้งเดียว
- image ก่อน migration 003 ไม่ใช่ rollback target

## Evidence

`src/platform_payment/ledger.py`, `ledger_migrations.py`, `payment_events.py`, `refunds.py`, `docs/runtime.md` §Ledger, `runtime_tests/test_ledger.py`
