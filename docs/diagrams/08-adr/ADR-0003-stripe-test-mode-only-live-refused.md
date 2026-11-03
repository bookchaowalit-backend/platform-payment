# ADR-0003 — Stripe sandbox เท่านั้น: live key และ live event ถูกปฏิเสธ (D8)

- **Status:** accepted (retro-documented)
- **วันที่:** `b22ef2a` (2026-10-29), fail-closed provider ใน `ea68a9d`

## Context

PR #1 ถูกปิดภายใต้ owner decision D8 เพราะซ้อนกับ live Stripe path ที่ `bookchaowalit/platform-webhooks-worker` ให้บริการอยู่ และ live activation ยังไม่ผ่าน review

## Decision

- `refuse_live_mode` ตรวจทุก environment variable: ค่าใดขึ้นต้น `sk_live_` หรือ `rk_live_` → `LiveModeRefused` (บอกชื่อตัวแปร ไม่บอกค่า)
- เรียกตอน `create_app`, ใน `configured_provider` (ก่อนเช็กว่า config ครบ), ทุก CLI command (exit 2 ก่อนเปิด DB) และทุกครั้งที่ `/readyz` ถูกเรียก
- provider ที่ inject ต้องประกาศ `livemode = False` ชัดเจน; `StripeProvider` รับเฉพาะ `sk_test_`/`rk_test_`; event ที่ `livemode` ไม่ใช่ `false` → 400
- ไม่มี flag หรือ env ใดเปิด live mode และห้ามเพิ่ม

## Consequences

- hosted service ตอบ `/readyz` 503 จนกว่าจะมี sandbox key (Infisical `/stripe` มีแต่ live keys; ops record)
- live activation ต้องเป็น owner decision แยก พร้อม payment/security review

## Evidence

`src/platform_payment/live_mode.py`, `config.py`, `adapters.py`, `app.py`, `__main__.py`, `docs/runtime.md` §Test-mode only, `runtime_tests/test_live_mode.py`, `runtime_tests/test_review_fixes.py`, `.github/workflows/shared-runtime.yml` (image probe), ops record: book-platforms-status (2026-11-03)
