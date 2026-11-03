# ADR-0004 — Durable idempotency ก่อนเรียก provider และ settlement จาก verified webhook เท่านั้น

- **Status:** accepted (retro-documented)
- **วันที่:** PR #1 (`SHARED-INFRA-2026-10-15`), merged `bfd4f7d` 2026-10-29

## Context

การเรียก Stripe อาจ timeout หลัง provider สำเร็จแล้ว; browser return page ปลอมได้; event มาซ้ำหรือสลับลำดับได้

## Decision

- commit แถว `creating` พร้อม UUID, `provider_key` และ lease 60 s (fence UUID) **ก่อน** เรียก SDK; uniqueness = workspace + operation + caller key; payload ต่างใต้ key เดิม → 409
- error/timeout จาก provider → `unknown` + 202; replay ด้วย key เดิมไม่เรียก provider ซ้ำ; reconcile ใช้ provider key เดิม และปฏิเสธหลัง 20 ชั่วโมง (Stripe เก็บ idempotency ≥ 24 ชั่วโมง)
- settlement เกิดจาก webhook ที่ verify raw-body signature (tolerance 300 s) + binding ที่บันทึกไว้ + amount/currency ตรงเท่านั้น; event id dedupe ต่อ provider และ fingerprint SHA-256 ของ raw body กัน payload เปลี่ยน
- inbox, state transition, effect และ ledger journal commit ใน transaction เดียว; event ที่มาก่อน binding ถูกเก็บใน spool แล้ว replay ได้

## Consequences

- ต้องมี operator ทำ manual reconciliation สำหรับรายการเก่ากว่า 20 ชั่วโมง
- event ที่ไม่ bound ได้ 409 ให้ Stripe retry; `payment_intent.*` ที่ยังไม่ bound หลัง 3 ครั้งถูก ack เป็น ignored

## Evidence

`src/platform_payment/app.py`, `provider_operations.py`, `payment_events.py`, `pending_webhooks.py`, `storage.py`, `docs/runtime.md` §API, idempotency and ambiguous outcomes, `runtime_tests/test_api.py`, `test_billing.py`, `test_refunds.py`, `test_review_fixes.py`
